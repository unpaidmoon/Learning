"""Native PDF selection and whole text alignment; broken-tail preview and scope."""

import argparse
import hashlib
import json
import os
import re
import runpy
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance
from test_workspace_v2 import manager_entry


def frozen():
    result = digests()
    for name in ("test_aligned_translation.py", "test_workbench_workspace.py"):
        path = Path(__file__).with_name(name)
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def compose(browser, title):
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('aligned_translation');await form.getByRole('textbox',{name:'标题',exact:true}).fill("
        + json.dumps(title)
        + ");await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
    )


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))["reading_pdf"](
        materials / "reader.pdf", 3
    )
    text = (
        "Each paragraph keeps its exact source and order.\n\n" * 130
    ) + "Final source sentence."
    (materials / "english.txt").write_bytes(text.encode("utf-8"))
    (materials / "migration-read.txt").write_bytes(
        b"Shared synthetic course setup only."
    )
    originals = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in materials.iterdir()
    }
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": frozen(),
        "layer": "actual Chromium + pinned Host + paired translation source proofs + local SSE",
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
        course, mapping = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        manager_entry(browser, "reader.pdf", double=True)
        browser.command(
            "run-code",
            """async(page)=>{
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('.page[data-page-number="1"] .textLayer span').first().waitFor();
          await page.evaluate(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;
            const n=w.document.querySelector('.page[data-page-number="1"] .textLayer span').firstChild;
            const r=w.document.createRange();r.setStart(n,0);r.setEnd(n,8);w.getSelection().removeAllRanges();w.getSelection().addRange(r);});
          await page.getByRole('button',{name:'逐段对照',exact:true}).click();
          await page.getByText('段落对照已保存到当前课程',{exact:true}).waitFor({timeout:30000});
        }""",
        )
        prefix = f"/courses/{course}/learning"
        tasks = api(browser, prefix + "/tasks")["body"]
        first = tasks[0]
        artifact = api(
            browser, prefix + f"/artifacts/{first['units'][0]['result_artifact_id']}"
        )["body"]
        blocks = artifact["revision"]["body"]["blocks"]
        check(
            len(tasks) == 1
            and first["kind"] == "aligned_translation"
            and first["chat_id"] == mapping["chat_id"]
            and blocks[0]["translation"]["source_text"] == "Learning"
            and blocks[0]["kind"] == "translation",
            "pdf_action_saves_only_exact_selected_word_as_paired_translation",
            result,
        )
        check(
            not api(browser, f"/chats/{mapping['chat_id']}")["body"]["messages"]
            and not api(browser, f"/chats/{mapping['chat_id']}/draft")["body"][
                "source_snapshot_ids"
            ],
            "paired_translation_preserves_native_chat_and_draft",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'查看对照任务',exact:true}).click();await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('button',{name:'原文段落位置 1',exact:true}).click();await page.getByRole('button',{name:'回看原资料',exact:true}).click();await page.getByRole('tab',{name:'reader.pdf',exact:true}).waitFor();}",
        )
        check(
            len(api(browser, f"/courses/{course}/chats")["body"]) == 1,
            "paragraph_source_returns_to_same_versioned_pdf_and_same_chat",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'文件',exact:true}).click();}",
        )
        manager_entry(browser, "english.txt", double=True)
        compose(browser, "全文对照")
        all_tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.some(t=>t.instruction.includes('把明确指定')&&t.state==='completed')",
            timeout_ms=45000,
        )
        whole = next(
            task
            for task in all_tasks
            if task["kind"] == "aligned_translation"
            and task["task_id"] != first["task_id"]
        )
        saved = api(
            browser, prefix + f"/artifacts/{whole['units'][0]['result_artifact_id']}"
        )["body"]
        check(
            len(whole["units"]) > 1
            and "".join(
                block["translation"]["source_text"]
                for block in saved["revision"]["body"]["blocks"]
            )
            == text,
            "explicit_whole_document_text_is_covered_once_in_one_saved_translation",
            result,
        )
        coverage = api(browser, prefix + f"/tasks/{whole['task_id']}/coverage")["body"]
        check(
            coverage["sources"][0]["declaration"]["text_only"] is True,
            "whole_translation_pins_text_only_permission",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('region',{name:'段落对照',exact:true}).first().waitFor();}",
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'paired-translation.png'}",
            "--full-page",
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'english.txt',exact:true}).click();}",
        )
        (instance.directory / "aligned-broken-final").write_text(
            "explicit synthetic malformed tail", "utf-8"
        )
        compose(browser, "末段损坏对照")
        failed = wait_api(
            browser,
            prefix + "/tasks",
            "value.find(t=>!['"
            + first["task_id"]
            + "','"
            + whole["task_id"]
            + "'].includes(t.task_id))?.state==='failed'",
            timeout_ms=45000,
        )
        broken = next(
            task
            for task in failed
            if task["task_id"] not in {first["task_id"], whole["task_id"]}
        )
        check(
            broken["state"] == "failed"
            and any(
                unit["error_code"] == "TASK_TRANSLATION_OUTPUT_INVALID"
                for unit in broken["units"]
            )
            and broken["units"][0]["result_artifact_id"],
            "broken_final_keeps_saved_prefix_and_never_reports_complete",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('heading',{name:'处理失败',exact:true}).waitFor();await page.getByRole('button',{name:'查看本项译文草稿',exact:true}).last().click();await page.getByRole('heading',{name:'已收到的对照草稿',exact:true}).waitFor();}",
        )
        preview = browser.snapshot("broken-prefix-preview")
        check(
            "尚未证明完整覆盖" in preview and "译文草稿" in preview,
            "valid_prefix_preview_remains_explicitly_unverified",
            result,
        )
        raw = [
            json.loads(path.read_text("utf-8"))
            for path in (instance.directory / "execution/boundary/private").rglob(
                "*.request.json"
            )
        ]
        check(
            all(not request.get("tools") for request in raw),
            "native_translation_grants_no_read_write_or_external_tools",
            result,
        )
        check(
            all(
                hashlib.sha256((materials / name).read_bytes()).hexdigest() == digest
                for name, digest in originals.items()
            ),
            "original_documents_remain_unchanged",
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
        result["source_digests_after"] = frozen()
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
    parser.add_argument("--backend-port", type=int, default=19279)
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
    value = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
