"""Native whole B selector and A/B same-name picks keep tasks/results under A."""

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


def frozen():
    value = digests()
    value[Path(__file__).relative_to(ROOT).as_posix()] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    return value


def run_case(directory, executable, port):
    owner_root, other_root = directory / "owner-course", directory / "foreign-course"
    owner_root.mkdir()
    other_root.mkdir()
    (owner_root / "migration-read.txt").write_text("Owner course.", "utf-8")
    (owner_root / "definition.md").write_text("A_FILE_SENTINEL", "utf-8")
    (other_root / "definition.md").write_text("B_FILE_SENTINEL", "utf-8")
    (other_root / "conditions.md").write_text("B_SECOND_FILE_SENTINEL", "utf-8")
    original = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in (owner_root, other_root)
        for path in root.iterdir()
    }
    result = {
        "status": "running",
        "assertions": [],
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
        "source_before": frozen(),
        "layer": "actual native Harness UI + fixed Host/local SSE + explicit task source origin",
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
        owner, mapping = create_course(
            browser, instance, {"course_root": str(owner_root)}, result
        )
        other = api(
            browser,
            "/courses/folder",
            {"path": str(other_root), "title": "明确整理课程B"},
            method="POST",
        )["body"]["course_id"]
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();await page.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('course');await page.getByRole('combobox',{name:'任务资料来源课程',exact:true}).selectOption("
            + json.dumps(other)
            + ");}",
        )
        check(
            api(browser, f"/courses/{owner}/learning/tasks")["body"] == [],
            "course_choice_alone_does_not_dispatch_or_create_a_task",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        tasks = wait_api(
            browser,
            f"/courses/{owner}/learning/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=60000,
        )
        task = tasks[0]
        coverage = api(
            browser, f"/courses/{owner}/learning/tasks/{task['task_id']}/coverage"
        )["body"]
        check(
            task["course_id"] == owner and len(coverage["leaves"]) == 2,
            "whole_foreign_course_creates_two_source_leaves_in_owner_task",
            result,
        )
        check(
            all(
                leaf["locator"]["source_course_id"] == other
                and "明确整理课程B" in leaf["label"]
                for leaf in coverage["leaves"]
            ),
            "all_frozen_leaves_display_their_actual_foreign_origin",
            result,
        )
        snapshots = [
            api(browser, f"/courses/{owner}/learning/selections/{identity}")["body"]
            for leaf in coverage["leaves"]
            for identity in leaf["snapshot_ids"]
        ]
        check(
            all(item["course_id"] == other for item in snapshots),
            "owner_access_is_exact_while_snapshots_remain_origin_b",
            result,
        )
        check(
            api(browser, f"/courses/{other}/learning/tasks")["body"] == []
            and api(browser, f"/courses/{other}/learning/artifacts")["body"] == [],
            "foreign_course_receives_no_task_or_result_write",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'整理资料',exact:true}).click();await page.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('custom');await page.getByRole('checkbox',{name:'definition.md',exact:true}).check();await page.getByRole('combobox',{name:'任务资料来源课程',exact:true}).selectOption("
            + json.dumps(other)
            + ");await page.getByRole('checkbox',{name:'definition.md',exact:true}).check();await page.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        both = wait_api(
            browser,
            f"/courses/{owner}/learning/tasks",
            "value.length===2&&value.every(t=>t.state==='completed')",
            timeout_ms=60000,
        )
        custom = next(item for item in both if item["task_id"] != task["task_id"])
        mixed = api(
            browser, f"/courses/{owner}/learning/tasks/{custom['task_id']}/coverage"
        )["body"]
        origins = {
            source["declaration"].get("source_course_id", owner)
            for source in mixed["sources"]
        }
        check(
            origins == {owner, other} and len(mixed["leaves"]) == 2,
            "same_named_files_from_a_and_b_are_two_explicit_independent_declarations",
            result,
        )
        check(
            len(api(browser, f"/courses/{owner}/learning/artifacts")["body"]) == 2
            and api(browser, f"/courses/{other}/chats")["body"] == [],
            "both_results_stay_in_a_without_creating_b_conversations",
            result,
        )
        check(
            all(
                hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
                for path, sha in original.items()
            ),
            "all_original_fixture_files_remain_unchanged",
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
        result["cleanup"] = instance.stop()
        result["source_after"] = frozen()
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
    parser.add_argument("--backend-port", type=int, default=19361)
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
    result = run_case(directory, args.browser_path, args.backend_port)
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
