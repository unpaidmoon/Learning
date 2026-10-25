"""Read repaired real outputs via Native UI, exercise workers, then offline ZIPs."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_mvp_a import api
from test_workbench_offline import unpack
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def run(directory, executable, port, from_run):
    prior_root = ROOT / ".runtime/validation" / from_run
    prior = json.loads((prior_root / "summary.json").read_text("utf-8"))
    if prior["status"] != "passed":
        raise RuntimeError("REPAIRED_RESULT_NOT_COMPLETE")
    assets = {key: value["artifact"] for key, value in prior["stages"].items()}
    course = next(iter(assets.values()))["course_id"]
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    shutil.copytree(prior_root / "data", instance.directory / "data")
    native_home = prior_root / "data/execution/host/home"
    if native_home.is_dir():
        shutil.copytree(native_home, instance.directory / "execution/host/home")
    result = {
        "status": "running",
        "source_before": digests(),
        "assertions": [],
        "new_actual_provider_calls": 0,
        "user_learning_experience": "not_run",
        "layer": "actual Native UI + actual repaired real assets + worker + backend-offline file packages",
    }
    stopped = False
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('textbox',{name:'输入问题',exact:true}).waitFor();await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();}",
        )
        check(
            len(api(browser, f"/courses/{course}/chats")["body"]) == 1,
            "opening_repaired_chain_preserves_one_business_conversation",
            result,
        )
        downloads = directory / "downloads"
        downloads.mkdir()
        for key, artifact in assets.items():
            browser.command(
                "run-code",
                "async(page)=>{await page.getByRole('navigation',{name:'学习成果列表',exact:true}).getByRole('button').filter({hasText:"
                + json.dumps(artifact["title"])
                + "}).click();await page.getByRole('heading',{name:"
                + json.dumps(artifact["title"])
                + ",exact:true}).waitFor();}",
            )
            check(True, "actual_repaired_asset_opens_" + key, result)
            if key == "explicit_replace_placeholder_plot":
                title = next(
                    block["visualization"]["presentation"]["title"]
                    for block in artifact["revision"]["body"]["blocks"]
                    if block["kind"] == "visualization"
                )
                browser.command(
                    "run-code",
                    "async(page)=>{const figure=page.getByRole('figure',{name:"
                    + json.dumps(title)
                    + ",exact:true});await figure.locator('svg[data-worker-frame]').waitFor();await figure.getByText(/f\\(1\\) = 0\\.328/).waitFor();await figure.getByRole('slider',{name:'参数 m',exact:true}).fill('2');await figure.getByText(/f\\(1\\) = 0\\.828/).waitFor();if(await figure.getByRole('button',{name:'播放动画',exact:true}).count())throw Error('UNREQUESTED_ANIMATION');}",
                )
                check(
                    True,
                    "real_output_sqrt_curve_and_parameter_use_actual_worker_values",
                    result,
                )
                browser.command(
                    "screenshot", f"--filename={directory / 'actual-curve.png'}"
                )
            elif key == "explicit_update_path_after_corrections":
                state = api(
                    browser,
                    f"/courses/{course}/learning/artifacts/{artifact['artifact_id']}/path",
                )["body"]
                check(
                    len(state["steps"]) == 3
                    and all(
                        not step["visited"]
                        and not step["answers"]
                        and step["self_assessment"] is None
                        for step in state["steps"]
                    ),
                    "repaired_three_step_route_contains_no_fabricated_user_progress",
                    result,
                )
            destination = downloads / (key + ".zip")
            browser.command(
                "run-code",
                "async(page)=>{const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出离线包',exact:true}).click();const item=await pending;await item.saveAs("
                + json.dumps(str(destination))
                + ");}",
            )
        result["cleanup"] = instance.stop()
        stopped = True
        check(
            port_closed(port),
            "backend_is_stopped_before_opening_real_chain_packages",
            result,
        )
        db = instance.directory / "data/business.sqlite3"
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        browser.command(
            "run-code",
            "async(page)=>{await page.context().setOffline(true);await page.goto('about:blank');await page.context().route('**/*',async(route)=>{if(/^https?:/.test(route.request().url()))await route.abort();else await route.continue();});}",
        )
        browser.command("requests", "--clear")
        for key, artifact in assets.items():
            target = directory / "unpacked" / key
            document = unpack(downloads / (key + ".zip"), target)
            browser.command(
                "run-code",
                "async(page)=>{await page.goto("
                + json.dumps((target / "index.html").resolve().as_uri())
                + ");await page.getByRole('heading',{name:"
                + json.dumps(artifact["title"])
                + ",exact:true}).waitFor();}",
            )
            check(
                document["kind"] == artifact["kind"],
                "real_saved_revision_opens_offline_" + key,
                result,
            )
            requests = browser.command("requests", "--static", "--filter=^https?://")
            check(
                not re.search(r"\bhttps?://", requests),
                "offline_package_has_no_http_request_" + key,
                result,
            )
            browser.command("requests", "--clear")
        # Calibrate after measuring the packages, so the deliberately aborted
        # request cannot arrive late and be attributed to an offline reader.
        browser.command(
            "run-code",
            "async(page)=>{await page.goto('https://offline-monitor.invalid/chain-calibration').catch(()=>null);}",
        )
        calibration = browser.command("requests", "--static", "--filter=^https?://")
        check(
            "chain-calibration" in calibration,
            "offline_network_observer_is_calibrated",
            result,
        )
        result["network_calibration"] = calibration
        check(
            hashlib.sha256(db.read_bytes()).hexdigest() == before,
            "offline_real_outputs_do_not_modify_application_records",
            result,
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        if not stopped:
            result["cleanup"] = instance.stop()
        result["source_after"] = digests()
        result["source_changed_during_run"] = (
            result["source_before"] != result["source_after"]
        )
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--from-run", default="deep-solution-repair-real-20261024-r1")
    parser.add_argument("--backend-port", type=int, default=19362)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not all(
        re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (args.run_id, args.from_run)
    ) or not port_closed(args.backend_port):
        raise RuntimeError("RUN_ID_INVALID_OR_PORT_IN_USE")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run(directory, args.browser_path, args.backend_port, args.from_run)
    print(
        json.dumps(
            {
                "status": result["status"],
                "assertions": len(result["assertions"]),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
