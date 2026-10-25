"""Native long note quote -> bounded edits -> saved head, history and restore."""

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
from test_requested_learning import source_digests as base_digests
from test_workbench_workspace import WorkspaceInstance


def source_digests():
    values = base_digests()
    path = Path(__file__)
    values[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    return values


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    path = materials / "migration-read.txt"
    path.write_bytes(b"Self-created local course. No user material.")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    text = "".join(
        f"Paragraph {index:03d}: the old note keeps $8 / 2 = 4$ and symbol 🧠.\n\n"
        for index in range(420)
    )
    assert len(text) > 24_000
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": source_digests(),
        "layer": "actual Chromium + fixed native Harness + formal CP edit transactions + local SSE",
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
    }
    instance = WorkspaceInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
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
        prefix = f"/courses/{course}/learning"
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();}",
        )
        press_button(browser, "新建笔记")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('textbox',{name:'笔记标题',exact:true}).fill('长篇核对笔记');await page.getByRole('textbox',{name:'笔记正文',exact:true}).fill("
            + json.dumps(text)
            + ");await page.getByRole('button',{name:'保存笔记',exact:true}).click();}",
        )
        manual = wait_api(browser, prefix + "/artifacts", "value.length===1")[0]
        check(
            manual["revision"]["body"]["blocks"][0]["markdown"] == text
            and manual["revision"]["author"] == "user",
            "large_note_saved_exactly_as_user_content",
            result,
        )
        press_button(browser, "引用到问题")
        draft = wait_api(
            browser,
            f"/chats/{mapping['chat_id']}/draft",
            "value.source_snapshot_ids?.length===1",
        )
        check(
            len(draft["source_snapshot_ids"]) == 1,
            "large_target_quoted_without_model_or_second_asset",
            result,
        )
        composer_fill(
            browser, "请修改这段笔记，将 old 全部替换成 modern，其余文字保持。"
        )
        press_button(browser, "发送消息")
        task = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=90000,
        )[0]
        check(
            len(task["units"]) > 3
            and all(unit["state"] == "completed" for unit in task["units"]),
            "long_target_finished_as_disjoint_finite_work",
            result,
        )
        edited = api(browser, prefix + "/artifacts/" + manual["artifact_id"])["body"]
        expected = text.replace("old", "modern")
        check(
            edited["revision"]["body"]["blocks"][0]["markdown"] == expected
            and edited["revision"]["content_role"] == "mixed",
            "all_original_cp_ranges_preserve_characters_and_shifted_lengths",
            result,
        )
        check(
            len(api(browser, prefix + "/artifacts")["body"]) == 1,
            "all_ranges_update_one_existing_asset",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText(/^Paragraph 000: the modern note keeps/).first().waitFor({state:'visible',timeout:15000});}",
        )
        check(True, "visible_head_follows_completed_background_edits", result)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:/长篇核对笔记.*混合修订/}).waitFor({state:'visible',timeout:15000});}",
        )
        check(True, "asset_list_role_matches_background_mixed_revision", result)
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'long-edit-published.png'}",
            "--full-page",
        )
        history = api(
            browser, prefix + "/artifacts/" + manual["artifact_id"] + "/revisions"
        )["body"]
        check(
            len(history) == len(task["units"]) + 1
            and any(
                item["revision_id"] == manual["head_revision_id"] for item in history
            ),
            "each_publication_and_original_remain_in_history",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'查看学习任务',exact:true}).last().click();}",
        )
        check(
            "已完成" in browser.snapshot("completed-range-task"),
            "native_chat_link_opens_completed_bound_task",
            result,
        )
        restored = api(
            browser,
            prefix + "/artifacts/" + manual["artifact_id"] + "/restore",
            {
                "operation_id": "restore_original_long_note",
                "expected_revision_id": edited["head_revision_id"],
                "restore_revision_id": manual["head_revision_id"],
            },
            method="POST",
        )
        check(
            restored["status"] == 200
            and restored["body"]["artifact"]["revision"]["body"]["blocks"][0][
                "markdown"
            ]
            == text,
            "restore_creates_new_revision_and_preserves_original_content",
            result,
        )
        # Reopen the same course page; settled old work must not overwrite the restore.
        browser.command("reload")
        wait_api(browser, prefix + "/tasks", "value[0]?.state==='completed'")
        current = api(browser, prefix + "/artifacts/" + manual["artifact_id"])["body"]
        check(
            current["head_revision_id"]
            == restored["body"]["artifact"]["head_revision_id"],
            "reload_does_not_replay_settled_patches_over_restored_head",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'long-edit-restored.png'}",
            "--full-page",
        )
        check(
            hashlib.sha256(path.read_bytes()).hexdigest() == original,
            "original_course_file_unchanged",
            result,
        )
        result.update(
            status="passed", task=task, restored_revision_id=current["head_revision_id"]
        )
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
    parser.add_argument("--backend-port", type=int, default=19261)
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
