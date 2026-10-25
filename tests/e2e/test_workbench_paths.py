"""Actual native desktop route generation, free navigation and persisted editing."""

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
from test_workspace_v2 import manager_entry


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "migration-read.txt").write_text("Synthetic setup only.", "utf-8")
    original = materials / "lesson.txt"
    original.write_text(
        "先核对来源身份，再核对题目的实际条件。学习顺序可以调整。", "utf-8"
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium + fixed native Harness + formal path DB + local synthetic SSE",
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
        browser.snapshot("initial-native")
        course, _ = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        manager_entry(browser, "lesson.txt", double=True)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('先核对来源身份，再核对题目的实际条件。学习顺序可以调整。',{exact:true}).waitFor();await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('learning_path');await form.getByRole('textbox',{name:'标题',exact:true}).fill('可自由调整的合成路线');await form.getByRole('textbox',{name:'具体要求',exact:true}).fill('按目标建议学习顺序，允许自由跳过和查看讲解。');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        prefix = f"/courses/{course}/learning"
        tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=40000,
        )
        target = api(browser, prefix + f"/tasks/{tasks[0]['task_id']}/coverage")[
            "body"
        ]["target"]
        artifact_id = target["artifact_id"]
        path = prefix + f"/artifacts/{artifact_id}/path"
        initial = api(browser, path)["body"]
        first, second = [step["block"]["block_id"] for step in initial["steps"]]
        check(
            len(initial["steps"]) == 2
            and initial["revision"] == 0
            and all(
                not step["visited"] and step["review"]["scheduler_state"]["reps"] == 0
                for step in initial["steps"]
            ),
            "explicit_route_generation_saves_content_without_user_progress",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('region',{name:'建议学习路线',exact:true}).waitFor();await page.getByRole('button',{name:'暂时跳过',exact:true}).click();await page.getByText('已跳过，可随时返回',{exact:true}).waitFor();await page.getByRole('button',{name:/核对条件/}).click();await page.getByRole('heading',{name:'核对条件',exact:true}).waitFor();await page.getByRole('button',{name:'查看完整讲解',exact:true}).click();await page.getByText('核对条件的完整讲解，不能以跳过替代掌握。',{exact:true}).waitFor();await page.getByRole('button',{name:'自我感觉熟悉',exact:true}).click();await page.getByText(/用户自评：自我感觉熟悉/).waitFor();}",
        )
        active = wait_api(browser, path, "value.revision===4")
        check(
            active["current_block_id"] == second
            and active["steps"][0]["skipped"]
            and active["steps"][1]["self_assessment"] == "confident"
            and active["steps"][1]["answers"] == [],
            "skip_reveal_and_self_assessment_allow_next_point_without_fake_answers",
            result,
        )
        check(
            [s["review"]["scheduler_state"] for s in active["steps"]]
            == [s["review"]["scheduler_state"] for s in initial["steps"]],
            "browsing_does_not_rate_or_complete_knowledge_review",
            result,
        )
        browser.snapshot("route-before-reorder")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'编辑',exact:true}).click();await page.getByRole('spinbutton',{name:'建议顺序',exact:true}).first().fill('5');await page.getByRole('button',{name:'保存修改',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();await page.getByRole('region',{name:'建议学习路线',exact:true}).waitFor();}",
        )
        reordered = api(browser, path)["body"]
        check(
            [step["block"]["block_id"] for step in reordered["steps"]]
            == [second, first]
            and reordered["revision"] == 4,
            "manual_route_order_is_versioned_and_does_not_change_user_journal",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'学习记录',exact:true}).click();await page.getByText('已保存的学习操作',{exact:true}).waitFor();}",
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'learning-route.png'}",
            "--full-page",
        )
        browser.command("reload")
        browser.snapshot("after-reload")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:/可自由调整的合成路线/}).click();await page.getByRole('region',{name:'建议学习路线',exact:true}).waitFor();}",
        )
        check(
            api(browser, path)["body"] == reordered,
            "reload_restores_route_content_and_user_actions_without_new_dispatch",
            result,
        )
        events = api(browser, path + "/events")["body"]["events"]
        check(
            len(events) == 4
            and all(e["detail"]["payload"]["grants_mastery"] is False for e in events),
            "all_actions_remain_non_mastery_events",
            result,
        )
        check(
            len(api(browser, prefix + "/tasks")["body"]) == 1
            and len(api(browser, prefix + "/artifacts")["body"]) == 1,
            "route_navigation_and_reload_do_not_create_extra_products_or_tasks",
            result,
        )
        check(
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash,
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
    parser.add_argument("--backend-port", type=int, default=19245)
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
