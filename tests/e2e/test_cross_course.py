"""Native draft course choices -> foreign query -> original PDF, same owner chat."""

import argparse
import hashlib
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
    owner_root, other_root = directory / "owner-course", directory / "foreign-course"
    owner_root.mkdir()
    other_root.mkdir()
    (owner_root / "migration-read.txt").write_bytes(
        b"Owner course only. No matching page definition."
    )
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))["reading_pdf"](
        other_root / "chapter.pdf", page_count=3
    )
    original = hashlib.sha256((other_root / "chapter.pdf").read_bytes()).hexdigest()
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium + native Harness + explicit course draft scope + local SSE",
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("initial")
        owner, mapping = create_course(
            browser, instance, {"course_root": str(owner_root)}, result
        )
        created = api(
            browser,
            "/courses/folder",
            {"path": str(other_root), "title": "跨课对照教材"},
            method="POST",
        )
        check(
            created["status"] == 200,
            "foreign_course_is_bound_without_changing_owner_chat",
            result,
        )
        foreign = created["body"]["course_id"]
        wait_api(
            browser,
            f"/courses/{foreign}/learning/corpus",
            "value.documents.some(d=>d.relative_path==='chapter.pdf'&&d.state==='ready')",
            timeout_ms=60000,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'课程资料',exact:true}).click();const scope=page.getByRole('region',{name:'本对话资料读取范围'});await scope.getByRole('checkbox',{name:'跨课对照教材',exact:true}).check();await scope.getByRole('checkbox',{name:'合成桌面功能迁移课程（当前课程）',exact:true}).uncheck();}",
        )
        draft = wait_api(
            browser,
            f"/chats/{mapping['chat_id']}/draft",
            "value.read_course_ids?.length===1&&value.read_course_ids[0]==="
            + json.dumps(foreign),
        )
        check(
            draft["read_course_ids"] == [foreign],
            "native_scope_chooser_saves_exact_foreign_course_only",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('资料读取范围：跨课对照教材',{exact:true}).waitFor({state:'visible'});}",
        )
        check(True, "native_input_displays_active_explicit_course_scope", result)
        composer_fill(browser, "请依据明确指定课程的Learning page 3原文说明。")
        press_button(browser, "发送消息")
        wait_api(
            browser,
            f"/chats/{mapping['chat_id']}",
            "value.generations.length===1&&value.generations[0].state==='completed'",
            timeout_ms=45000,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('region',{name:'课程资料索引'}).getByRole('button',{name:'关闭',exact:true}).click();await page.getByRole('button',{name:'问答来源',exact:true}).click();await page.getByRole('region',{name:'问答资料来源'}).getByText('跨课对照教材 · chapter.pdf',{exact:true}).waitFor();}",
        )
        evidence = api(
            browser, f"/desktop/sessions/{mapping['session_id']}/learning-evidence"
        )["body"]
        card = next(
            item for item in evidence["items"] if "chapter.pdf" in item["label"]
        )
        check(
            evidence["course_id"] == owner
            and card["source"]["course_id"] == foreign
            and card["source"]["reference"]["page_index"] == 2,
            "foreign_source_keeps_origin_course_and_frozen_page",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('region',{name:'问答资料来源'}).getByRole('button',{name:'打开原文',exact:true}).click();await page.waitForFunction(()=>document.querySelector('iframe[data-learning-pdf-reader]')?.contentWindow?.PDFViewerApplication?.pdfViewer.currentPageNumber===3,{},{timeout:25000});}",
        )
        check(True, "verified_foreign_source_opens_its_original_pdf_page", result)
        check(
            api(browser, f"/chats/{mapping['chat_id']}")["body"]["course_id"] == owner
            and len(api(browser, f"/courses/{owner}/chats")["body"]) == 1
            and api(browser, f"/courses/{foreign}/chats")["body"] == [],
            "source_navigation_does_not_switch_or_create_owner_conversation",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'foreign-source-original-page.png'}",
            "--full-page",
        )
        press_button(browser, "恢复仅当前课程")
        wait_api(
            browser,
            f"/chats/{mapping['chat_id']}/draft",
            "value.read_course_ids?.length===0",
        )
        check(
            True,
            "restoring_default_scope_persists_without_resubmitting_old_question",
            result,
        )
        check(
            hashlib.sha256((other_root / "chapter.pdf").read_bytes()).hexdigest()
            == original,
            "foreign_original_is_unchanged",
            result,
        )
        result.update(status="passed", owner_course=owner, source_course=foreign)
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
    parser.add_argument("--backend-port", type=int, default=19265)
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
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    value = run_case(directory, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "evidence": str(directory / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
