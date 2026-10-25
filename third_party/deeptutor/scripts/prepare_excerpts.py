"""Rebuild selected modules from archived, unmodified DeepTutor sources.

Run from any directory with Python 3.12. This preparation step does not fetch
network data or import DeepTutor's application runtime.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstream"
PACKAGE = ROOT / "src" / "deeptutor_reuse"
COMMIT = "a31d82624c27d75f4e771d4fcb78dce16470b383"
REPO = "https://github.com/HKUDS/DeepTutor"


def source(path: str) -> str:
    return (UPSTREAM / path).read_text(encoding="utf-8")


def excerpt(path: str, name: str) -> tuple[str, list[int]]:
    text = source(path)
    lines = text.splitlines(keepends=True)
    node = next(n for n in ast.parse(text).body if getattr(n, "name", None) == name)
    start = min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])
    return "".join(lines[start - 1 : node.end_lineno]), [start, node.end_lineno]


def write_module(name: str, text: str) -> None:
    (PACKAGE / name).write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    PACKAGE.mkdir(parents=True, exist_ok=True)
    locked = json.loads((ROOT / "upstream-lock.json").read_text("utf-8"))
    for item in locked["files"]:
        raw = (UPSTREAM / item["path"]).read_bytes()
        blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
        if len(raw) != item["bytes"] or hashlib.sha1(blob).hexdigest() != item["git_blob_sha1"]:
            raise ValueError(f"UPSTREAM_BLOB_MISMATCH: {item['path']}")
    migrations: list[dict] = []

    def record(candidate: str, path: str, target: str, mode: str, symbols: list[str], changes: list[str]) -> None:
        migrations.append({
            "candidate": candidate,
            "upstream": path,
            "target": target,
            "mode": mode,
            "symbols": symbols,
            "changes": changes,
            "target_sha256": hashlib.sha256((ROOT / target).read_bytes()).hexdigest(),
        })

    copies = [
        ("C08", "deeptutor/services/rag/eval/dataset.py", "eval_dataset.py", ["EvalCase", "EvalDataset", "load_dataset"]),
        ("C08", "deeptutor/services/rag/eval/metrics.py", "eval_metrics.py", ["recall_at_k", "precision_at_k", "ndcg_at_k", "reciprocal_rank", "average_precision", "aggregate_query_metrics"]),
        ("C10", "deeptutor/book/models.py", "book_models.py", ["Book", "Spine", "Chapter", "Page", "Block", "SourceAnchor"]),
        ("C11", "deeptutor/visualizers/protocol.py", "visualization_protocol.py", ["RendererRef", "VisualizationEnvelope", "VisualizerManifest", "VisualizerPlugin"]),
        ("C14", "deeptutor/services/file_io.py", "file_io.py", ["atomic_write_json", "atomic_write_text", "read_text"]),
    ]
    for candidate, old, new, symbols in copies:
        (PACKAGE / new).write_bytes((UPSTREAM / old).read_bytes())
        record(candidate, old, f"src/deeptutor_reuse/{new}", "R", symbols, ["Package and module path changed; file bytes unchanged."])

    path = "deeptutor/services/rag/source_pages.py"
    function, _ = excerpt(path, "source_digest")
    write_module("source_digest.py", '"""DeepTutor source_digest excerpt; provenance in distribution UPSTREAM.md."""\n\nimport hashlib\nfrom pathlib import Path\n\n\n' + function)
    record("C07", path, "src/deeptutor_reuse/source_digest.py", "R-excerpt", ["source_digest"], ["Unmodified function and its two standard-library imports extracted; source resolution and rendering omitted."])
    snippets = [excerpt(path, n)[0] for n in ["SourcePageError", "SourceRef"]]
    write_module("source_ref.py", '"""Unmodified upstream DTO excerpt; project authorization is external."""\n\nfrom pydantic import BaseModel, ConfigDict, Field\n\n\n' + "\n\n\n".join(snippets))
    record("C07", path, "src/deeptutor_reuse/source_ref.py", "R-excerpt", ["SourceRef", "SourcePageError"], ["Only DTO/error classes extracted; project identity, crop validation, and authority are external."])

    path = "deeptutor/learning/models.py"
    names = ["PendingOption", "PendingQuestion", "InteractionStatus", "MasteryInteraction", "MasteryEvent", "MasteryPathLease"]
    snippets = []
    for name in names:
        body, _ = excerpt(path, name)
        if name == "PendingQuestion":
            # The legacy parser imports DT pending/text-display helpers. New
            # typed contracts reject legacy string options through Pydantic.
            start = body.index('    @field_validator("options", mode="before")')
            end = body.index("    @property", start)
            body = body[:start] + body[end:]
        snippets.append(body)
    write_module("learning_transactions.py", '"""DeepTutor typed transaction excerpts; legacy string-option import removed."""\n\nfrom __future__ import annotations\n\nfrom enum import Enum\nimport time\nfrom typing import Any\n\nfrom pydantic import BaseModel, ConfigDict, Field, model_validator\n\n\n' + "\n\n\n".join(snippets))
    record("C09", path, "src/deeptutor_reuse/learning_transactions.py", "A", names, ["Selected typed class closure; removed _read_legacy_option_strings and its DT pending/text-display import. New typed options only.", "No persistence, scoring, schedule, revision, or project identity behavior added."])

    path = "deeptutor/book/compiler.py"
    text = source(path)
    cls = next(n for n in ast.parse(text).body if getattr(n, "name", None) == "BookCompiler")
    method = next(n for n in cls.body if getattr(n, "name", None) == "_finalize_page_status")
    start = min(method.lineno, *(d.lineno for d in method.decorator_list))
    body = "".join(text.splitlines(keepends=True)[start - 1 : method.end_lineno])
    write_module("book_compiler_state.py", '"""Original terminal aggregation plus a project-side precondition wrapper."""\n\nfrom .book_models import BlockStatus, Page, PageStatus\n\n\nclass _OriginalCompilerState:\n' + body + '\n\n\ndef finalize_terminal_page(page: Page) -> None:\n    """Aggregate only READY/ERROR blocks; other states still need a decision."""\n    if any(block.status not in {BlockStatus.READY, BlockStatus.ERROR} for block in page.blocks):\n        raise ValueError("PAGE_NOT_TERMINAL")\n    _OriginalCompilerState._finalize_page_status(page)\n')
    record("C10", path, "src/deeptutor_reuse/book_compiler_state.py", "A", ["BookCompiler._finalize_page_status", "finalize_terminal_page"], ["Original static method body retained in a minimal owner class.", "Wrapper rejects pending/generating/hidden blocks; original method otherwise unchanged.", "Compiler model factory, storage, stream, concurrency, and retry pipeline excluded."])

    path = "deeptutor/visualizers/protocol.py"
    names = ["RendererRef", "VisualizationPayload", "VisualizationPresentation", "VisualizationInteraction", "VisualizationEnvelope"]
    write_module("visualization_models.py", '"""Original envelope DTOs without the optional jsonschema validation import."""\n\nfrom typing import Any, Literal\n\nfrom pydantic import BaseModel, Field\n\n\n' + "\n\n\n".join(excerpt(path, n)[0] for n in names))
    record("C11", path, "src/deeptutor_reuse/visualization_models.py", "R-excerpt", names, ["Unmodified data classes extracted with Pydantic-only imports; no safety or schema validation claim."])
    write_module("visualization_refs.py", '"""Original recursive remote-schema-reference detection excerpt."""\n\nfrom typing import Any\n\n\n' + excerpt(path, "_find_remote_schema_ref")[0] + '\n\n\nfind_remote_schema_ref = _find_remote_schema_ref\n')
    record("C11", path, "src/deeptutor_reuse/visualization_refs.py", "R-excerpt", ["_find_remote_schema_ref"], ["Function body unchanged; added public alias find_remote_schema_ref."])

    path = "deeptutor/agents/research/data_structures.py"
    text = source(path)
    const_start = text.index("DEFAULT_TOPIC_SIMILARITY_THRESHOLD =")
    const_end = text.index("\n\nclass TopicStatus", const_start)
    cls = next(n for n in ast.parse(text).body if getattr(n, "name", None) == "DynamicTopicQueue")
    snippets = []
    for name in ["_normalize_topic", "_topic_tokens", "_topic_similarity"]:
        method = next(n for n in cls.body if getattr(n, "name", None) == name)
        start = min(method.lineno, *(d.lineno for d in method.decorator_list))
        snippets.append("".join(text.splitlines(keepends=True)[start - 1 : method.end_lineno]))
    write_module("research_similarity.py", '"""Original title-normalization/similarity methods; queue ownership omitted."""\n\nimport difflib\nimport re\n\n' + text[const_start:const_end] + '\n\n\nclass TopicSimilarity:\n' + "\n\n".join(snippets) + '\n\n\nnormalize_topic = TopicSimilarity._normalize_topic\ntopic_tokens = TopicSimilarity._topic_tokens\ntopic_similarity = TopicSimilarity._topic_similarity\n')
    record("C13", path, "src/deeptutor_reuse/research_similarity.py", "R-excerpt", ["DynamicTopicQueue._normalize_topic", "DynamicTopicQueue._topic_tokens", "DynamicTopicQueue._topic_similarity"], ["Original constant values and method bodies retained in a stateless owner class with public aliases.", "Queue, naive datetime, trace truncation, auto-save, and model JSON repair excluded."])

    path = "deeptutor/services/parsing/cache.py"
    original = source(path)
    adapted = original.replace("from deeptutor.services.file_io import atomic_write_json", "from .file_io import atomic_write_json")
    assert adapted != original
    write_module("parser_cache.py", adapted)
    record("C14", path, "src/deeptutor_reuse/parser_cache.py", "A", ["lookup", "reserve", "write_manifest", "load_ir"], ["One import rewritten to isolated file_io; all cache/IR logic unchanged.", "Use only behind project-authorized roots and validated hex keys. Readiness alone does not validate cache content or source identity."])

    path = "web/components/visualize/VisualizationViewer.tsx"
    text = source(path)
    strip = text[text.index("function stripCodeFence"):text.index("\n\nfunction parseChartConfig")]
    split = re.search(r"function splitSvgBlocks\(raw: string\): string\[\] \{[\s\S]*?\n\}", text)
    assert split is not None
    web = ROOT / "web"
    web.mkdir(exist_ok=True)
    (web / "visualizationParsers.ts").write_text('// Selected DeepTutor helpers; provenance in ../UPSTREAM.md.\n\n' + strip + '\n\n' + split.group(0) + '\n\nexport { stripCodeFence, splitSvgBlocks };\n\n// Project adaptation: keep chart payloads strict JSON.\nexport function parseChartConfigStrict(source: string): unknown {\n  return JSON.parse(stripCodeFence(source));\n}\n', encoding="utf-8", newline="\n")
    record("C12", path, "web/visualizationParsers.ts", "A", ["stripCodeFence", "splitSvgBlocks", "parseChartConfig"], ["stripCodeFence and splitSvgBlocks bodies unchanged; exports added.", "Chart parser removes jsonish repair fallback; calls strict JSON.parse.", "No Next/React/i18n/viewer, SVG sanitizer, iframe, or remote renderer copied."])

    files = []
    for path in sorted(p for p in UPSTREAM.rglob("*") if p.is_file()):
        raw = path.read_bytes()
        relative = path.relative_to(UPSTREAM).as_posix()
        files.append({
            "path": relative,
            "bytes": len(raw),
            "git_blob_sha1": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "url": f"{REPO}/blob/{COMMIT}/{relative}",
        })
    manifest = {
        "schema_version": 1,
        "source_repository": REPO,
        "source_commit": COMMIT,
        "source_license": "Apache-2.0",
        "root_notice": {"status": "not_present", "evidence": "Fixed-commit recursive Git tree, truncated=false; exact root NOTICE request returned 404."},
        "scope": "Selected original files and pure modules; no complete DeepTutor installation or product-level acceptance.",
        "upstream_files": files,
        "migrations": migrations,
        "verification": "See research/reports/workbench/deeptutor-reuse-audit.md and adjacent evidence JSON; hashes are identity evidence, not behavior acceptance.",
    }
    (ROOT / "reuse-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"upstream_files": len(files), "migrations": len(migrations)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
