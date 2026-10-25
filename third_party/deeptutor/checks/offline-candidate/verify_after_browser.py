"""Verify the exported file tree remained immutable during actual offline use."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


identity = json.loads((ROOT / "evidence/package-identity.json").read_text("utf-8"))
manifest = json.loads((ROOT / "verification/unpacked-r1/asset-manifest.json").read_text("utf-8"))
assert sha(ROOT / identity["zip_path"]) == identity["zip_sha256"]
assert sha(ROOT / "verification/unpacked-r1/asset-manifest.json") == identity["asset_manifest_sha256"]
for record in manifest["files"]:
    actual = ROOT / "verification/unpacked-r1" / record["path"]
    assert actual.stat().st_size == record["bytes"] and sha(actual) == record["sha256"]
browser = json.loads((ROOT / "evidence/browser-verification.json").read_text("utf-8"))
observed = browser["before_reload"]
assert observed["document"]["offline"] and observed["document"]["storageWrites"] == []
assert observed["observations"]["failedRequests"] == []
assert observed["observations"]["webSockets"] == []
assert observed["observations"]["pageErrors"] == []
assert observed["observations"]["consoleErrors"] == []
assert observed["document"]["diagnostics"]["policyViolations"] == []
assert all(row["url"].startswith("file:") for row in observed["observations"]["requests"])
assert observed["checkpoints"][0]["choiceGrade"] == "correct"
assert observed["checkpoints"][0]["writtenGrade"] == "ungraded"
assert observed["checkpoints"][1]["slope"] == "8"
assert browser["after_reload"]["slope"] == "2" and browser["after_reload"]["storageWrites"] == []
source_lookup = {}
for name in ["intake.json", "intake-extra.json"]:
    for source in json.loads((ROOT / name).read_text("utf-8"))["files"]:
        source_lookup[source["path"]] = source["sha"]
for source in json.loads((REPO / "third_party/deeptutor/upstream-lock.json").read_text("utf-8"))["files"]:
    source_lookup.setdefault(source["path"], source["git_blob_sha1"])
sources = []
for record in identity["upstream_files"]:
    path = ROOT / record["path"]
    raw = path.read_bytes()
    relative = path.relative_to(ROOT / "upstream").as_posix()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert blob == source_lookup[relative]
    sources.append({**record, "git_blob_sha1": blob, "source_commit": "a31d82624c27d75f4e771d4fcb78dce16470b383"})
targets = ["tools/compiler_core.py", "tools/book_export.py", "tools/progress_core.py", "package/runtime/upstream.js", "package/runtime/reader.js", "package/runtime/reader.css"]
ledger = {
    "source_commit": "a31d82624c27d75f4e771d4fcb78dce16470b383",
    "source_files": sources,
    "target_files": [{"path": path, "sha256": sha(ROOT / path)} for path in targets],
    "actual_reuse": [
        {"candidate": "C10", "symbols": ["Page (BookPage alias)", "Block (LearningBlock alias)", "Book", "Spine", "Chapter"], "mode": "R", "evidence": "Original loaded model file bytes match fixed source; used to compile/validate six actual blocks and two pages"},
        {"candidate": "C10", "symbols": ["BlockContext", "BlockGenerator.generate", "_failure_metadata"], "mode": "A", "changes": "Original prefix retained; model import rewritten; default AI registry excluded", "evidence": "Success/error/CancelledError executed in pure fixture compiler"},
        {"candidate": "C10", "symbols": ["BookCompiler._finalize_page_status"], "mode": "A-existing-wrapper", "changes": "Existing isolated reuse wrapper accepts terminal blocks only", "evidence": "Both compiled pages reach ready"},
        {"candidate": "C10", "symbols": ["render_block", "render_book_markdown"], "mode": "A", "changes": "Whole original export.py retained; one model import rewritten", "evidence": "Produces accompanying package/book.md"},
        {"candidate": "C10", "symbols": ["record_attempt", "recompute_score", "recompute_weak_chapters"], "mode": "A-check-only", "changes": "Whole original progress.py retained; one model import rewritten", "evidence": "Ungraded attempt remains score0 and weak_chapters empty; browser does not persist formal progress"},
        {"candidate": "C10-renderer", "symbols": ["normalizeQuizQuestionType", "isChoiceQuizQuestion", "resolveChoiceAnswerKey", "sequentialReadTarget", "chapterReadingPercent"], "mode": "R-transpile", "changes": "Original TypeScript files unchanged; type erasure and classic-script exports wrapper only", "evidence": "Actual browser choice grading and reading scroll use compiled original functions"},
        {"candidate": "C11/C12", "symbols": ["VisualizationEnvelope", "RendererRef", "stripCodeFence", "parseChartConfigStrict"], "mode": "R-excerpt/A", "changes": "Original envelope DTO excerpt; existing attributed strict-JSON adapter excludes upstream repair fallback", "evidence": "Typed envelope serialized; actual browser plot decoder uses strict parser"},
    ],
    "new_implementation": ["Bounded pure-data payload schema", "Synthetic fixture generator", "DOM/SVG offline renderer", "Local asset collector and ZIP packer"],
}
(ROOT / "reuse-ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
result = {"status": "passed", "unpacked_files_unchanged_after_use": len(manifest["files"]) + 1,
          "source_blobs_verified": len(sources), "browser": browser["browser"], "http_or_websocket_requests_observed": 0,
          "storage_writes_observed": 0, "application_backend_started": False, "zip_sha256": identity["zip_sha256"],
          "model_requests": 0, "not_run": ["formal application export/store/API/UI", "arbitrary HTML/JS visualization", "all-function families/animation", "user learning acceptance"]}
(ROOT / "evidence/final-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
