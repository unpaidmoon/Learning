"""Actual native browser concept retrieval, source cards and original-page jumps."""

import argparse
import json
import os
import re
import runpy
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop import composer_fill, press_button
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "migration-read.txt").write_text("Synthetic folder setup only.", "utf-8")
    generate = runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))[
        "reading_pdf"
    ]
    generate(materials / "chapter.pdf", page_count=3)
    (materials / "private").mkdir()
    (materials / "private/hidden.md").write_text(
        "Learning page 3 EXCLUDED_CORPUS_SENTINEL", "utf-8"
    )
    (materials / "captions.srt").write_text(
        "Learning page 3 FORBIDDEN_GLOBAL_SUBTITLE", "utf-8"
    )
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium + fixed native Host + actual local index and PDFium + synthetic SSE",
        "actual_model_attempts": 0,
    }
    instance = WorkspaceInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("initial-native")
        course, mapping = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        prefix = f"/courses/{course}/learning"
        status = wait_api(
            browser,
            prefix + "/corpus",
            "value.documents.some(d=>d.relative_path==='chapter.pdf'&&d.state==='ready')",
            timeout_ms=60000,
        )
        check(
            any(
                doc["relative_path"] == "chapter.pdf" and doc["cursor"] == 3
                for doc in status["documents"]
            ),
            "automatic_document_page_checkpoints_ready_without_model",
            result,
        )
        browser.snapshot("before-corpus-policy")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'课程资料',exact:true}).click();const panel=page.getByRole('region',{name:'课程资料索引'});await panel.getByRole('textbox',{name:'索引排除项'}).fill('private');await panel.getByRole('button',{name:'保存排除项'}).click();await panel.getByRole('button',{name:'保存排除项'}).waitFor({state:'visible'});}",
        )
        status = wait_api(
            browser,
            prefix + "/corpus",
            "value.revision===1&&value.exclusions[0]==='private'",
        )
        check(
            status["exclusions"] == ["private"],
            "user_exclusion_policy_persists_before_query",
            result,
        )
        browser.snapshot("before-native-concept")
        question = "请按需查阅当前课程 Learning page 3 的原文，注明来源。"
        composer_fill(browser, question)
        press_button(browser, "发送消息")
        chat = wait_api(
            browser,
            f"/chats/{mapping['chat_id']}",
            "value.generations.length===1&&value.generations[0].state==='completed'",
            timeout_ms=40000,
        )
        check(
            chat["generations"][0]["body"] == "已核对当前课程第3页原文。",
            "native_skill_concept_read_continuation_completes",
            result,
        )
        browser.snapshot("before-source-cards")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('region',{name:'课程资料索引'}).getByRole('button',{name:'关闭',exact:true}).click();await page.getByRole('button',{name:'问答来源',exact:true}).click();await page.getByRole('region',{name:'问答资料来源'}).getByText('chapter.pdf',{exact:true}).waitFor();}",
        )
        evidence = api(
            browser, f"/desktop/sessions/{mapping['session_id']}/learning-evidence"
        )["body"]
        card = next(
            item for item in evidence["items"] if item["label"] == "chapter.pdf"
        )
        check(
            card["source"]["reference"]["page_index"] == 2
            and card["source"]["content_role"] == "original"
            and "SENTINEL" not in str(evidence)
            and "FORBIDDEN_GLOBAL_SUBTITLE" not in str(evidence),
            "source_card_uses_actual_page_version_role_and_excludes_ungranted_material",
            result,
        )
        browser.snapshot("before-explicit-source-jump")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('region',{name:'问答资料来源'}).getByRole('button',{name:'打开原文',exact:true}).click();await page.waitForFunction(()=>{const f=document.querySelector('iframe[data-learning-pdf-reader]');return f?.contentWindow?.PDFViewerApplication?.pdfViewer.currentPageNumber===3;},{timeout:25000});}",
        )
        check(True, "native_source_card_opens_original_pdf_page_three", result)
        browser.snapshot("before-manual-page-navigation")
        browser.command(
            "run-code",
            "async(page)=>{const reader=page.frameLocator('iframe[data-learning-pdf-reader]');await reader.locator('#pageNumber').fill('1');await reader.locator('#pageNumber').press('Enter');await page.waitForFunction(()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentPageNumber===1);await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('tab',{name:'chapter.pdf',exact:true}).click();await page.waitForTimeout(500);}",
        )
        page_number = browser.evaluate(
            "()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentPageNumber"
        )
        check(
            page_number == 1,
            "ordinary_tab_restore_does_not_repeat_old_explicit_jump",
            result,
        )
        browser.snapshot("before-second-explicit-source-jump")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('region',{name:'问答资料来源'}).getByRole('button',{name:'打开原文',exact:true}).click();await page.waitForFunction(()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentPageNumber===3);}",
        )
        check(True, "new_explicit_reference_click_can_jump_again", result)
        browser.snapshot("source-jump-complete")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'corpus-source-jump.png'}",
            "--full-page",
        )
        check(
            api(browser, prefix + "/artifacts")["body"] == [],
            "ordinary_concept_qa_does_not_create_learning_assets",
            result,
        )
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command(
                "screenshot",
                f"--filename={browser.directory / 'failure.png'}",
                "--full-page",
            )
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_digests_after"] = digests()
        result["source_changed_during_run"] = (
            result["source_digests_before"] != result["source_digests_after"]
        )
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19242)
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
        raise RuntimeError("RUN_ID_INVALID_OR_PORT_IN_USE")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    outcome = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": outcome["status"],
                "assertions": len(outcome["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if outcome["status"] == "passed" else 1)
