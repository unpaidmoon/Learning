"""Read-only, sanitized evidence for the isolated platform worktree."""

import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "68c9cbcf7e96ef390ba51bd9e143ae75113f46dd"


def main():
    manifest = json.loads(
        (ROOT / "docs/development/platform-baseline-20261024.json").read_text("utf-8")
    )
    source = Path(manifest["source"])
    changes = []
    for item in manifest["files"]:
        path = source / item["path"]
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if digest != item["sha256"]:
            changes.append(item["path"])
    index = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--stage", "-z"]
    )
    result = {
        "development_head": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"]
        )
        .decode()
        .strip(),
        "source_manifest_files": len(manifest["files"]),
        "baseline": BASELINE,
        "source_changed_since_capture": changes,
        "source_index_matches": hashlib.sha256(index).hexdigest()
        == manifest["index_sha256"],
    }
    db = ROOT / ".runtime/platforms/desktop/data/business.sqlite3"
    with sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        result["generations"] = [
            dict(r)
            for r in conn.execute(
                "SELECT state,model,thinking,length(body) AS body_chars FROM generations"
            )
        ]
        result["actual_model_attempts"] = conn.execute(
            "SELECT count(*) FROM outbound_attempts WHERE actual_outbound=1"
        ).fetchone()[0]
        result["usage"] = [
            {
                key: json.loads(row[key]) if row[key] is not None else None
                for key in ["raw_usage", "field_presence", "normalized"]
            }
            for row in conn.execute(
                "SELECT raw_usage,field_presence,normalized FROM usage_records"
            )
        ]
        result["copies"] = [
            dict(row)
            for row in conn.execute(
                "SELECT asset_id,source_version,local_sha256 FROM integration_sources WHERE local_path IS NOT NULL"
            )
        ]
        result["tasks"] = [
            dict(row)
            for row in conn.execute(
                "SELECT kind,state FROM learning_preparations WHERE kind LIKE 'platform_%'"
            )
        ]
        result["native_reads"] = []
        for row in conn.execute("SELECT result FROM learning_native_query_receipts"):
            value = json.loads(row[0])
            result["native_reads"].append(
                {
                    key: value.get(key)
                    for key in [
                        "status",
                        "start_ms",
                        "end_ms",
                        "image_frames_requested",
                        "external_fallback",
                    ]
                }
            )
    frontend = ROOT / ".runtime/platforms/evidence/frontend-final-r2.json"
    if frontend.exists():
        tests = json.loads(frontend.read_text("utf-8"))
        result["frontend"] = {
            key: tests[key]
            for key in ["numTotalTests", "numPassedTests", "numFailedTests", "success"]
        }
    output = (
        ROOT / ".runtime/platforms/evidence" / f"delivery-audit-{time.time_ns()}.json"
    )
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
