"""Native user note -> quoted block -> explicit edit -> task link and saved note."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop import composer_fill, press_button
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def source_digests():
    result = digests()
    for name in ("test_requested_learning.py", "test_workbench_workspace.py"):
        path = Path(__file__).with_name(name)
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    path = materials / "migration-read.txt"
    path.write_text("Synthetic original: verify conditions and units.", "utf-8")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": source_digests(),
        "layer": "actual Chromium + pinned Harness + formal task transactions + local SSE",
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    instance.directory.mkdir()
    (instance.directory / "requested-learning-fixture").write_text(
        "explicit synthetic fixture", "utf-8"
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
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();}",
        )
        press_button(browser, "新建笔记")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('textbox',{name:'笔记标题',exact:true}).fill('我的核对笔记');await page.getByRole('textbox',{name:'笔记正文',exact:true}).fill('用户原写：先核对来源。');await page.getByRole('button',{name:'保存笔记',exact:true}).click();}",
        )
        assets = wait_api(
            browser, f"/courses/{course}/learning/artifacts", "value.length===1"
        )
        manual = assets[0]
        check(
            manual["kind"] == "note"
            and manual["revision"]["author"] == "user"
            and manual["revision"]["content_role"] == "user_note"
            and manual["indexed"],
            "manual_note_is_saved_indexed_user_content_without_model",
            result,
        )
        press_button(browser, "引用到问题")
        draft = wait_api(
            browser,
            f"/chats/{mapping['chat_id']}/draft",
            "value.source_snapshot_ids?.length===1",
        )
        source = api(
            browser,
            f"/courses/{course}/learning/selections/{draft['source_snapshot_ids'][0]}",
        )["body"]
        check(
            source["reference"]["artifact_id"] == manual["artifact_id"]
            and source["reference"]["revision_id"] == manual["head_revision_id"]
            and source["reference"]["block_id"] == "note_1",
            "quote_pins_one_saved_block_and_revision_without_sending",
            result,
        )
        composer_fill(browser, "请修改这段笔记，加入一个核对条件的例子")
        press_button(browser, "发送消息")
        wait_api(
            browser,
            f"/courses/{course}/learning/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=45000,
        )
        edited = api(
            browser, f"/courses/{course}/learning/artifacts/{manual['artifact_id']}"
        )["body"]
        check(
            edited["head_revision_id"] != manual["head_revision_id"]
            and edited["revision"]["author"] == "model"
            and edited["revision"]["content_role"] == "mixed",
            "explicit_native_edit_updates_same_asset_and_preserves_mixed_identity",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('用户笔记的模型修订：先核对来源。例子：比较数值前先检查单位。',{exact:true}).waitFor({state:'visible'});}",
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'查看学习任务',exact:true}).last().click();}",
        )
        scene = browser.snapshot("task-from-chat-link")
        check(
            "生成工作" in scene
            or "处理范围" in scene
            or "任务详情" in scene
            or "任务进度" in scene,
            "chat_task_link_opens_bound_workbench_task",
            result,
        )
        composer_fill(browser, "请生成学习笔记，说明先核对资料来源")
        press_button(browser, "发送消息")
        wait_api(
            browser,
            f"/courses/{course}/learning/tasks",
            "value.length===2&&value.every(t=>t.state==='completed')",
            timeout_ms=45000,
        )
        assets = api(browser, f"/courses/{course}/learning/artifacts")["body"]
        check(
            len(assets) == 2 and len({item["artifact_id"] for item in assets}) == 2,
            "explicit_create_saves_one_independent_asset",
            result,
        )
        composer_fill(browser, "这是什么意思？")
        press_button(browser, "发送消息")
        wait_api(
            browser,
            f"/chats/{mapping['chat_id']}",
            "value.messages.some(m=>m.role==='assistant'&&m.text.includes('这是普通问答'))",
            timeout_ms=45000,
        )
        check(
            len(api(browser, f"/courses/{course}/learning/tasks")["body"]) == 2
            and len(api(browser, f"/courses/{course}/learning/artifacts")["body"]) == 2,
            "ordinary_qa_does_not_create_another_task_or_asset",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'explicit-task.png'}",
            "--full-page",
        )
        check(
            hashlib.sha256(path.read_bytes()).hexdigest() == original,
            "original_material_unchanged",
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
        result["source_digests_after"] = source_digests()
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
    parser.add_argument("--backend-port", type=int, default=19259)
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
