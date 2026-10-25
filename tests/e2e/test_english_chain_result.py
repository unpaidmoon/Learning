"""Actual English chain outputs, preserved unknown round and offline recall."""

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
        raise RuntimeError("ENGLISH_CHAIN_RESULT_NOT_COMPLETE")
    assets = {value["artifact_id"]: value for value in prior["artifacts"]}
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
        "layer": "actual Native UI + saved English chain revisions + unknown history + offline recall",
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
            "reading_task_outputs_creates_one_normal_workspace_conversation",
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
            if artifact["kind"] == "translation":
                browser.command(
                    "run-code",
                    "async(page)=>{if(await page.getByRole('region',{name:'段落对照',exact:true}).count()!==2)throw Error('PAIRED_PARAGRAPHS_MISSING');}",
                )
                check(
                    True,
                    "actual_translation_shows_two_source_translation_pairs",
                    result,
                )
            if any(
                block["kind"] == "essay_draft"
                for block in artifact["revision"]["body"]["blocks"]
            ):
                rounds = api(
                    browser, f"/courses/{course}/learning/artifacts/{key}/essay-rounds"
                )["body"]["items"]
                identities = {row["round_number"]: row["round_id"] for row in rounds}
                check(
                    len(rounds) == 3
                    and next(row for row in rounds if row["round_number"] == 2)["state"]
                    == "unknown",
                    "three_rounds_keep_original_unknown_separate",
                    result,
                )
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('button',{name:'对照两轮',exact:true}).click();await page.getByRole('combobox',{name:/^左侧轮次/}).selectOption("
                    + json.dumps(identities[1])
                    + ");await page.getByRole('combobox',{name:/^右侧轮次/}).selectOption("
                    + json.dumps(identities[3])
                    + ");await page.getByRole('article',{name:'第1轮对照内容',exact:true}).getByText(/用户更正：63/).first().waitFor();const third=page.getByRole('article',{name:'第3轮对照内容',exact:true});await third.locator('pre').filter({hasText:'My friend enjoys short stories about nature'}).waitFor();}",
                )
                check(
                    True,
                    "completed_rounds_compare_actual_drafts_and_preserve_user_override",
                    result,
                )
                browser.command(
                    "screenshot",
                    f"--filename={directory / 'actual-essay-comparison.png'}",
                )
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('combobox',{name:/^右侧轮次/}).selectOption("
                    + json.dumps(identities[2])
                    + ");await page.getByRole('article',{name:'第2轮对照内容',exact:true}).getByText('此轮尚无可用完整批改，保留实际稿和任务状态。',{exact:true}).waitFor();}",
                )
                check(
                    True, "unknown_round_is_not_displayed_as_completed_feedback", result
                )
            destination = downloads / (key + ".zip")
            browser.command(
                "run-code",
                "async(page)=>{const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出离线包',exact:true}).click();const item=await pending;await item.saveAs("
                + json.dumps(str(destination))
                + ");}",
            )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'今日复习',exact:true}).click();const review=page.getByRole('region',{name:'今日复习',exact:true});await review.getByRole('combobox',{name:/^查看/}).selectOption('paused');await review.getByRole('navigation',{name:'复习列表',exact:true}).getByText(/已暂停/).waitFor();}",
        )
        check(True, "saved_pause_is_visible_without_resuming_the_card", result)
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
            if artifact["kind"] == "card_set":
                browser.command(
                    "run-code",
                    "async(page)=>{const cards=page.getByRole('region',{name:'离线回忆卡片',exact:true});if(await cards.count()!==2)throw Error('OFFLINE_CARD_COUNT');const card=cards.first();await card.getByRole('button',{name:'翻面核对',exact:true}).click();await card.getByRole('button',{name:'已回忆',exact:true}).click();await card.getByText('本次标为已回忆，不改变正式复习安排。',{exact:true}).waitFor();}",
                )
                check(
                    True,
                    "actual_offline_cards_flip_and_recall_without_formal_rating",
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
    parser.add_argument("--from-run", default="english-chain-real-20261024-r3")
    parser.add_argument("--backend-port", type=int, default=19363)
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
