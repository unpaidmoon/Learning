"""Exercise actual Windows ownership and readiness, not mock process calls."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import pytest

from learning_app.dev import wait_for_http
from learning_app.processes import OwnedProcessGroup, ProcessError, require_free_port


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_port_conflict_does_not_close_existing_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ProcessError, match="未结束占用进程") as raised:
            require_free_port("127.0.0.1", port)
        assert raised.value.code == "PORT_IN_USE"
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object acceptance")
def test_job_cleans_descendant_and_preserves_other_process(tmp_path: Path) -> None:
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import http.server, json, os, sys\n"
        "server=http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),http.server.BaseHTTPRequestHandler)\n"
        "print(json.dumps({'pid':os.getpid()}),flush=True)\n"
        "server.serve_forever()\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys\n"
        "child=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
        "child.wait()\n",
        encoding="utf-8",
    )
    owned_port, other_port = _free_port(), _free_port()
    other = subprocess.Popen(
        [sys.executable, str(child_script), str(other_port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert other.stdout is not None
        assert json.loads(other.stdout.readline())["pid"] == other.pid
        group = OwnedProcessGroup("pytest-descendant")
        try:
            parent = group.spawn(
                [sys.executable, str(parent_script), str(child_script), str(owned_port)],
                cwd=tmp_path,
            )
            assert parent.stdout is not None
            child_pid = json.loads(parent.stdout.readline())["pid"]
            assert {parent.pid, child_pid}.issubset(group.pids())
            cleanup = group.close()
            assert cleanup["remaining_pids"] == []
            assert {parent.pid, child_pid}.issubset(cleanup["owned_pids_before"])
            require_free_port("127.0.0.1", owned_port)
            assert other.poll() is None
            with socket.create_connection(("127.0.0.1", other_port), timeout=1):
                pass
        finally:
            group.close()
    finally:
        other.terminate()
        other.wait(timeout=5)
        other.communicate()


def test_partial_start_failure_rolls_back_owned_service(tmp_path: Path) -> None:
    group = OwnedProcessGroup("pytest-partial-failure")
    try:
        first = group.spawn(
            [sys.executable, "-c", "import sys; print('ready',flush=True);sys.stdin.read()"],
            cwd=tmp_path,
        )
        assert first.stdout is not None and first.stdout.readline().strip() == "ready"
        failing = group.spawn([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path)
        failing.wait(timeout=5)
        with pytest.raises(ProcessError) as raised:
            wait_for_http("http://127.0.0.1:1", processes=[first, failing], timeout=1)
        assert raised.value.code == "SERVICE_EXITED"
    finally:
        cleanup = group.close()
    assert first.poll() is not None
    assert cleanup["remaining_pids"] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object owner-death acceptance")
def test_owner_death_closes_job_without_cooperative_cleanup(tmp_path: Path) -> None:
    port = _free_port()
    owner = tmp_path / "owner.py"
    owner.write_text(
        "from learning_app.processes import OwnedProcessGroup\n"
        "from pathlib import Path\n"
        "import json,sys\n"
        "group=OwnedProcessGroup('test-owner-death')\n"
        "source=\"import socket,sys; listener=socket.socket();listener.bind(('127.0.0.1',int(sys.argv[1])));listener.listen();print('ready',flush=True);sys.stdin.read()\"\n"
        "child=group.spawn([sys.executable,'-c',source,sys.argv[1]],cwd=Path.cwd())\n"
        "assert child.stdout.readline().strip()=='ready'\n"
        "print(json.dumps({'pid':child.pid}),flush=True)\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(owner), str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.stdout is not None
        assert json.loads(process.stdout.readline())["pid"] > 0
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        # Fault at the confirmed child-listening event. No cooperative finally
        # runs; the last handle closing must terminate the owned child itself.
        process.terminate()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                require_free_port("127.0.0.1", port)
                break
            except ProcessError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.communicate()


def test_readiness_rejects_unrelated_instance(tmp_path: Path) -> None:
    port = _free_port()
    script = tmp_path / "health.py"
    script.write_text(
        "import http.server,sys\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        '  self.send_response(200);self.end_headers();self.wfile.write(b\'{"ready":true,"instance_id":"other"}\')\n'
        " def log_message(self,*args):pass\n"
        "server=http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler)\n"
        "print('ready',flush=True)\n"
        "server.serve_forever()\n",
        encoding="utf-8",
    )
    with OwnedProcessGroup("pytest-instance-identity") as group:
        process = group.spawn([sys.executable, str(script), str(port)], cwd=tmp_path)
        assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        for identity_header in (False, True):
            with pytest.raises(ProcessError) as raised:
                wait_for_http(
                    f"http://127.0.0.1:{port}",
                    processes=[process],
                    timeout=2,
                    instance_id="mine",
                    identity_header=identity_header,
                )
            assert raised.value.code == "INSTANCE_MISMATCH"
        # Mismatch rejection did not send a control/shutdown request to that listener.
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}", timeout=1) as response:
            assert response.status == 200


def test_developer_runner_rolls_back_ready_backend_on_frontend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learning_app.dev as developer
    from learning_app.config import AppConfig

    backend_port, frontend_port = _free_port(), _free_port()
    fake_vite = tmp_path / "frontend/node_modules/vite/bin/vite.js"
    fake_vite.parent.mkdir(parents=True)
    fake_vite.write_text("process.exit(73);\n", encoding="utf-8")
    config = AppConfig(
        project_root=tmp_path,
        backend_port=backend_port,
        frontend_port=frontend_port,
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "run",
        harness_enabled=False,
    )

    def configured(overrides: dict[str, object]) -> AppConfig:
        return config.model_copy(update=overrides)

    monkeypatch.setattr(developer, "load_config", configured)
    monkeypatch.setenv("LEARNING_APP_HARNESS_ENABLED", "false")
    assert developer.run(["dev", "--timeout", "10"]) == 1
    records = list((tmp_path / "run/apps").glob("*/run.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["failure_code"] == "SERVICE_EXITED"
    assert record["status"] == "failed"
    assert record["cleanup"]["remaining_pids"] == []
    require_free_port("127.0.0.1", backend_port)
    require_free_port("127.0.0.1", frontend_port)
