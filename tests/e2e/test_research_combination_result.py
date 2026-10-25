"""Read/export the two actual research-chain reports without new model requests."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_material_chain_result import run as read_and_export


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def run(directory, executable, port):
    first_root = ROOT / ".runtime/validation/research-combination-real-20261025-r1"
    corrected_root = (
        ROOT / ".runtime/validation/research-combination-correction-real-20261025-r1"
    )
    first = json.loads((first_root / "summary.json").read_text("utf-8"))
    corrected = json.loads((corrected_root / "summary.json").read_text("utf-8"))
    if first["status"] != "passed" or corrected["status"] != "passed":
        raise RuntimeError("ACTUAL_RESEARCH_CHAIN_NOT_READY")
    originals = {
        str(root): hashes(root / "data") for root in [first_root, corrected_root]
    }
    # A fresh owned input adapts the shared artifact-reader runner; the frozen
    # real runs and their summaries never receive derived fields or DB writes.
    derived_name = directory.name + "-owned-input"
    derived = ROOT / ".runtime/validation" / derived_name
    derived.mkdir(exist_ok=False)
    shutil.copytree(corrected_root / "data", derived / "data")
    assets = [case["artifact"] for case in first["cases"]]
    write_json(derived / "summary.json", {"status": "passed", "artifacts": assets})
    result = read_and_export(directory, executable, port, derived_name)
    result["layer"] = (
        "actual Native UI and downloaded offline ZIPs of the two real research-chain reports"
    )
    try:
        for case in first["cases"]:
            artifact = case["artifact"]
            page = directory / "unpacked" / artifact["artifact_id"] / "index.html"
            match = re.search(
                r'<script type="application/json" id="learning-offline-data">(.*?)</script>',
                page.read_text("utf-8"),
                re.S,
            )
            document = json.loads(match.group(1))
            citations = {
                identity
                for block in artifact["revision"]["body"]["blocks"]
                for identity in block.get("source_snapshot_ids", [])
            }
            check(
                document["role"] == "ai_generated"
                and len(document["sources"]) == len(citations)
                and all(source["text"] for source in document["sources"]),
                "actual_report_keeps_ai_identity_and_all_cited_source_texts_"
                + case["kind"],
                result,
            )
            if case["kind"] == "external_nist":
                sources = json.dumps(document["sources"], ensure_ascii=False)
                check(
                    "https://www.nist.gov/pml/owm/si-units-length" in sources
                    and "https://www.nist.gov/pml/owm/metric-si/si-units-area"
                    in sources,
                    "downloaded_official_comparison_preserves_both_exact_nist_source_urls",
                    result,
                )
        native_ledger = directory / "server/execution/boundary/boundary-ledger.jsonl"
        requests = (
            [json.loads(line) for line in native_ledger.read_text("utf-8").splitlines()]
            if native_ledger.exists()
            else []
        )
        check(
            not any(
                row.get("event") in {"request_received", "dispatch_started"}
                for row in requests
            ),
            "reading_and_exporting_actual_reports_creates_zero_new_model_requests",
            result,
        )
        check(
            all(
                hashes(Path(root) / "data") == before
                for root, before in originals.items()
            ),
            "both_frozen_real_runs_and_their_original_histories_remain_unchanged",
            result,
        )
        if result["status"] == "passed":
            browser = Browser(directory / "offline-preview-browser", executable)
            try:
                artifact = next(
                    case["artifact"]
                    for case in first["cases"]
                    if case["kind"] == "external_nist"
                )
                path = directory / "unpacked" / artifact["artifact_id"] / "index.html"
                browser.command("open", "about:blank", f"--config={browser.config}")
                browser.command(
                    "run-code",
                    "async(page)=>{await page.context().setOffline(true);await page.goto("
                    + json.dumps(path.resolve().as_uri())
                    + ");await page.getByRole('heading',{name:"
                    + json.dumps(artifact["title"])
                    + ",exact:true}).waitFor();}",
                )
                browser.command(
                    "screenshot",
                    f"--filename={directory / 'actual-nist-report-offline.png'}",
                )
            finally:
                result["preview_cleanup"] = browser.close()
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19425)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or not port_closed(
        args.backend_port
    ):
        raise RuntimeError("OWNED_REPORT_TARGET_INVALID")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    record = run(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": record["status"],
                "assertions": len(record["assertions"]),
                "error": record.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if record["status"] == "passed" else 1)
