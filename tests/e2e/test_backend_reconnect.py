"""Outer backend crash: actual pinned Host, same mounted UI and no write replay."""

import argparse
import asyncio
import json
import os
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
from test_data_transfer import source_digests
from test_desktop import DesktopInstance, composer_fill
from test_desktop_migration import wait_api
from test_execution_degradation import api


async def serve():
    import uvicorn

    from learning_app.app import create_app
    from learning_app.config import load_config
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.execution.native_tools import (
        NativeCallRegistry,
        NativeToolTransport,
    )
    from learning_app.workbench.native_business import NativeBusiness

    config = load_config({"desktop_enabled": True, "thinking": "none"})

    def factory(repository, configured):
        return ExecutionAdapter(
            ROOT,
            configured.data_dir / "execution",
            mode="fixture",
            thinking="none",
            desktop_enabled=True,
            timeout=90,
            native_tools=NativeToolTransport(
                NativeCallRegistry(
                    configured.data_dir / "execution/native-calls.jsonl"
                ),
                NativeBusiness(repository),
            ),
        )

    app = create_app(config, execution_factory=factory)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config.backend_port,
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
    await server.serve()
    return 0 if server.started else 1


def launch(directory, port):
    import learning_app.dev as dev
    from learning_app.processes import OwnedProcessGroup

    children = []

    class Observed(OwnedProcessGroup):
        def spawn(self, args, **kwargs):
            is_backend = "-m" in args and "learning_app" in args
            if is_backend:
                args = [str(PYTHON), str(Path(__file__).resolve()), "--serve-fixture"]
            child = super().spawn(args, **kwargs)
            if is_backend:
                children.append(child)
            return child

    def controls():
        for line in sys.stdin:
            if line.strip() == "crash-backend":
                # Only a live Popen started by this owner is authority to kill.
                if children and children[-1].poll() is None:
                    children[-1].kill()

    threading.Thread(target=controls, daemon=True).start()
    dev.OwnedProcessGroup = Observed
    return dev.run(
        ["start", "--harness", "--backend-port", str(port), "--timeout", "45"]
    )


class Instance(DesktopInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.log = (self.directory / "launcher.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [
                str(PYTHON),
                str(Path(__file__).resolve()),
                "--launch-fixture",
                str(self.directory),
                "--backend-port",
                str(self.backend),
            ],
            cwd=ROOT,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "LEARNING_APP_DATA_DIR": str(self.directory / "data"),
                "LEARNING_APP_RUNTIME_DIR": str(self.directory / "run"),
                "LEARNING_APP_DESKTOP_ENABLED": "true",
                "LEARNING_APP_SECRET_FILE": str(self.directory / "NO_SECRETS"),
            },
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("OWNED_BACKEND_LAUNCH_FAILED")
            try:
                return (
                    http(self.url + "/api/v1/health")[2]["capabilities"][
                        "question_answering"
                    ]
                    == "ready"
                )
            except (OSError, TypeError):
                return False

        wait_until(ready, timeout=60)
        return self

    def stop(self):
        if self.process and self.process.poll() is None:
            from learning_app.dev import _request_owned_exit

            records = [
                json.loads(p.read_text("utf-8"))
                for p in (self.directory / "run/apps").glob("*/run.json")
            ]
            record = next((row for row in records if row["status"] == "ready"), None)
            if record:
                _request_owned_exit(record, confirm_active=True, timeout=10)
                self.process.wait(timeout=35)
        cleanup = self.group.close()
        if self.log:
            self.log.close()
        return {"group": cleanup, "backend_port_closed": port_closed(self.backend)}


def ledger(directory):
    path = directory / "data/execution/boundary/boundary-ledger.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    return {
        "request_ids": sorted(
            {row["request_id"] for row in rows if row["event"] == "request_received"}
        ),
        "actual_cloud_attempts": sum(
            row["event"] == "dispatch_started" for row in rows
        ),
    }


