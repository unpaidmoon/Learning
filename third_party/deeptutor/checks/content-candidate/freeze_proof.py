"""Final byte/AST proof; no application state or source/lock writes."""

from pathlib import Path
import ast
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
CACHE = REPO / ".local/cache/workbench/deeptutor-c06-c08-c13"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


manifest = json.loads((ROOT / "reuse-manifest.json").read_text("utf-8"))
original = ast.parse(
    (ROOT / "upstream/deeptutor/agents/research/data_structures.py").read_text("utf-8")
)
adapted = ast.parse(
    (ROOT / "src/deeptutor_reuse/research_records.py").read_text("utf-8")
)
old = next(
    x
    for x in original.body
    if isinstance(x, ast.ClassDef) and x.name == "DynamicTopicQueue"
)
new = next(
    x
    for x in adapted.body
    if isinstance(x, ast.ClassDef) and x.name == "ResearchTopicView"
)
old_methods = {n.name: n for n in old.body if isinstance(n, ast.FunctionDef)}
methods = []
for method in [n for n in new.body if isinstance(n, ast.FunctionDef)]:
    assert ast.dump(method, include_attributes=False) == ast.dump(
        old_methods[method.name], include_attributes=False
    )
    methods.append(method.name)
assert len(methods) == 9
wheel = CACHE / "dist/learning_deeptutor_reuse-0.1.0-py3-none-any.whl"
with zipfile.ZipFile(wheel) as z:
    modules = []
    for migration in manifest["migrations"]:
        if not migration["target"].startswith("src/deeptutor_reuse/"):
            continue
        path = migration["target"].removeprefix("src/")
        actual = z.read(path)
        assert (
            sha(actual) == migration["target_sha256"]
            and actual == (ROOT / migration["target"]).read_bytes()
        )
        modules.append(path)
    assert not any(
        "deepstudent" in name.lower() or "handout_adapter" in name
        for name in z.namelist()
    )
    for name in ("LICENSE", "UPSTREAM.md"):
        assert (
            z.read("learning_deeptutor_reuse-0.1.0.dist-info/licenses/" + name)
            == (ROOT / name).read_bytes()
        )
    assert (
        z.read("deeptutor_reuse/reuse-manifest.json")
        == (ROOT / "reuse-manifest.json").read_bytes()
    )
content = json.loads((CACHE / "wheel-r1/summary.json").read_text("utf-8"))
node = json.loads((CACHE / "c06-node-r4/summary.json").read_text("utf-8"))
previous = json.loads((CACHE / "book-regression-r1/summary.json").read_text("utf-8"))
assert content["status"] == node["status"] == previous["status"] == "passed"
assert node["retained_closure_type_errors"] == 0
result = {
    "status": "passed",
    "wheel_sha256": sha(wheel.read_bytes()),
    "wheel_modules_verified": len(modules),
    "C13_original_query_bodies_verified": methods,
    "C06_DS_not_in_Python_wheel": True,
    "content_checks": content["passed"],
    "node_checks": len(node["checks"]),
    "C06_closure_type_errors": 0,
    "previous_book_checks": previous["passed"],
    "model_or_retriever_calls": 0,
    "product_acceptance": "not_certified",
    "module_hashes": {
        p.name: sha(p.read_bytes()) for p in (ROOT / "src/deeptutor_reuse").glob("*.py")
    },
    "C06_runtime_hashes": {
        str(p.relative_to(HERE)).replace("\\", "/"): sha(p.read_bytes())
        for p in [
            HERE / "handout_adapter.ts",
            *sorted((HERE / "retained").glob("*.ts")),
        ]
    },
}
(HERE / "evidence").mkdir(exist_ok=True)
(HERE / "evidence/proof.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8"
)
print(
    json.dumps(
        {
            key: result[key]
            for key in (
                "status",
                "wheel_sha256",
                "wheel_modules_verified",
                "content_checks",
                "node_checks",
                "C06_closure_type_errors",
            )
        }
    )
)
