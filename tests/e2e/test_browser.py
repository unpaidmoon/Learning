"""Real Chromium acceptance through the project's fixed Playwright CLI.

Artifacts contain synthetic data only and stay in ignored output/playwright.
The optional Host fixture uses formal application modules and never loads a key.
No @playwright/test runner, research probe or arbitrary video/screen is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.server as http_server
import json
import os
from pathlib import Path
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
PYTHON = ROOT / "backend/.venv/Scripts/python.exe"
NODE = ROOT / ".tools/node/node.exe"
CLI = ROOT / "node_modules/@playwright/cli/playwright-cli.js"
OPENER = build_opener(ProxyHandler({}))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check(value: object, name: str, result: dict, **observed: object) -> None:
    result.setdefault("assertions", []).append(
        {"name": name, "passed": bool(value), **observed}
    )
    if not value:
        raise AssertionError(name)


def http(
    url: str, *, method: str = "GET", headers: dict | None = None, data=None
) -> tuple:
    request = Request(url, headers=headers or {}, method=method, data=data)
    try:
        response = OPENER.open(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = None
        return response.status, dict(response.headers), body


def port_closed(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        return connection.connect_ex(("127.0.0.1", port)) != 0


def wait_until(predicate, *, timeout: float = 15) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.05)
    raise TimeoutError("确定条件未在期限内出现")


class Instance:
    """Manage only an instance this test started and its instance-bound stop mailbox."""

    def __init__(self, directory: Path, mode: str, backend: int, frontend: int):
        self.directory, self.mode = directory, mode
        self.backend, self.frontend = backend, frontend
        self.env = {
            **os.environ,
            "LEARNING_APP_DATA_DIR": str(directory / "data"),
            "LEARNING_APP_RUNTIME_DIR": str(directory / "runtime"),
            "LEARNING_APP_HARNESS_ENABLED": "false",
            "PYTHONIOENCODING": "utf-8",
        }
        self.process = None
        self.record = {}

    def start(self) -> Instance:
        self.directory.mkdir(parents=True, exist_ok=True)
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        self.process = subprocess.Popen(
            [
                str(PYTHON),
                str(ROOT / "scripts/app.py"),
                self.mode,
                "--backend-port",
                str(self.backend),
                "--frontend-port",
                str(self.frontend),
            ],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            **options,
        )
        lines: queue.Queue[str] = queue.Queue()

        def capture():
            with (self.directory / "launcher.stdout.txt").open(
                "w", encoding="utf-8"
            ) as log:
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    lines.put(line)

        threading.Thread(target=capture, daemon=True).start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(f"应用启动失败，见 {self.directory}")
                continue
            if line.startswith("LEARNING_APP_READY "):
                self.ready = json.loads(line.split(" ", 1)[1])
                self.url = self.ready["url"]
                self.record_path = (
                    self.directory
                    / "runtime/apps"
                    / self.ready["instance_id"]
                    / "run.json"
                )
                self.record = json.loads(self.record_path.read_text("utf-8"))
                status, _, health = http(
                    f"http://127.0.0.1:{self.backend}/api/v1/health"
                )
                if status != 200 or health["instance_id"] != self.ready["instance_id"]:
                    raise AssertionError("就绪事件与真实后台身份不一致")
                return self
        raise TimeoutError("应用就绪事件超时")

    def stop(self) -> dict:
        if self.process is None:
            return {}
        if self.process.poll() is None:
            if hasattr(self, "ready"):
                options = (
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                )
                subprocess.run(
                    [
                        str(PYTHON),
                        str(ROOT / "scripts/app.py"),
                        "stop",
                        "--instance-id",
                        self.ready["instance_id"],
                    ],
                    cwd=ROOT,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=True,
                    **options,
                )
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # This handle belongs to the launcher created above; closing its
                # private Job bounds descendants without discovering other PIDs.
                self.process.kill()
                self.process.wait(timeout=10)
                raise AssertionError("所属launcher未能按实例请求退出")
        if hasattr(self, "record_path"):
            self.record = json.loads(self.record_path.read_text("utf-8"))
        return {
            "exit_code": self.process.returncode,
            "record": self.record,
            "backend_port_closed": port_closed(self.backend),
            "frontend_port_closed": port_closed(self.frontend),
        }


class Browser:
    def __init__(self, directory: Path, executable: Path):
        from learning_app.processes import OwnedProcessGroup

        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.session = "learning-" + uuid.uuid4().hex[:12]
        self.group = OwnedProcessGroup(self.session)
        self.steps = []
        self.config = directory / "cli.config.json"
        write_json(
            self.config,
            {
                "browser": {
                    "browserName": "chromium",
                    "isolated": True,
                    "launchOptions": {
                        "headless": True,
                        "executablePath": str(executable),
                    },
                    "contextOptions": {"viewport": {"width": 1440, "height": 1000}},
                }
            },
        )

    def command(self, *arguments: str, raw: bool = False) -> str:
        argv = [str(NODE), str(CLI), f"-s={self.session}"]
        if raw:
            argv.append("--raw")
        argv.extend(arguments)
        process = self.group.spawn(argv, cwd=self.directory, env=os.environ)
        stdout, stderr = process.communicate(timeout=45)
        self.steps.append(
            {
                "command": list(arguments),
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        write_json(self.directory / "cli-transcript.json", self.steps)
        if process.returncode or "### Error" in stdout:
            raise RuntimeError(
                f"Playwright CLI失败：{arguments[0]}，见 {self.directory}"
            )
        return stdout.strip()

    def evaluate(self, expression: str):
        output = self.command("eval", expression, raw=True)
        return json.loads(output)

    def snapshot(self, label: str) -> str:
        result = self.command("snapshot")
        inline = re.search(r"```yaml\n(.*?)\n```", result, re.DOTALL)
        if inline:
            snapshot = inline[1]
        else:
            paths = re.findall(r"\(([^\n]*?\.yml)\)", result)
            if not paths:
                raise AssertionError("CLI未产生可检查快照")
            snapshot = (self.directory / paths[-1]).resolve().read_text("utf-8")
        (self.directory / f"{label}.yml").write_text(snapshot, encoding="utf-8")
        return snapshot

    def ref(self, snapshot: str, pattern: str) -> str:
        for line in snapshot.splitlines():
            if pattern in line:
                match = re.search(r"\[ref=([A-Za-z0-9_-]+)\]", line)
                if match:
                    return match[1]
        raise AssertionError(f"快照未找到元素：{pattern}")

    def wait_text(self, text: str) -> None:
        # This is an event/state condition, not a sleep used to infer a fault window.
        self.command(
            "run-code",
            f"async (page) => {{ await page.getByText({json.dumps(text)}, "
            "{exact:true}).waitFor({state:'visible',timeout:20000}); }",
        )

    def close(self) -> dict:
        try:
            self.command("close")
        finally:
            return self.group.close()


class FixtureInstance:
    """A real ASGI application with an actual fixed Host and local Messages fixture."""

    def __init__(self, directory: Path, port: int):
        from learning_app.processes import OwnedProcessGroup

        self.directory, self.backend = directory, port
        self.url = f"http://127.0.0.1:{port}"
        self.group = OwnedProcessGroup("learning-browser-fixture")
        self.process = None
        self.log = None

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
                raise RuntimeError("固定Host测试服务启动失败，见server.stdout.txt")
            try:
                status, _, health = http(self.url + "/api/v1/health")
                return (
                    status == 200
                    and health["capabilities"]["question_answering"] == "ready"
                )
            except OSError:
                return False

        wait_until(ready, timeout=45)
        return self

    def counts(self):
        ledger = self.directory / "execution/boundary/boundary-ledger.jsonl"
        entries = (
            [json.loads(line) for line in ledger.read_text("utf-8").splitlines()]
            if ledger.exists()
            else []
        )
        with sqlite3.connect(self.directory / "data/business.sqlite3") as connection:
            business = connection.execute(
                "SELECT COUNT(*) FROM outbound_attempts"
            ).fetchone()[0]
            submissions = connection.execute(
                "SELECT COUNT(*) FROM submissions"
            ).fetchone()[0]
        return {
            "local_messages": sum(
                row.get("event") == "request_received" for row in entries
            ),
            "real_provider_attempts": sum(
                row.get("event") == "dispatch_started" for row in entries
            ),
            "business_attempts": business,
            "submissions": submissions,
        }

    def control(self, operation: str, **value) -> dict:
        """Test-only stdin command; no HTTP control route or browser credential."""
        identity = uuid.uuid4().hex
        self.process.stdin.write(
            json.dumps({"id": identity, "operation": operation, **value}) + "\n"
        )
        self.process.stdin.flush()
        acknowledgement = self.directory / "control" / f"{identity}.json"
        wait_until(acknowledgement.is_file, timeout=25)
        result = json.loads(acknowledgement.read_text("utf-8"))
        if "error" in result:
            raise AssertionError(result["error"])
        return result

    def generation_for(self, text: str) -> dict:
        with sqlite3.connect(self.directory / "data/business.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT g.generation_id,g.submission_id,g.state,g.body,g.cancel_requested "
                "FROM generations g JOIN submissions s ON s.submission_id=g.submission_id "
                "JOIN messages m ON m.message_id=s.user_message_id WHERE m.text=?",
                (text,),
            ).fetchone()
        if row is None:
            raise AssertionError("未找到该合成输入对应的稳定生成身份")
        return dict(row)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.stdin.write("shutdown\n")
            self.process.stdin.flush()
            self.process.wait(timeout=30)
        cleanup = self.group.close()
        if self.log:
            self.log.close()
        worker = self.directory / "worker-cleanup.json"
        return {
            "group": cleanup,
            "backend_port_closed": port_closed(self.backend),
            "worker": json.loads(worker.read_text("utf-8"))
            if worker.exists()
            else None,
        }


async def serve_fixture(directory: Path, port: int) -> int:
    import uvicorn

    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    adapter = ExecutionAdapter(ROOT, directory / "execution", mode="fixture")
    await adapter.open()
    config = AppConfig(
        mode="start",
        backend_port=port,
        frontend_port=port,
        data_dir=directory / "data",
        runtime_dir=directory / "runtime",
        harness_enabled=True,
        instance_id="browser-fixture-" + directory.parent.name,
    )
    application = create_app(config, execution=adapter)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            access_log=False,
        )
    )
    loop = asyncio.get_running_loop()

    async def control(command: dict):
        if command["operation"] == "scenario":
            adapter.boundary.set_scenario(command["scenario"])
            return {"scenario_installed": True}
        generation_id = command["generation_id"]
        if command["operation"] == "wait_gate":
            event = await asyncio.to_thread(
                adapter.boundary.wait_for_event,
                "gate_waiting",
                15,
                generation_id=generation_id,
            )
            return {"fault_point_event": event}
        if command["operation"] == "inspect":
            binding = application.state.service._binding_for(generation_id)
            native = await adapter.reconcile(binding)
            rows = [
                row
                for row in adapter.boundary.records
                if row["generation_id"] == generation_id
            ]
            snapshots = [
                event.text for event in native["events"] if event.kind == "snapshot"
            ]
            return {
                "generation_id": generation_id,
                "session_id": binding.session_id,
                "epoch": binding.epoch,
                "native_status": native["status"],
                "native_body": "".join(snapshots),
                "host_alive": adapter.host.process.poll() is None,
                "boundary": [
                    {
                        "status": row["status"],
                        "cancel_observed": row.get("cancel_observed", False),
                        "usage_settled": row.get("usage", {}).get("settled"),
                    }
                    for row in rows
                ],
            }
        raise ValueError("UNKNOWN_TEST_CONTROL")

    def parent():
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
            command = json.loads(line)
            try:
                result = asyncio.run_coroutine_threadsafe(
                    control(command), loop
                ).result(timeout=20)
            except Exception as error:
                result = {"error": f"{type(error).__name__}: {error}"}
            write_json(directory / "control" / f"{command['id']}.json", result)
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        cleanup = await adapter.close()
        write_json(directory / "worker-cleanup.json", cleanup)
    return 0 if server.started else 1


def security_case(root: Path, executable: Path, port: int) -> dict:
    directory = root / "security"
    instance = FixtureInstance(directory, port)
    browser = Browser(directory / "browser", executable)
    result = {
        "id": "V2-15",
        "status": "running",
        "evidence_level": "real fixed Host + formal ASGI + real Chromium, HTTP and WebSocket",
        "real_provider_calls": 0,
        "http": [],
        "browser": {},
    }
    foreign = None
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        browser.wait_text("后台已连接")
        snapshot = browser.snapshot("security-initial")
        browser.command(
            "fill", browser.ref(snapshot, 'textbox "新课程名称"'), "安全边界合成课程"
        )
        snapshot = browser.snapshot("security-course-filled")
        browser.command("click", browser.ref(snapshot, 'button "新建"'))
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',{name:'选择课程'})."
            "locator('option').filter({hasText:'安全边界合成课程'}).waitFor({state:'attached'});}",
        )
        snapshot = browser.snapshot("security-course")
        browser.command("click", browser.ref(snapshot, 'button "＋ 新对话"'))
        browser.wait_text("课程对话")
        snapshot = browser.snapshot("security-chat")
        fixture = json.loads(
            (ROOT / "contracts/history-example.json").read_text("utf-8")
        )
        image = directory / "confirmed-synthetic.png"
        image.write_bytes(
            base64.b64decode(fixture["confirmed_attachment"]["fixture_base64"])
        )
        browser.command("click", browser.ref(snapshot, 'button "选择待确认的图片"'))
        browser.command("upload", str(image))
        browser.wait_text("编辑截图")
        before_confirmation = instance.counts()
        snapshot = browser.snapshot("image-awaiting-user-confirmation")
        browser.command("click", browser.ref(snapshot, 'button "确认选区并加入草稿"'))
        browser.command(
            "run-code",
            "async(page)=>{await page.getByAltText('待发送的确认图片 1').waitFor({state:'visible'});}",
        )
        check(
            instance.counts() == before_confirmation,
            "confirming_attachment_does_not_dispatch_model",
            result,
        )
        snapshot = browser.snapshot("image-confirmed")
        browser.command(
            "fill",
            browser.ref(snapshot, 'textbox "问题或追问"'),
            "仅测试已确认合成图的正式发送。",
        )
        snapshot = browser.snapshot("authorized-question")
        browser.command("click", browser.ref(snapshot, 'button "发送"'))
        browser.wait_text("这是本地测试回答。")
        browser.wait_text("已完成")
        baseline = instance.counts()
        result["attempts_before"] = baseline
        check(
            baseline["local_messages"] == 1 and baseline["real_provider_attempts"] == 0,
            "authorized_browser_question_reaches_real_Host_local_boundary",
            result,
            counts=baseline,
        )
        browser.snapshot("authorized-completed")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'confirmed-chat.png'}",
            "--full-page",
        )

        initial = instance.generation_for("仅测试已确认合成图的正式发送。")
        initial_native = instance.control(
            "inspect", generation_id=initial["generation_id"]
        )
        partial = "这是足够长的合成正文片段，用来验证浏览器确实显示生成中的部分回答。"
        forbidden_tail = "这段尾部在停止后绝不应出现。"
        instance.control(
            "scenario",
            scenario={
                "text_chunks": [partial, forbidden_tail],
                "pause_after_event": "text_delta",
                "gate": "browser-stop",
            },
        )
        stop_question = "浏览器按钮停止的合成问题。"
        snapshot = browser.snapshot("before-active-stop-question")
        browser.command(
            "fill", browser.ref(snapshot, 'textbox "问题或追问"'), stop_question
        )
        snapshot = browser.snapshot("active-stop-question-ready")
        browser.command("click", browser.ref(snapshot, 'button "发送"'))
        browser.wait_text(partial)
        active = instance.generation_for(stop_question)
        gate = instance.control("wait_gate", generation_id=active["generation_id"])
        snapshot = browser.snapshot("real-active-generation-before-stop")
        check(
            partial in snapshot
            and forbidden_tail not in snapshot
            and 'button "停止回答"' in snapshot
            and active["state"] not in {"completed", "cancelled"},
            "browser_has_partial_body_and_enabled_stop_during_real_Host_gate",
            result,
            generation_id=active["generation_id"],
            fault_event=gate["fault_point_event"],
        )
        browser.command("click", browser.ref(snapshot, 'button "停止回答"'))
        browser.wait_text("已停止，保留部分内容")
        stopped = instance.generation_for(stop_question)
        native_stop = instance.control(
            "inspect", generation_id=stopped["generation_id"]
        )
        browser.snapshot("real-active-generation-stopped")
        check(
            stopped["state"] == "cancelled"
            and stopped["body"] == partial
            and native_stop["native_status"] == "cancelled"
            and native_stop["native_body"] == partial
            and native_stop["host_alive"]
            and native_stop["boundary"][0]["cancel_observed"],
            "browser_stop_preserves_partial_and_native_cancel_without_killing_Host",
            result,
        )
        instance.control(
            "scenario", scenario={"text_chunks": ["停止后同一对话的合成追问正常完成。"]}
        )
        continuation = "停止后同一对话继续的合成问题。"
        snapshot = browser.snapshot("before-continued-question")
        browser.command(
            "fill", browser.ref(snapshot, 'textbox "问题或追问"'), continuation
        )
        snapshot = browser.snapshot("continued-question-ready")
        browser.command("click", browser.ref(snapshot, 'button "发送"'))
        browser.wait_text("停止后同一对话的合成追问正常完成。")
        # There are now two completed status elements; inspect the exact input's
        # generation rather than guessing from the final visible answer.
        wait_until(
            lambda: instance.generation_for(continuation)["state"] == "completed"
        )
        continued = instance.generation_for(continuation)
        native_continued = instance.control(
            "inspect", generation_id=continued["generation_id"]
        )
        check(
            native_continued["native_status"] == "completed"
            and native_continued["host_alive"]
            and initial_native["session_id"]
            == native_stop["session_id"]
            == native_continued["session_id"]
            and instance.generation_for(stop_question)["body"] == partial,
            "browser_continues_same_native_session_without_old_callback_pollution",
            result,
        )
        result["browser_execution"] = {
            "initial": initial_native,
            "gate": gate,
            "cancelled": stopped,
            "native_cancelled": native_stop,
            "continued": continued,
            "native_continued": native_continued,
        }
        browser.snapshot("continued-in-same-chat")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'cancel-and-continue.png'}",
            "--full-page",
        )
        baseline = instance.counts()
        result["attempts_before"] = baseline
        check(
            baseline["local_messages"] == 3 and baseline["real_provider_attempts"] == 0,
            "three_explicit_browser_submissions_match_local_messages",
            result,
            counts=baseline,
        )

        # Acquire a separate local application session for raw HTTP probes. Its
        # cookie/CSRF remain in memory and never enter result/transcript files.
        _, headers, session = http(instance.url + "/api/v1/session")
        cookie = next(
            value for name, value in headers.items() if name.lower() == "set-cookie"
        ).split(";", 1)[0]
        credentials = {
            "Cookie": cookie,
            "X-Learning-CSRF": session["csrf_token"],
            "Content-Type": "application/json",
        }
        _, _, courses = http(instance.url + "/api/v1/courses", headers=credentials)
        _, _, chats = http(
            instance.url + f"/api/v1/courses/{courses[0]['course_id']}/chats",
            headers=credentials,
        )
        payload = json.dumps(
            {
                "chat_id": chats[0]["chat_id"],
                "submission_id": "blocked-browser-security",
                "text": "这次请求必须拒绝",
                "attachment_ids": [],
                "model": "deepseek-flash",
                "thinking": "high",
            }
        ).encode()
        probes = [
            (
                "wrong_origin_submission",
                "/api/v1/submissions",
                "POST",
                {"Origin": "http://untrusted.invalid"},
                payload,
                403,
                "ORIGIN_DENIED",
            ),
            (
                "wrong_host_submission",
                "/api/v1/submissions",
                "POST",
                {"Host": "untrusted.invalid"},
                payload,
                403,
                "HOST_DENIED",
            ),
            (
                "no_csrf_write",
                "/api/v1/courses",
                "POST",
                {"X-Learning-CSRF": "invalid"},
                b'{"title":"blocked"}',
                403,
                "CSRF_DENIED",
            ),
            (
                "unknown_attachment",
                "/api/v1/attachments/never-registered",
                "GET",
                {},
                None,
                404,
                "ATTACHMENT_NOT_FOUND",
            ),
            (
                "unknown_asset_path",
                "/api/v1/assets/unregistered?path=C%3A%2Fsynthetic-outside.txt",
                "GET",
                {},
                None,
                404,
                "HTTP_404",
            ),
            (
                "attachment_path_escape",
                "/api/v1/attachments/..%2Fsynthetic-outside.txt",
                "GET",
                {},
                None,
                404,
                "HTTP_404",
            ),
            (
                "wrong_origin_exit",
                "/api/v1/exit",
                "POST",
                {"Origin": "http://untrusted.invalid"},
                b"{}",
                403,
                "ORIGIN_DENIED",
            ),
        ]
        for name, path, method, extra, body, expected, code in probes:
            status, _, response = http(
                instance.url + path,
                method=method,
                headers={**credentials, **extra},
                data=body,
            )
            actual_code = response.get("code") if isinstance(response, dict) else None
            result["http"].append({"name": name, "status": status, "code": actual_code})
            check(
                status == expected and actual_code == code,
                name,
                result,
                status=status,
                code=actual_code,
            )

        # Native WebSocket in the legitimate Chromium page, without capability.
        ws_url = instance.url.replace("http:", "ws:") + "/api/v1/ws"
        allowed_ws = browser.evaluate(
            "async()=>{const session=await (await fetch('/api/v1/session')).json();"
            "const courses=await (await fetch('/api/v1/courses')).json();"
            "const chats=await (await fetch('/api/v1/courses/'+courses[0].course_id+'/chats')).json();"
            "const chat=await (await fetch('/api/v1/chats/'+chats[0].chat_id)).json();"
            "const generation=chat.generations[0];"
            "return await new Promise(resolve=>{const w=new WebSocket("
            + json.dumps(ws_url)
            + ",['learning.'+session.csrf_token]);"
            "w.onopen=()=>w.send(JSON.stringify({generation_id:generation.generation_id}));"
            "w.onmessage=e=>{const value=JSON.parse(e.data);w.close();resolve({opened:true,"
            "same_generation:value.generation_id===generation.generation_id,"
            "internal_credentials_present:['peer_token','cookie','binding','session_id','local_token'].some(k=>k in value)})};"
            "w.onerror=()=>resolve({opened:false});setTimeout(()=>{w.close();resolve({timeout:true})},5000)})}"
        )
        result["browser"]["authorized_websocket"] = allowed_ws
        check(
            allowed_ws.get("opened")
            and allowed_ws.get("same_generation")
            and not allowed_ws.get("internal_credentials_present"),
            "authorized_browser_WS_returns_own_public_generation_only",
            result,
        )
        ws = browser.evaluate(
            "async()=>await new Promise(resolve=>{const w=new WebSocket("
            + json.dumps(ws_url)
            + ");"
            "w.onopen=()=>{w.close();resolve({opened:true})};w.onclose=e=>resolve({opened:false,code:e.code});"
            "w.onerror=()=>{};setTimeout(()=>{w.close();resolve({timeout:true})},5000)})"
        )
        result["browser"]["same_origin_websocket_without_csrf"] = ws
        check(
            ws.get("opened") is False and not ws.get("timeout"),
            "browser_WS_missing_capability_rejected",
            result,
        )

        class ForeignPage(http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = "<!doctype html><meta charset=utf-8><title>未授权来源测试</title><p>合成跨来源页面</p>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html;charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        foreign = http_server.ThreadingHTTPServer(("127.0.0.1", 0), ForeignPage)
        threading.Thread(target=foreign.serve_forever, daemon=True).start()
        foreign_url = f"http://127.0.0.1:{foreign.server_port}"
        browser.command("tab-new", foreign_url)
        browser.snapshot("untrusted-origin")
        # Chromium supplies the actual Origin. A no-cors POST is observable via
        # its real response event even though page JS sees an opaque response.
        attack = json.loads(
            browser.command(
                "run-code",
                "async(page)=>{const target="
                + json.dumps(instance.url + "/api/v1/courses")
                + ";"
                "const observed=page.waitForResponse(r=>r.url()===target,{timeout:10000});"
                "const outcome=await page.evaluate(async target=>{try{const r=await fetch(target,{method:'POST',mode:'no-cors',"
                "credentials:'include',headers:{'Content-Type':'text/plain'},body:'{\"title\":\"blocked foreign\"}'});"
                "return {type:r.type,status:r.status}}catch(e){return {error:e.name}}},target);"
                "const response=await observed;return {status:response.status(),outcome,origin:page.url()};}",
                raw=True,
            )
        )
        result["browser"]["cross_origin_POST"] = attack
        check(
            attack["status"] == 403, "real_browser_cross_origin_write_rejected", result
        )
        foreign_ws = browser.evaluate(
            "async()=>await new Promise(resolve=>{const w=new WebSocket("
            + json.dumps(ws_url)
            + ");"
            "w.onopen=()=>{w.close();resolve({opened:true})};w.onclose=e=>resolve({opened:false,code:e.code});"
            "w.onerror=()=>{};setTimeout(()=>{w.close();resolve({timeout:true})},5000)})"
        )
        result["browser"]["cross_origin_websocket"] = foreign_ws
        check(
            foreign_ws.get("opened") is False and not foreign_ws.get("timeout"),
            "real_browser_cross_origin_WS_rejected",
            result,
        )
        after = instance.counts()
        result["attempts_before"], result["attempts_after"] = baseline, after
        check(
            after == baseline,
            "all_rejections_produce_zero_dispatch_and_business_mutation",
            result,
            before=baseline,
            after=after,
        )
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
    finally:
        if foreign:
            foreign.shutdown()
            foreign.server_close()
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        write_json(directory / "v2-15-result.json", result)
    return result


def browser_shell(browser: Browser, instance: Instance, result: dict) -> None:
    browser.command("open", instance.url, f"--config={browser.config}")
    browser.wait_text("后台已连接")
    snapshot = browser.snapshot("initial")
    check(
        "P0 工程与持久化验证界面" in snapshot, "page_is_explicit_P0_validation", result
    )
    check(
        "视频学习" in snapshot and "未接入" in snapshot,
        "unimplemented_capabilities_are_honest",
        result,
    )
    health = browser.evaluate(
        "async () => {const r=await fetch('/api/v1/health');"
        "return {status:r.status,health:await r.json(),origin:location.origin}}"
    )
    check(
        health["status"] == 200
        and health["health"]["instance_id"] == instance.ready["instance_id"],
        "browser_reads_owned_real_backend",
        result,
        instance_id=instance.ready["instance_id"],
    )
    if instance.mode == "start":
        check(
            health["origin"] == f"http://127.0.0.1:{instance.backend}",
            "built_same_origin",
            result,
        )
        missing = browser.evaluate(
            "async()=>{const r=await fetch('/api/not-an-endpoint');"
            "return {status:r.status,type:r.headers.get('content-type')}}"
        )
        check(
            missing["status"] == 404 and "json" in missing["type"],
            "unknown_API_is_not_SPA_HTML",
            result,
        )
    browser.command(
        "screenshot", f"--filename={browser.directory / 'light.png'}", "--full-page"
    )
    browser.command("click", browser.ref(snapshot, 'button "深色"'))
    snapshot = browser.snapshot("dark")
    check('button "浅色"' in snapshot, "manual_dark_toggle", result)
    layout = browser.evaluate(
        "() => ({dark:document.querySelector('.app-shell').classList.contains('dark'),"
        "overflow:document.documentElement.scrollWidth>innerWidth,lang:document.documentElement.lang})"
    )
    check(
        layout["dark"] and not layout["overflow"] and layout["lang"] == "zh-CN",
        "Chinese_layout_1440_dark_no_page_overflow",
        result,
    )
    browser.command(
        "screenshot", f"--filename={browser.directory / 'dark.png'}", "--full-page"
    )
    browser.command(
        "fill", browser.ref(snapshot, 'textbox "新课程名称"'), "浏览器合成数学课程"
    )
    snapshot = browser.snapshot("course-filled")
    browser.command("click", browser.ref(snapshot, 'button "新建"'))
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('combobox',{name:'选择课程'})."
        "locator('option').filter({hasText:'浏览器合成数学课程'}).waitFor({state:'attached'});}",
    )
    snapshot = browser.snapshot("course-created")
    browser.command("click", browser.ref(snapshot, 'button "＋ 新对话"'))
    browser.wait_text("课程对话")
    snapshot = browser.snapshot("chat-created")
    check('textbox "问题或追问"' in snapshot, "real_course_and_chat_created", result)
    check(
        'button "发送" [disabled]' in snapshot,
        "unconfigured_model_cannot_submit",
        result,
    )


def shell_cases(
    root: Path, executable: Path, mode: str, backend: int, frontend: int
) -> dict:
    directory = root / mode
    instance = Instance(
        directory, "start" if mode == "built" else "dev", backend, frontend
    )
    browser = Browser(directory / "browser", executable)
    result = {
        "id": f"P0-01-browser-{mode}",
        "status": "running",
        "evidence_level": "real Chromium + owned formal services",
    }
    try:
        instance.start()
        browser_shell(browser, instance, result)
        if mode == "dev":
            # Stop the owned backend through its launcher shutdown, retaining the
            # already loaded document. The launcher rolls back the Vite service.
            result["first_cleanup"] = instance.stop()
            browser.wait_text("后台未连接")
            browser.snapshot("backend-disconnected")
            check(True, "stopped_backend_is_observed_by_existing_browser", result)
            instance = Instance(directory, "dev", backend, frontend).start()
            snapshot = browser.snapshot("before-reconnect")
            browser.ref(snapshot, 'button "重新连接"')
            # Vite can reload the page after the snapshot when its server returns.
            # Resolve the observed button at action time instead of using a stale ref.
            browser.command(
                "run-code",
                "async(page)=>{await page.getByRole('button',"
                "{name:'重新连接',exact:true}).click();}",
            )
            browser.wait_text("后台已连接")
            browser.snapshot("backend-reconnected")
            check(True, "real_backend_restart_and_explicit_reconnect", result)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        if result["cleanup"].get("record", {}).get("cleanup", {}).get(
            "remaining_pids"
        ) or not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["dev", "built", "all", "security"], default="all"
    )
    parser.add_argument(
        "--run-id",
        default=f"browser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
    )
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18820)
    parser.add_argument("--frontend-port", type=int, default=15190)
    parser.add_argument(
        "--host-fixture",
        action="store_true",
        help="另验固定Host本地fixture与V2-15，无真实模型",
    )
    parser.add_argument("--serve-fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        return asyncio.run(serve_fixture(args.serve_fixture, args.backend_port))
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    browser = args.browser_path
    if browser is None:
        local = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
        )
        browser = local if local.is_file() else None
    if browser is None or not browser.is_file():
        raise RuntimeError(
            "BROWSER_MISSING: 请用 --browser-path 指定已核验Chromium；本脚本不自动下载"
        )
    cli_package = json.loads((CLI.parent / "package.json").read_text("utf-8"))
    record = {
        "run_id": args.run_id,
        "cli_version": cli_package["version"],
        "browser_executable": str(browser),
        "browser_sha256": hashlib.sha256(browser.read_bytes()).hexdigest(),
        "real_model_attempts": 0,
        "scope": "P0 engineering acceptance; not learning trial",
        "cases": [],
    }
    modes = (
        ["dev", "built"]
        if args.mode == "all"
        else ([] if args.mode == "security" else [args.mode])
    )
    for index, mode in enumerate(modes):
        record["cases"].append(
            shell_cases(
                directory,
                browser,
                mode,
                args.backend_port + index,
                args.frontend_port + index,
            )
        )
    if args.host_fixture or args.mode == "security":
        record["cases"].append(security_case(directory, browser, args.backend_port + 2))
    record["status"] = (
        "passed"
        if all(case["status"] == "passed" for case in record["cases"])
        else "failed"
    )
    write_json(directory / "result.json", record)
    print(
        json.dumps(
            {"status": record["status"], "evidence": str(directory / "result.json")},
            ensure_ascii=False,
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
