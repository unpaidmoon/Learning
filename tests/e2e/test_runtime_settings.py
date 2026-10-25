"""Native resource settings persistence; idle power selection, no real model."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_data_transfer import source_digests as base_digests
from test_desktop import press_button
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_workspace import WorkspaceInstance


def digests():
    return {
        **base_digests(),
        Path(__file__).relative_to(ROOT).as_posix(): hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
    }


def run(directory, executable, port):
    materials = directory / "synthetic-materials"
    materials.mkdir()
    (materials / "migration-read.txt").write_text(
        "Self-created resource settings fixture.", "utf-8"
    )
    instance = WorkspaceInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    result = {
        "status": "running",
        "assertions": [],
        "source_before": digests(),
        "layer": "actual fixed native Harness + Chromium + durable local resource controls",
        "actual_cloud_calls": 0,
        "actual_local_model_calls": 0,
        "actual_system_sleep_test": "not_run",
        "actual_keep_awake_hold": "not_run; idle task state",
        "user_learning_experience": "not_run",
    }
    try:
        instance.directory.mkdir()
        (instance.directory / "requested-learning-fixture").write_text(
            "owned synthetic fixture", "utf-8"
        )
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        create_course(browser, instance, {"course_root": str(materials)}, result)
        press_button(browser, "设置")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('menuitem',{name:'运行设置',exact:true}).click();await page.getByRole('combobox',{name:'本地处理资源',exact:true}).waitFor();}",
        )
        initial = api(browser, "/runtime/preferences")["body"]
        updates = api(browser, "/runtime/updates")["body"]
        check(
            not updates["preferences"]["values"]["automatic"]
            and updates["preferences"]["values"]["feed_url"] is None
            and updates["last_check"] is None,
            "default_update_settings_have_no_source_check_or_automatic_request",
            result,
        )
        check(
            initial["effective"] == {"resource_mode": "gentle", "keep_awake": False}
            and initial["keep_awake"]["held"] is False,
            "native_runtime_settings_default_to_gentle_and_no_power_hold",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',{name:'本地处理资源',exact:true}).selectOption('accelerated');}",
        )
        saved = wait_api(
            browser,
            "/runtime/preferences",
            "value.record.revision===1&&value.effective.resource_mode==='accelerated'",
        )
        check(
            saved["resources"]["priority"] == "normal"
            and saved["resources"]["cpu_threads"] >= 2,
            "native_mode_selection_persists_actual_next_unit_resource_policy",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('checkbox',{name:'任务进行时保持系统唤醒',exact:true}).check();}",
        )
        selected = wait_api(
            browser,
            "/runtime/preferences",
            "value.record.revision===2&&value.effective.keep_awake",
        )
        check(
            selected["keep_awake"]["held"] is False,
            "enabling_option_while_idle_does_not_request_system_wake",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.reload();await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('menuitem',{name:'运行设置',exact:true}).click();await page.getByRole('combobox',{name:'本地处理资源',exact:true}).waitFor();if(await page.getByRole('combobox',{name:'本地处理资源',exact:true}).inputValue()!=='accelerated')throw Error('MODE_NOT_RESTORED');if(!await page.getByRole('checkbox',{name:'任务进行时保持系统唤醒',exact:true}).isChecked())throw Error('POWER_OPTION_NOT_RESTORED');}",
        )
        check(True, "runtime_preferences_restore_after_real_page_refresh", result)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('checkbox',{name:'任务进行时保持系统唤醒',exact:true}).uncheck();}",
        )
        wait_api(
            browser,
            "/runtime/preferences",
            "value.record.revision===3&&!value.effective.keep_awake",
        )
        press_button(browser, "暂停本地处理并释放资源")
        wait_api(
            browser,
            "/runtime/preferences",
            "!value.resources.local_processing&&!value.keep_awake.held",
        )
        check(True, "explicit_release_with_no_worker_keeps_idle_local_state", result)
        browser.command(
            "screenshot", f"--filename={directory / 'runtime-settings.png'}"
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={directory / 'failure.png'}")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        database = instance.directory / "data/business.sqlite3"
        if database.is_file():
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as connection:
                attempts = connection.execute(
                    "SELECT COUNT(*) FROM outbound_attempts"
                ).fetchone()[0]
            check(
                attempts == 0, "runtime_settings_do_not_submit_model_requests", result
            )
        result["source_after"] = digests()
        result["changed_files"] = [
            name
            for name in result["source_before"]
            if result["source_before"][name] != result["source_after"].get(name)
        ]
        if result["changed_files"] or not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19409)
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
        raise RuntimeError("OWNED_RUN_TARGET_INVALID")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    value = run(directory, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "error": value.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
