"""Register selected originals and explicit project adapters; keep locks fixed."""

from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "reuse-manifest.json").read_text("utf-8"))
extraction = json.loads(
    (ROOT / "checks/content-candidate/extraction-manifest.json").read_text("utf-8")
)
source = extraction["C08_matching"]
original = source["path"]
raw = (ROOT / "upstream" / original).read_bytes()
if not any(x["path"] == original for x in manifest["upstream_files"]):
    manifest["upstream_files"].append(
        {
            "path": original,
            "bytes": len(raw),
            "git_blob_sha1": source["git_blob_sha1"],
            "sha256": source["sha256"],
            "url": "https://github.com/HKUDS/DeepTutor/blob/"
            + manifest["source_commit"]
            + "/"
            + original,
        }
    )
items = [
    (
        "C08",
        original,
        "eval_matching.py",
        "R",
        True,
        [
            "MatchPolicy",
            "MatchResult",
            "GoldPassage",
            "RelevanceMatcher",
            "normalize_text",
            "tokenize",
            "source_text",
        ],
        ["Original complete standard-library-only file, unchanged bytes."],
    ),
    (
        "C06-project-adapter",
        "project_lossless_unit_contract",
        "source_windows.py",
        "project-adapter",
        False,
        [
            "TextUnit",
            "TextWindow",
            "split_text_unit",
            "reassemble_text_unit",
            "text_digest",
        ],
        [
            "New project codepoint-window implementation; not a DS function translation.",
            "Preserves exact text/whitespace/newline hashes and segment/version/time identity; no source reads or dispatch.",
        ],
    ),
    (
        "C13",
        "deeptutor/agents/research/data_structures.py",
        "research_records.py",
        "A",
        True,
        ["TopicStatus", "TopicBlock", "ResearchTopicView", "TraceReference"],
        [
            "Preserve 9 original read-only query/statistics methods and original topic DTO methods.",
            "UTC clocks, evidence asset/version/hash refs replace silently truncated raw_answer.",
            "No DynamicTopicQueue constructor, append/mark, auto-save or file IO enters runtime package.",
        ],
    ),
    (
        "C13",
        "deeptutor/agents/research/data_structures.py",
        "research_recovery.py",
        "project-adapter",
        False,
        ["RecoveryReview", "review_research_recovery"],
        [
            "New pure decision review; never changes states or dispatches.",
            "Researching requires actual attempt reconciliation; missing sources/evidence and naive timestamps are explicit blockers.",
        ],
    ),
]
for candidate, upstream, target, mode, reused, symbols, changes in items:
    path = "src/deeptutor_reuse/" + target
    manifest["migrations"] = [x for x in manifest["migrations"] if x["target"] != path]
    manifest["migrations"].append(
        {
            "candidate": candidate,
            "upstream": upstream,
            "target": path,
            "mode": mode,
            "source_body_reused": reused,
            "symbols": symbols,
            "changes": changes,
            "target_sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
            "validation": "checks/content-candidate/verify_content.py; installed-wheel run separate",
        }
    )
(ROOT / "reuse-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
)
print(
    json.dumps(
        {
            "registered": len(items),
            "dependency_changes": 0,
            "DS_TS_not_in_Python_wheel": True,
        }
    )
)