def run(directory, executable, port):
    from learning_app.course_files import CourseFileStore
    from learning_app.storage import Repository

    materials = directory / "materials"
    materials.mkdir()
    (materials / "lesson.txt").write_text("OWNED_BACKEND_RECOVERY_SOURCE", "utf-8")
    result = {
        "status": "running",
        "assertions": [],
        "source_before": source_digests(),
        "layer": "actual outer launcher + fixed native Host + Chromium; local synthetic model only",
        "actual_cloud_calls": 0,
        "user_learning_experience": "not_run",
    }
    instance = Instance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        repo = Repository(instance.directory / "data")
        try:
            course = CourseFileStore(repo).bind(None, str(materials), "后台恢复验证")[
                "course_id"
            ]
        finally:
            repo.close()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').waitFor();}",
        )
        mapping = api(browser, "/desktop/courses/" + course + "/open", {})
        composer_fill(browser, "ORIGINAL_COMPLETED_TURN_BEFORE_BACKEND_CRASH")
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
        )
        before = wait_api(
            browser,
            "/chats/" + mapping["chat_id"],
            "value.generations.length===1&&value.generations[0].state==='completed'",
        )
        first_health = api(browser, "/health")
        first_ledger = ledger(instance.directory)
        check(
            len(first_ledger["request_ids"]) == 1,
            "one_original_local_request_before_crash",
            result,
        )
        draft = "UNSENT_DRAFT_MUST_SURVIVE_后台恢复"
        composer_fill(browser, draft)
        browser.command(
            "run-code",
            "async(page)=>{await page.evaluate(()=>{window.__ownedMountedMarker='same-document';window.__ownedRecoverySessions=0;});page.on('response',async response=>{if(response.url().endsWith('/api/v1/session')&&response.status()===200)await page.evaluate(()=>{window.__ownedRecoverySessions++;}).catch(()=>{});});}",
        )
        instance.process.stdin.write("crash-backend\n")
        instance.process.stdin.flush()

        def recovered():
            try:
                health = http(instance.url + "/api/v1/health")[2]
                return (
                    health
                    and health["instance_id"] != first_health["instance_id"]
                    and health["capabilities"]["question_answering"] == "ready"
                )
            except OSError:
                return False

        wait_until(recovered, timeout=65)
        browser.command(
            "run-code",
            "async(page)=>{await page.waitForFunction(()=>window.__ownedRecoverySessions>=3,{},{timeout:25000});if(await page.evaluate(()=>window.__ownedMountedMarker)!=='same-document')throw Error('DOCUMENT_RELOADED');if((await page.locator('[data-composer-input]').innerText()).trim()!=="
            + json.dumps(draft)
            + ")throw Error('DRAFT_CHANGED');}",
        )
        check(
            True,
            "new_backend_session_refreshed_without_reload_and_unsent_draft_preserved",
            result,
        )
        after = api(browser, "/chats/" + mapping["chat_id"])
        check(
            after["generations"] == before["generations"]
            and ledger(instance.directory) == first_ledger,
            "recovery_preserves_original_generation_and_never_replays_model_request",
            result,
        )
        composer_fill(browser, "EXPLICIT_NEW_TURN_AFTER_BACKEND_RECOVERY")
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
        )
        continued = wait_api(
            browser,
            "/chats/" + mapping["chat_id"],
            "value.generations.length===2&&value.generations[1].state==='completed'",
        )
        check(
            continued["generations"][0] == before["generations"][0],
            "explicit_new_turn_uses_refreshed_csrf_and_keeps_original_turn",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('本地测试回答。',{exact:false}).last().waitFor();}",
        )
        browser.command(
            "screenshot", f"--filename={directory / 'recovered-native.png'}"
        )
        check(
            len(ledger(instance.directory)["request_ids"]) == 2
            and ledger(instance.directory)["actual_cloud_attempts"] == 0,
            "exactly_two_explicit_local_turns_and_zero_cloud_requests",
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
        records = [
            json.loads(p.read_text("utf-8"))
            for p in (instance.directory / "run/apps").glob("*/run.json")
        ]
        result["instances"] = [
            {
                key: row.get(key)
                for key in ("instance_id", "recovery_parent", "status", "cleanup")
            }
            for row in records
        ]
        if not result["cleanup"]["backend_port_closed"] or any(
            row["cleanup"].get("remaining_pids") for row in records
        ):
            result["status"] = "failed"
        result["source_after"] = source_digests()
        result["changed_files"] = [
            key
            for key, value in result["source_before"].items()
            if result["source_after"].get(key) != value
        ]
        if result["changed_files"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-fixture", action="store_true")
    parser.add_argument("--launch-fixture", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--backend-port", type=int, default=19423)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if args.serve_fixture:
        raise SystemExit(asyncio.run(serve()))
    if args.launch_fixture:
        raise SystemExit(launch(args.launch_fixture, args.backend_port))
    if not args.run_id or not port_closed(args.backend_port):
        raise RuntimeError("OWNED_RECONNECT_TARGET_INVALID")
    target = ROOT / "output/playwright" / args.run_id
    target.mkdir(parents=True, exist_ok=False)
    observed = run(target, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": observed["status"],
                "assertions": len(observed["assertions"]),
                "error": observed.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if observed["status"] == "passed" else 1)
