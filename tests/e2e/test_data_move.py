"""Move a closed native execution home; the retained old path is unavailable."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from test_browser import (
    Browser,
    PYTHON,
    ROOT,
    check,
    http,
    port_closed,
    wait_until,
    write_json,
)
from test_data_transfer import source_digests as base_digests
from test_desktop import DesktopInstance, composer_fill
from test_desktop_migration import wait_api
from test_mvp_a import api as raw_api


def api(browser, path, body=None):
    value = raw_api(browser, path, body, method="GET" if body is None else "POST")
    if value["status"] != 200:
        raise RuntimeError(
            "DATA_MOVE_API_"
            + str(value["status"])
            + ":"
            + str(value["body"].get("code", "UNKNOWN"))
        )
    return value["body"]


def source_digests():
    return {
        **base_digests(),
        Path(__file__).relative_to(ROOT).as_posix(): hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
    }


async def serve(directory, port):
    import uvicorn
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.course_files import CourseFileStore
    from learning_app.data_location import ProfileLocation
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.storage import Repository

    initial = directory / "data"
    if not (directory / "seed.json").exists():
        repo = Repository(initial)
        materials = directory / "materials"
        materials.mkdir()
        (materials / "lesson.txt").write_text(
            "Self-created course; no private user material.", "utf-8"
        )
        course = CourseFileStore(repo).bind(None, str(materials), "迁移原生会话验证")[
            "course_id"
        ]
        write_json(directory / "seed.json", {"course_id": course})
        repo.close()
    active = ProfileLocation(initial).current()
    adapter = ExecutionAdapter(
        ROOT,
        active / "execution",
        mode="fixture",
        thinking="none",
        timeout=60,
        desktop_enabled=True,
    )
    await adapter.open()
    initial_requests = {row["request_id"] for row in adapter.boundary.records}
    app = create_app(
        AppConfig(
            mode="test",
            backend_port=port,
            frontend_port=port,
            data_dir=initial,
            runtime_dir=directory / "run",
            desktop_enabled=True,
            thinking="none",
            secret_file=directory / "NO_SECRETS",
        ),
        execution=adapter,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )

    def parent():
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        write_json(
            directory
            / ("provider-before.json" if active == initial else "provider-after.json"),
            {
                "count": len(adapter.boundary.records),
                "initial_request_ids": sorted(initial_requests),
                "request_ids": [row["request_id"] for row in adapter.boundary.records],
                "completed": all(
                    row["status"] == "completed" for row in adapter.boundary.records
                ),
            },
        )
        write_json(directory / "worker-cleanup.json", await adapter.close())
    return 0


class Instance(DesktopInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("a", encoding="utf-8")
        self.process = self.group.spawn(
            [
                str(PYTHON),
                str(Path(__file__).resolve()),
                "--serve-fixture",
                str(self.directory),
                "--backend-port",
                str(self.backend),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("MOVE_FIXTURE_START_FAILED")
            try:
                return http(self.url + "/api/v1/health")[2]["ready"]
            except OSError:
                return False

        wait_until(ready, timeout=60)
        return self


def ask(browser, chat, text, count):
    composer_fill(browser, text)
    browser.command(
        "run-code",
        "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
    )
    return wait_api(
        browser,
        "/chats/" + chat,
        f"value.generations.length==={count}&&value.generations.every(g=>g.state==='completed')",
    )


def run(directory, executable, port):
    from learning_app.data_location import ProfileLocation, inventory

    result = {
        "status": "running",
        "assertions": [],
        "layer": "actual native Harness + Chromium + closed directory move + actual saved Host journal",
        "cloud_provider_requests": 0,
        "source_before": source_digests(),
    }
    server_directory = directory / "server"
    instance = Instance(server_directory, port)
    browser = Browser(directory / "before-browser", executable)
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]:visible').waitFor();}",
        )
        course = json.loads((server_directory / "seed.json").read_text("utf-8"))[
            "course_id"
        ]
        mapping = api(browser, "/desktop/courses/" + course + "/open", {})
        chat = mapping["chat_id"]
        first = ask(browser, chat, "MOVE_FIRST_KNOWN_QUESTION", 1)
        check(
            first["generations"][0]["state"] == "completed",
            "one_original_native_turn_completed_before_data_move",
            result,
        )
        target_parent = directory / "另一个 数据磁盘"
        target_parent.mkdir()
        api(
            browser,
            "/data/transfers",
            {
                "operation_id": "move_native",
                "kind": "move",
                "parent": str(target_parent),
            },
        )
        wait_api(browser, "/data/operations/move_native", "value.state==='ready'")
        api(browser, "/data/operations/move_native/schedule", {})
        health = api(browser, "/health")
        api(
            browser,
            "/runtime/prepare-exit",
            {"instance_id": health["instance_id"], "confirm_active": True},
        )
        browser.close()
        result["before_cleanup"] = instance.stop()
        assert result["before_cleanup"]["backend_port_closed"]
        initial = server_directory / "data"
        location = ProfileLocation(initial)
        closed_inventory = inventory(initial)
        with location.lease():
            moved = location.apply_closed()
        check(
            moved["state"] == "completed"
            and [
                row
                for row in inventory(location.current())
                if row["path"] != "execution/workspace-relocations.json"
            ]
            == [
                row
                for row in closed_inventory
                if row["path"] != "execution/workspace-relocations.json"
            ],
            "closed_move_preserves_database_assets_and_native_execution_bytes",
            result,
        )
        retained = server_directory / "retained-old-data"
        assert (
            initial.resolve().parent == server_directory.resolve()
            and retained.resolve().parent == server_directory.resolve()
            and not retained.exists()
        )
        initial.rename(retained)
        check(
            not initial.exists(),
            "old_native_workspace_path_is_unavailable_but_original_bytes_retained",
            result,
        )
        instance = Instance(server_directory, port)
        instance.start()
        browser = Browser(directory / "after-browser", executable)
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('MOVE_FIRST_KNOWN_QUESTION',{exact:false}).last().waitFor();await page.getByText('本地测试回答。',{exact:false}).waitFor();}",
        )
        check(
            True,
            "actual_native_saved_turn_reopens_from_moved_home_without_old_path",
            result,
        )
        current = api(browser, "/desktop/chats/" + chat + "/open", {})
        check(
            current["session_id"] == mapping["session_id"]
            and current["epoch"] == mapping["epoch"],
            "same_machine_move_preserves_the_original_native_binding",
            result,
        )
        after = ask(browser, chat, "MOVE_SECOND_EXPLICIT_QUESTION", 2)
        check(
            after["generations"][0] == first["generations"][0],
            "prior_completed_generation_and_usage_stay_unchanged",
            result,
        )
        check(
            not initial.exists(),
            "new_turn_does_not_recreate_or_depend_on_old_software_directory",
            result,
        )
        browser.command(
            "screenshot", f"--filename={directory / 'moved-native-history.png'}"
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={directory / 'failure.png'}")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_after"] = source_digests()
        result["changed_files"] = [
            name
            for name in result["source_before"]
            if result["source_before"][name] != result["source_after"].get(name)
        ]
        if result["status"] == "passed":
            before = json.loads(
                (server_directory / "provider-before.json").read_text("utf-8")
            )
            after = json.loads(
                (server_directory / "provider-after.json").read_text("utf-8")
            )
            check(
                before["count"] == 1
                and after["count"] == 2
                and set(after["initial_request_ids"]) == set(before["request_ids"])
                and len(set(after["request_ids"]) - set(before["request_ids"])) == 1
                and before["completed"]
                and after["completed"],
                "one_local_request_on_each_side_of_move_no_replayed_first_question",
                result,
            )
        if result["changed_files"] or not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-fixture", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--backend-port", type=int, default=19407)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if args.serve_fixture:
        raise SystemExit(asyncio.run(serve(args.serve_fixture, args.backend_port)))
    if (
        not args.run_id
        or not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id)
        or not port_closed(args.backend_port)
    ):
        raise RuntimeError("OWNED_MOVE_TARGET_INVALID")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    value = run(directory, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "failure": value.get("failure"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
