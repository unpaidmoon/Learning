"""Actual mixed-material chain assets, book navigation and offline self-tests."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from learning_app.storage.repository import _hash

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_mvp_a import api
from test_workbench_offline import unpack
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def run(directory, executable, port, from_run):
    prior_root = ROOT / ".runtime/validation" / from_run
    prior = json.loads((prior_root / "summary.json").read_text("utf-8"))
    if prior["status"] != "passed":
        raise RuntimeError("MATERIAL_CHAIN_RESULT_NOT_COMPLETE")
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
        "layer": "actual Native UI + mixed-material chain + book/offline questions/cards",
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
            "opening_material_chain_uses_one_normal_workspace_conversation",
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
            if artifact["kind"] == "book":
                browser.command(
                    "run-code",
                    "async(page)=>{const book=page.getByRole('region',{name:'交互书阅读',exact:true});await book.getByText('本书生成完成',{exact:true}).waitFor();const nav=book.getByRole('navigation',{name:'书目录',exact:true});await nav.getByRole('button').filter({hasText:'长度和面积单位'}).click();await book.getByRole('article',{name:'书页内容',exact:true}).getByRole('textbox',{name:'实际作答',exact:true}).first().waitFor();}",
                )
                check(
                    True,
                    "actual_two_chapter_book_navigation_keeps_completed_saved_pages",
                    result,
                )
                browser.command(
                    "screenshot", f"--filename={directory / 'actual-book.png'}"
                )
            elif artifact["kind"] == "question_set":
                question = next(
                    block
                    for block in artifact["revision"]["body"]["blocks"]
                    if block["kind"] == "question"
                )
                history = api(
                    browser,
                    f"/courses/{course}/learning/artifacts/{key}/answers?block_id={question['block_id']}&limit=20",
                )["body"]["answers"]
                check(
                    len(history) == 2
                    and history[0]["effective_assessment"]["author"] == "user_override"
                    and history[0]["effective_assessment"]["verdict"] == "correct"
                    and history[1]["effective_assessment"]["verdict"] == "wrong",
                    "actual_redo_and_original_reference_verdict_both_remain",
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
                document["kind"] == artifact["kind"]
                and document["revision_hash"] == _hash(artifact["revision"]),
                "real_saved_revision_opens_offline_" + key,
                result,
            )
            if artifact["kind"] == "book":
                browser.command(
                    "run-code",
                    "async(page)=>{const nav=page.getByRole('navigation',{name:'交互书目录',exact:true});if(await nav.getByRole('button').count()!==2)throw Error('BOOK_PAGES');await nav.getByRole('button').last().click();}",
                )
                check(
                    True,
                    "actual_offline_book_switches_between_both_saved_pages",
                    result,
                )
            if artifact["kind"] in {"book", "question_set"}:
                question = next(
                    block
                    for block in artifact["revision"]["body"]["blocks"]
                    if block["kind"] == "question"
                    and block["question"]["question_type"] == "single_choice"
                )
                answer_index = next(
                    index
                    for index, option in enumerate(question["question"]["options"])
                    if option["option_id"] == question["question"]["reference_answer"]
                )
                browser.command(
                    "run-code",
                    "async(page)=>{const quiz=page.getByRole('region',{name:'离线自测',exact:true}).filter({has:page.getByRole('radio')}).first();await quiz.getByRole('radio').nth("
                    + str(answer_index)
                    + ").check();await quiz.getByRole('button',{name:'对照参考答案',exact:true}).click();await quiz.getByRole('status').getByText('与参考选项一致。',{exact:true}).waitFor();}",
                )
                check(
                    True,
                    "actual_offline_reference_feedback_" + artifact["kind"],
                    result,
                )
            elif artifact["kind"] == "card_set":
                browser.command(
                    "run-code",
                    "async(page)=>{const cards=page.getByRole('region',{name:'离线回忆卡片',exact:true});if(await cards.count()!==2)throw Error('CARD_COUNT');const card=cards.first();await card.getByRole('button',{name:'翻面核对',exact:true}).click();await card.getByRole('button',{name:'已回忆',exact:true}).click();await card.getByText('本次标为已回忆，不改变正式复习安排。',{exact:true}).waitFor();}",
                )
                check(
                    True,
                    "actual_offline_material_cards_flip_without_formal_records",
                    result,
                )
            elif artifact["kind"] == "handout":
                browser.command(
                    "run-code",
                    "async(page)=>{const images=page.locator('img');if(await images.count()<2)throw Error('PDF_SOURCE_IMAGES_MISSING');await page.waitForFunction(()=>Array.from(document.images).every(img=>img.complete&&img.naturalWidth>0));}",
                )
                check(True, "actual_pdf_page_source_images_decode_offline", result)
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
    parser.add_argument("--from-run", default="material-chain-postprocess-20261025-r1")
    parser.add_argument("--backend-port", type=int, default=19364)
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
