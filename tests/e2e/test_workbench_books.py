"""Actual native book workflow, incremental reading, practice identity and history."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    original = materials / "migration-read.txt"
    original.write_text("先核对来源。函数y=a*x^2，a=1、x=1时y=1。", "utf-8")
    other = materials / "second.txt"
    other.write_text("串联与回顾：a=2、x=1时y=2。原始条件与AI参考评价分开。", "utf-8")
    originals = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, other)
    }
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium + fixed native Harness + formal book and practice stores + synthetic SSE",
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        (instance.directory / "hold-book-page").write_text(
            "owned synthetic gate", "utf-8"
        )
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("initial-native")
        course, _ = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        prefix = f"/courses/{course}/learning"
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('book');await form.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('course');await form.getByRole('textbox',{name:'标题',exact:true}).fill('逐章可读交互书');await form.getByRole('textbox',{name:'具体要求',exact:true}).fill('把指定资料组织成两章，提供正文、练习和参数图，目录确认后连续完成。');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].state==='awaiting_outline'",
            timeout_ms=40000,
        )
        task_id = tasks[0]["task_id"]
        coverage = api(browser, prefix + f"/tasks/{task_id}/coverage")["body"]
        check(
            not api(browser, prefix + "/artifacts")["body"]
            and sum(s["processed_leaf_count"] for s in coverage["sources"]) == 0,
            "outline_receipt_does_not_create_body_or_claim_source_coverage",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{const outline=page.getByRole('region',{name:'交互书目录确认',exact:true});await outline.getByRole('textbox',{name:'章节标题',exact:true}).first().fill('来源与核对');await outline.getByRole('button',{name:'确认目录并连续生成',exact:true}).click();}",
        )
        current = wait_api(
            browser,
            prefix + "/artifacts",
            "value.length===1&&value[0].kind==='book'",
            timeout_ms=40000,
        )[0]
        artifact_id, first_revision = (
            current["artifact_id"],
            current["head_revision_id"],
        )
        book_path = prefix + f"/artifacts/{artifact_id}/book"
        partial = wait_api(
            browser,
            book_path,
            "value.chapters[0].pages.length===1&&value.chapters[1].pages.length===0",
        )
        check(
            partial["chapters"][0]["state"] == "ready"
            and partial["task_state"] == "running",
            "first_chapter_is_readable_while_next_page_has_active_native_generation",
            result,
        )
        page = partial["chapters"][0]["pages"][0]["layout"]
        question_id, question_pin = next(iter(page["question_revisions"].items()))
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('region',{name:'交互书阅读',exact:true}).waitFor();await page.getByRole('textbox',{name:'实际作答',exact:true}).fill('我在第二章生成时核对并提交了原条件。');await page.getByRole('button',{name:'提交作答',exact:true}).click();await page.getByText('待判断 · 确定性比较',{exact:true}).waitFor();}",
        )
        answers = api(
            browser, prefix + f"/artifacts/{artifact_id}/answers?block_id={question_id}"
        )["body"]["answers"]
        check(
            len(answers) == 1
            and answers[0]["question_revision_id"] == question_pin == first_revision,
            "book_question_submits_to_existing_practice_store_with_its_true_revision",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'编辑此页',exact:true}).click();await page.getByRole('textbox',{name:'编辑内容块',exact:true}).first().fill('用户在第二章生成时修改了第一页。');await page.getByRole('button',{name:'保存修改',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();}",
        )
        (instance.directory / "release-book-page").write_text("release", "utf-8")
        wait_api(
            browser,
            prefix + f"/tasks/{task_id}",
            "value.state==='completed'",
            timeout_ms=40000,
        )
        full = wait_api(
            browser,
            book_path,
            "value.chapters.every(c=>c.pages.length===1&&c.state==='ready')",
        )
        full_revision = full["revision_id"]
        artifact = api(browser, prefix + f"/artifacts/{artifact_id}")["body"]
        check(
            artifact["content_role"] == "mixed"
            and any(
                b["markdown"] == "用户在第二章生成时修改了第一页。"
                for b in artifact["revision"]["body"]["blocks"]
            ),
            "next_page_commutes_with_the_user_edit_and_retains_one_book",
            result,
        )
        check(
            full["chapters"][0]["pages"][0]["layout"]["question_revisions"][question_id]
            == question_pin,
            "unchanged_question_keeps_its_answer_identity_when_the_book_head_advances",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'刷新章节',exact:true}).click();await page.getByRole('button',{name:/串联与回顾/}).click();await page.getByRole('heading',{name:'串联与回顾',exact:true}).waitFor();await page.getByRole('button',{name:/来源与核对/}).click();await page.getByText('我在第二章生成时核对并提交了原条件。',{exact:true}).waitFor();}",
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'incremental-book.png'}",
            "--full-page",
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'调整目录',exact:true}).click();const form=page.getByRole('region',{name:'调整书目录',exact:true});await form.getByRole('textbox',{name:'章节标题',exact:true}).first().fill('手动调整的来源章');await form.getByRole('button',{name:'上移章节',exact:true}).click();await form.getByRole('button',{name:'保存目录调整',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();}",
        )
        renamed = wait_api(
            browser,
            book_path,
            "value.chapters[0].chapter.title==='串联与回顾'&&value.chapters[1].chapter.title==='手动调整的来源章'",
        )
        check(
            renamed["revision_id"] != full_revision,
            "directory_order_and_labels_are_saved_as_an_artifact_revision",
            result,
        )
        old = api(browser, book_path + "?revision_id=" + full_revision)["body"]
        check(
            old["chapters"][0]["chapter"]["title"] == "来源与核对",
            "old_book_revision_keeps_its_original_directory",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'版本记录',exact:true}).click();const history=page.getByRole('complementary',{name:'版本记录',exact:true});await history.getByRole('button').last().click();const version=page.getByRole('complementary',{name:'历史版本内容',exact:true});await version.getByRole('region',{name:'交互书阅读',exact:true}).waitFor();await version.getByRole('button',{name:'采用此版本，保留历史',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();}",
        )
        restored = wait_api(
            browser,
            book_path,
            "value.chapters[0].pages.length===1&&value.chapters[1].pages.length===0",
        )
        check(
            restored["chapters"][0]["chapter"]["title"] == "来源与核对",
            "restoring_an_old_book_restores_its_layout_without_deleting_later_revisions",
            result,
        )
        browser.command("reload")
        browser.snapshot("book-reopened")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:/逐章可读交互书/}).click();await page.getByRole('region',{name:'交互书阅读',exact:true}).waitFor();}",
        )
        check(
            api(browser, book_path)["body"]["revision_id"] == restored["revision_id"]
            and len(api(browser, prefix + "/tasks")["body"]) == 1,
            "book_reopen_and_history_actions_do_not_restart_the_generation_task",
            result,
        )
        outline = api(browser, prefix + f"/tasks/{task_id}/book-outline")["body"]
        check(
            outline["confirmation_count"] == 1,
            "outline_is_confirmed_once_for_the_continuous_book",
            result,
        )
        check(
            all(
                hashlib.sha256((materials / name).read_bytes()).hexdigest() == digest
                for name, digest in originals.items()
            ),
            "synthetic_originals_remain_unchanged",
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
        if instance.directory.exists():
            (instance.directory / "release-book-page").write_text(
                "cleanup release", "utf-8"
            )
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
    parser.add_argument("--backend-port", type=int, default=19247)
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
    result = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": result["status"],
                "assertions": len(result["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
