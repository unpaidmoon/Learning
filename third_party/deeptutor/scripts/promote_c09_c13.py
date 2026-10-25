"""Promote already verified pure excerpts; no environment or dependency changes."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
OFFLINE = ROOT / "checks/offline-candidate"
PACKAGE = ROOT / "src/deeptutor_reuse"
manifest = json.loads((ROOT / "reuse-manifest.json").read_text("utf-8"))
ledger = json.loads((OFFLINE / "reuse-ledger.json").read_text("utf-8"))
files = {item["path"]: item for item in ledger["source_files"]}
specs = [
    ("deeptutor/book/blocks/base.py", "compiler_core.py", "book_block_compiler.py",
     ["BlockContext", "BlockGenerator", "GenerationFailure", "_classify_failure", "_failure_metadata"]),
    ("deeptutor/book/progress.py", "progress_core.py", "book_progress.py",
     ["latest_attempts", "recompute_score", "recompute_weak_chapters", "record_attempt", "mark_visited", "toggle_bookmark", "completion_ratio"]),
    ("deeptutor/book/export.py", "book_export.py", "book_export.py",
     ["render_block", "render_book_markdown", "export_filename"]),
]
for original, candidate, target, symbols in specs:
    upstream = OFFLINE / "upstream" / original
    raw = upstream.read_bytes()
    item = files["upstream/" + original]
    assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == item["git_blob_sha1"]
    destination = ROOT / "upstream" / original
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert destination.read_bytes() == raw
    else:
        shutil.copyfile(upstream, destination)
    source = (OFFLINE / "tools" / candidate).read_bytes()
    (PACKAGE / target).write_bytes(source)
    if not any(x["path"] == original for x in manifest["upstream_files"]):
        manifest["upstream_files"].append({
            "path": original, "bytes": len(raw), "git_blob_sha1": item["git_blob_sha1"], "sha256": item["sha256"],
            "url": "https://github.com/HKUDS/DeepTutor/blob/" + manifest["source_commit"] + "/" + original,
        })
    target_path = "src/deeptutor_reuse/" + target
    if not any(x["target"] == target_path for x in manifest["migrations"]):
        manifest["migrations"].append({
            "candidate": "C09" if "progress" in target else "C10", "upstream": original,
            "target": target_path, "mode": "A", "symbols": symbols,
            "changes": ["Retained pure candidate promoted to importable package; only model import path changed."]
              + (["Excluded default generator registry and its application/provider imports."] if "compiler" in target else []),
            "target_sha256": hashlib.sha256(source).hexdigest(),
            "validation": "checks/integration-candidate/verify_integration.py (source and installed-wheel runs)",
        })
for candidate, original, target, symbols, differences in [
    ("C10", "deeptutor/book/compiler.py", "book_resume.py",
     ["BookCompileSnapshot", "outline_digest", "confirm_outline", "begin_block", "make_block_context", "complete_block", "interrupt_block", "resume_book", "readable_chapters"],
     ["New project DTO/transitions calling retained original models/terminal aggregation; not an upstream scheduler.",
      "No storage, provider, implicit retry or authoritative queue; unknown attempts require application reconciliation."]),
    ("C09", "deeptutor/learning/models.py", "learning_actions.py",
     ["LearningActionResult", "apply_learning_action", "independent_grade_eligible"],
     ["New project adapter over original Progress/MasteryInteraction/MasteryEvent.",
      "Skip/reveal only emit events and do not append QuizAttempt or grant mastery."]),
    ("C11/C12", "deeptutor/visualizers/protocol.py", "offline_data.py",
     ["Inline", "TextPayload", "FigurePayload", "RangeParameter", "PlotData", "validate_offline_visualization"],
     ["New project closed-data policy based on existing offline-candidate fixture validators, using original VisualizationEnvelope.",
      "No arbitrary HTML/JS/iframe, entry URL, business capability or automatic model events; requires host CSP/asset manifest too."]),
]:
    target_path="src/deeptutor_reuse/"+target
    manifest["migrations"]=[x for x in manifest["migrations"] if x["target"]!=target_path]
    manifest["migrations"].append({"candidate":candidate,"upstream":original,"target":target_path,"mode":"project-adapter",
                                    "source_body_reused":False,"symbols":symbols,"changes":differences,
                                    "target_sha256":hashlib.sha256((PACKAGE/target).read_bytes()).hexdigest(),
                                    "validation":"checks/integration-candidate/verify_integration.py (source and installed-wheel runs)"})
(ROOT / "reuse-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
print(json.dumps({"promoted": [s[2] for s in specs], "dependencies_changed": False}))
