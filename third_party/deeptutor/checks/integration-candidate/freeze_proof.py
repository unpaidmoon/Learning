"""Archive code/real-wheel identity without touching product state."""
from pathlib import Path
import ast
import hashlib
import json
import zipfile

PACKAGE = Path(__file__).resolve().parents[2]
REPO = PACKAGE.parents[1]
CACHE = REPO / ".local/cache/workbench/deeptutor-c09-c13"
HERE = Path(__file__).resolve().parent


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


retained = []
for original, target in (
    ("deeptutor/book/blocks/base.py", "book_block_compiler.py"),
    ("deeptutor/book/progress.py", "book_progress.py"),
    ("deeptutor/book/export.py", "book_export.py"),
):
    raw = (PACKAGE / "upstream" / original).read_bytes()
    source = raw.decode("utf-8")
    if target == "book_block_compiler.py":
        expected = source.split("\nclass BlockGeneratorRegistry:", 1)[0].replace(
            "from ..models import (", "from deeptutor_reuse.book_models import ("
        )
    else:
        expected = source.replace("from .models import ", "from deeptutor_reuse.book_models import ")
    target_path = PACKAGE / "src/deeptutor_reuse" / target
    actual = target_path.read_text("utf-8")
    assert actual == expected
    node_count = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(ast.parse(actual)))
    retained.append({"upstream": original, "upstream_sha256": sha(raw), "target": target,
                     "target_sha256": sha(target_path.read_bytes()), "retained_function_bodies": node_count,
                     "verified_diff": "only model import; base excludes default registry prefix remainder"})

wheel = CACHE / "dist-final/learning_deeptutor_reuse-0.1.0-py3-none-any.whl"
manifest = json.loads((PACKAGE / "reuse-manifest.json").read_text("utf-8"))
with zipfile.ZipFile(wheel) as archive:
    modules = []
    for migration in manifest["migrations"]:
        if not migration["target"].startswith("src/deeptutor_reuse/"):
            continue
        path = migration["target"].removeprefix("src/")
        content = archive.read(path)
        assert sha(content) == migration["target_sha256"]
        assert content == (PACKAGE / migration["target"]).read_bytes()
        modules.append({"path": path, "sha256": sha(content)})
    notices = [p for p in archive.namelist() if p.endswith(("/LICENSE", "/UPSTREAM.md"))]
    for local in ("LICENSE", "UPSTREAM.md"):
        archived = "learning_deeptutor_reuse-0.1.0.dist-info/licenses/" + local
        assert archive.read(archived) == (PACKAGE / local).read_bytes()
    assert archive.read("deeptutor_reuse/reuse-manifest.json") == (PACKAGE / "reuse-manifest.json").read_bytes()

checks = json.loads((CACHE / "wheel-r3/summary.json").read_text("utf-8"))
assert checks["status"] == "passed" and checks["failed"] == 0
result = {"status": "passed", "commit": manifest["source_commit"], "wheel_sha256": sha(wheel.read_bytes()),
          "retained_originals": retained, "wheel_modules_verified": modules, "wheel_notices_verified": notices,
          "installed_evidence": ".local/cache/workbench/deeptutor-c09-c13/wheel-r3/summary.json",
          "checks_passed": checks["passed"], "model_calls": 0, "product_status": "not_certified",
          "verification_sources": []}
for path in sorted(HERE.glob("*.py")):
    result["verification_sources"].append({"path": path.name, "sha256": sha(path.read_bytes())})
(HERE / "evidence").mkdir(exist_ok=True)
(HERE / "evidence/proof.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
print(json.dumps({"status": result["status"], "retained_function_bodies": sum(x["retained_function_bodies"] for x in retained),
                  "wheel_modules": len(modules), "wheel_sha256": result["wheel_sha256"]}))
