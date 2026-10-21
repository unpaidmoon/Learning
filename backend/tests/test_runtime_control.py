"""Exit intent, authentication and asset retention against the business service."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.processes import require_free_port
from learning_app.runtime_control import install_runtime_routes
from learning_app.storage import Repository


def runtime_app(tmp_path: Path, *, identity: str = "f" * 32):
    config = AppConfig(
        mode="test",
        instance_id=identity,
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "run",
    )
    repository = Repository(config.data_dir)
    repository.create_course("合成数学", "course")
    repository.create_chat("course", "chat")
    app = create_app(config, repository=repository)
    if not any(route.path == "/api/v1/runtime" for route in app.routes):
        install_runtime_routes(app)
    return app, repository, config


def connect(client):
    session = client.get("/api/v1/session")
    return {"X-Learning-CSRF": session.json()["csrf_token"]}


def prepared_generation(repo):
    result = repo.publish_submission("chat", "保留这个题目", "question")
    gid = result["generation_id"]
    binding = repo.bind_execution("chat", "internal_session")
    repo.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    repo.ingest_event(
        gid,
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id="chunk_1",
        seq=1,
        kind="body",
        text="已产生的部分回答",
    )
    repo.mark_unknown(gid, "FIXTURE_TRANSPORT_LOST")
    return gid, binding


def test_exit_preparation_requires_confirmation_and_retains_unknown_partial_answer(tmp_path):
    app, repo, config = runtime_app(tmp_path)
    gid, _ = prepared_generation(repo)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED"
        assert not client.get("/api/v1/runtime").json()["exit_prepared"]
        assert not repo.get_generation(gid)["cancel_requested"]
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id, "confirm_active": True},
        )
        assert response.status_code == 200
        assert response.json()["exit_prepared"]
        assert repo.get_generation(gid)["state"] == "unknown"
        assert repo.get_generation(gid)["cancel_requested"]
        assert repo.get_generation(gid)["body"] == "已产生的部分回答"
        assert repo.counts()["messages"] == 2
        assert repo.counts()["outbound_attempts"] == 0
        frozen = client.post(
            "/api/v1/submissions",
            headers=headers,
            json={"chat_id": "chat", "submission_id": "after_exit", "text": "不要新发"},
        )
        assert frozen.status_code == 409
        assert frozen.json()["code"] == "APPLICATION_EXIT_PREPARED"
        assert repo.counts()["submissions"] == 1


def test_runtime_routes_require_capability_and_never_exit_unowned_server(tmp_path):
    app, repo, config = runtime_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/v1/runtime").status_code == 403
        headers = connect(client)
        assert not client.get("/api/v1/runtime").json()["can_exit"]
        assert (
            client.post(
                "/api/v1/runtime/prepare-exit",
                json={"instance_id": config.instance_id},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/runtime/exit",
            headers=headers,
            json={"instance_id": config.instance_id},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "OWNED_LAUNCHER_REQUIRED"
        assert not config.runtime_dir.exists()
        assert not app.state.exit_prepared
        assert repo.counts()["chats"] == 1


def test_stale_instance_exit_does_not_freeze_current_service(tmp_path):
    app, _, _ = runtime_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=connect(client),
            json={"instance_id": "e" * 32, "confirm_active": True},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "INSTANCE_MISMATCH"
        assert not app.state.exit_prepared


def test_owned_exit_writes_only_matching_instance_mailbox(tmp_path, monkeypatch):
    app, _, config = runtime_app(tmp_path)
    monkeypatch.setenv("LEARNING_APP_OWNED_STDIN", "1")
    run_dir = config.runtime_dir / "apps" / config.instance_id
    run_dir.mkdir(parents=True)
    record = {"instance_id": config.instance_id, "backend_pid": os.getpid(), "status": "ready"}
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        assert client.get("/api/v1/runtime").json()["can_exit"]
        record["backend_pid"] = os.getpid() + 1
        (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
        assert not client.get("/api/v1/runtime").json()["can_exit"]
        record["backend_pid"] = os.getpid()
        (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
        response = client.post(
            "/api/v1/runtime/exit",
            headers=headers,
            json={"instance_id": config.instance_id},
        )
        assert response.status_code == 200
        assert response.json()["exit_requested"]
        assert (run_dir / "stop.request").read_text(encoding="ascii") == config.instance_id
        assert not (run_dir / "stop.request.tmp").exists()


def test_completed_answer_with_unknown_usage_is_not_reported_as_running(tmp_path):
    app, repo, _ = runtime_app(tmp_path)
    gid, binding = prepared_generation(repo)
    repo.ingest_event(
        gid,
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id="completed",
        seq=2,
        kind="terminal",
        status="completed",
    )
    assert any(value["generation_id"] == gid for value in repo.recoverable_generations())
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        connect(client)
        assert client.get("/api/v1/runtime").json()["active_generations"] == []


def test_hidden_launch_and_confirmed_exit_preserve_uncertain_answer(tmp_path):
    """Real owned process/HTTP lifecycle; the provider is deliberately disabled."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<p>合成运行入口 fixture</p>", encoding="utf-8")
    data_dir = tmp_path / "data"
    repository = Repository(data_dir)
    repository.create_course("合成数学", "course")
    repository.create_chat("course", "chat")
    gid, _ = prepared_generation(repository)
    repository.close()
    environment = {
        **os.environ,
        "LEARNING_APP_HARNESS_ENABLED": "false",
        "LEARNING_APP_DATA_DIR": str(data_dir),
        "LEARNING_APP_RUNTIME_DIR": str(tmp_path / "run"),
        "LEARNING_APP_STATIC_DIR": str(static_dir),
    }
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    command = [sys.executable, str(root / "scripts/app.py")]
    launched = subprocess.run(
        [*command, "launch", "--backend-port", str(port), "--timeout", "15"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=40,
        **hidden,
    )
    assert launched.returncode == 0, launched.stdout + launched.stderr
    line = next(
        value for value in launched.stdout.splitlines() if value.startswith("LEARNING_APP_READY ")
    )
    identity = json.loads(line.removeprefix("LEARNING_APP_READY "))["instance_id"]
    record_path = tmp_path / "run/apps" / identity / "run.json"
    try:
        # This noninteractive process cannot implicitly consent to active exit.
        refused = subprocess.run(
            [*command, "stop", "--instance-id", identity],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=15,
            **hidden,
        )
        assert refused.returncode == 1
        assert "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED" in refused.stderr
        assert json.loads(record_path.read_text(encoding="utf-8"))["status"] == "ready"
    finally:
        stopped = subprocess.run(
            [*command, "stop", "--instance-id", identity, "--confirm-active"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            **hidden,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record["status"] == "stopped":
                break
            time.sleep(0.05)
        assert record["status"] == "stopped"
        assert record["cleanup"]["remaining_pids"] == []
        require_free_port("127.0.0.1", port)
    reopened = Repository(data_dir)
    try:
        assert reopened.get_generation(gid)["state"] == "unknown"
        assert reopened.get_generation(gid)["cancel_requested"]
        assert reopened.get_generation(gid)["body"] == "已产生的部分回答"
        assert reopened.counts()["outbound_attempts"] == 0
    finally:
        reopened.close()
