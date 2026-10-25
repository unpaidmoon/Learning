"""After owned Host failure, reload still reads files/history without model input."""

import argparse
import asyncio
import hashlib
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
from test_data_transfer import source_digests as base_digests
from test_desktop import DesktopInstance, composer_fill
from test_desktop_migration import wait_api
from test_mvp_a import api as raw_api


def api(browser, path, body=None):
    result = raw_api(browser, path, body, method="GET" if body is None else "POST")
    if result["status"] != 200:
        raise RuntimeError("DEGRADATION_API_" + str(result["status"]))
    return result["body"]


def digests():
    values = base_digests()
    for path in [Path(__file__).resolve(), *ROOT.joinpath("frontend/dist").rglob("*")]:
        if path.is_file():
            values[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return values


async def serve(directory, port):
    import uvicorn
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.course_files import CourseFileStore
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.execution.native_tools import (
        NativeCallRegistry,
        NativeToolTransport,
    )
    from learning_app.storage import Repository
    from learning_app.workbench.native_business import NativeBusiness

    materials = directory / "materials"
    materials.mkdir()
    (materials / "lesson.txt").write_text("OWNED_FALLBACK_ORIGINAL_TEXT", "utf-8")
    repo = Repository(directory / "data")
    course = CourseFileStore(repo).bind(None, str(materials), "故障查看验证")[
        "course_id"
    ]
    write_json(directory / "seed.json", {"course_id": course})
    repo.close()
    adapters = []

    def factory(repository, config):
        adapter = ExecutionAdapter(
            ROOT,
            config.data_dir / "execution",
            mode="fixture",
            thinking="none",
            timeout=60,
            desktop_enabled=True,
            native_tools=NativeToolTransport(
                NativeCallRegistry(config.data_dir / "execution/native-calls.jsonl"),
                NativeBusiness(repository),
            ),
        )
        adapters.append(adapter)
        return adapter

    app = create_app(
        AppConfig(
            mode="start",
            backend_port=port,
            frontend_port=port,
            data_dir=directory / "data",
            runtime_dir=directory / "run",
            desktop_enabled=True,
            thinking="none",
            secret_file=directory / "NO_SECRETS",
        ),
        execution_factory=factory,
    )
    original_start = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def forced_unavailable(application):
        async with original_start(application):
            # Fixture forces exhausted recovery; production still permits two.
            application.state.execution_supervisor.restart_limit = 0
            yield

    app.router.lifespan_context = forced_unavailable
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    loop = asyncio.get_running_loop()

    def crash_owned():
        process = app.state.service.execution.host.process
        if process and process.poll() is None:
            process.kill()

    def parent():
        for line in sys.stdin:
            if line.strip() == "crash-host":
                loop.call_soon_threadsafe(crash_owned)
            if line.strip() == "shutdown":
                break
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        write_json(
            directory / "model-counts.json",
            {
                "boundary_requests": len(adapters[0].boundary.records),
                "actual_cloud": adapters[0].boundary.actual_outbound_attempts,
            },
        )
    return 0


class Instance(DesktopInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
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
                raise RuntimeError("DEGRADATION_FIXTURE_FAILED")
            try:
                return http(self.url + "/api/v1/health")[2]["ready"]
            except OSError:
                return False

        wait_until(ready, timeout=50)
        return self


def run(directory, executable, port):
    result = {
        "status": "running",
        "assertions": [],
        "source_before": digests(),
        "layer": "actual native Host and Chromium + forced exhausted Host recovery + read-only fallback",
        "cloud_provider_calls": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        Instance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        seed = json.loads((instance.directory / "seed.json").read_text("utf-8"))
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').waitFor();}",
        )
        mapping = api(browser, "/desktop/courses/" + seed["course_id"] + "/open", {})
        composer_fill(browser, "QUESTION_BEFORE_OWNED_HOST_CRASH")
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
        )
        saved = wait_api(
            browser,
            "/chats/" + mapping["chat_id"],
            "value.generations.length===1&&value.generations[0].state==='completed'",
        )
        check(True, "original_native_turn_saved_before_owned_failure", result)
        instance.process.stdin.write("crash-host\n")
        instance.process.stdin.flush()
        wait_api(browser, "/runtime", "value.execution_recovery.state==='unavailable'")
        browser.command(
            "run-code",
            "async(page)=>{await page.reload();await page.waitForURL('**/?read_only=1');await page.getByText(/当前仅查看资料与历史/).waitFor();}",
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',{name:'选择课程',exact:true}).selectOption("
            + json.dumps(seed["course_id"])
            + ");await page.getByText('QUESTION_BEFORE_OWNED_HOST_CRASH',{exact:false}).last().waitFor();await page.getByText('本地测试回答。',{exact:false}).waitFor();}",
        )
        check(
            True, "real_reload_reads_saved_conversation_after_host_unavailable", result
        )
        browser.command(
            "run-code",
            "async(page)=>{const tree=page.getByRole('tree',{name:'当前课程文件树'});await tree.getByRole('button',{name:'lesson.txt',exact:true}).click();await page.getByText('OWNED_FALLBACK_ORIGINAL_TEXT',{exact:false}).waitFor();if(!await page.getByRole('button',{name:'发送',exact:true}).isDisabled())throw Error('FALLBACK_SUBMIT_ENABLED');}",
        )
        check(
            True,
            "original_file_stays_readable_and_model_submission_is_disabled",
            result,
        )
        current = api(browser, "/chats/" + mapping["chat_id"])
        check(
            current["generations"] == saved["generations"],
            "saved_generation_not_replayed_or_rewritten_by_fallback",
            result,
        )
        browser.command(
            "screenshot", f"--filename={directory / 'degraded-read-view.png'}"
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
        counts = instance.directory / "model-counts.json"
        if counts.exists():
            observed = json.loads(counts.read_text("utf-8"))
            check(
                observed["boundary_requests"] == 1 and observed["actual_cloud"] == 0,
                "failure_and_reload_create_no_second_model_request",
                result,
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
    parser.add_argument("--serve-fixture", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--backend-port", type=int, default=19417)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if args.serve_fixture:
        raise SystemExit(asyncio.run(serve(args.serve_fixture, args.backend_port)))
    if not args.run_id or not port_closed(args.backend_port):
        raise RuntimeError("OWNED_DEGRADATION_TARGET_INVALID")
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
