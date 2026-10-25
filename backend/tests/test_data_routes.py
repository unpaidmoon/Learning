"""Authenticated real package endpoints, cancellation and closed activation."""

import threading
import time
from zipfile import ZipFile

from fastapi.testclient import TestClient
from workbench_support import learning_course

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.data_location import ProfileLocation
from learning_app.storage import Repository


def connect(client):
    return {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}


def wait_operation(client, identity):
    for _ in range(200):
        result = client.get(f"/api/v1/data/operations/{identity}").json()
        if result["state"] not in {"prepared", "running"}:
            return result
        time.sleep(0.02)
    raise AssertionError("owned data operation did not finish")


def test_export_routes_require_csrf_and_download_same_completed_package(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    app = create_app(AppConfig(mode="test", data_dir=repo.root), repository=repo)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        assert (
            client.post("/api/v1/data/exports", json={"operation_id": "export"}).status_code == 403
        )
        started = client.post(
            "/api/v1/data/exports", headers=headers, json={"operation_id": "export"}
        )
        assert started.status_code == 200
        completed = wait_operation(client, "export")
        assert completed["state"] == "completed"
        info = client.get("/api/v1/data").json()
        assert info["data_directory"] == str(repo.root) and info["business_bytes"] > 0
        archive = client.get("/api/v1/data/operations/export/download")
        assert archive.status_code == 200 and archive.content.startswith(b"PK")
        assert (
            client.post(
                "/api/v1/data/exports", headers=headers, json={"operation_id": "export"}
            ).json()["result"]
            == completed["result"]
        )
        assert client.get("/api/v1/data/operations/export/download").content == archive.content
        from io import BytesIO

        with ZipFile(BytesIO(archive.content)) as zipped:
            assert "offline/index.html" in zipped.namelist()
            assert not any(name.startswith("originals/") for name in zipped.namelist())


def test_active_data_operation_requires_exit_confirmation_and_stops_without_publish(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def hook(name, detail):
        if name == "data_export_after_intent":
            entered.set()
            assert release.wait(10)

    repo.fault_hook = hook
    config = AppConfig(mode="test", data_dir=repo.root)
    app = create_app(config, repository=repo)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        try:
            assert (
                client.post(
                    "/api/v1/data/exports", headers=headers, json={"operation_id": "active"}
                ).status_code
                == 200
            )
            assert entered.wait(3)
            status = client.get("/api/v1/runtime").json()
            assert [row["operation_id"] for row in status["active_data_operations"]] == ["active"]
            denied = client.post(
                "/api/v1/runtime/prepare-exit",
                headers=headers,
                json={"instance_id": config.instance_id},
            )
            assert denied.status_code == 409
            assert (
                client.post(
                    "/api/v1/data/operations/active/cancel", headers=headers, json={}
                ).status_code
                == 200
            )
        finally:
            release.set()
        cancelled = wait_operation(client, "active")
        assert cancelled["state"] == "cancelled"
        assert client.get("/api/v1/data/operations/active/download").status_code == 409
        assert repo.counts()["outbound_attempts"] == 0


def test_move_route_only_schedules_then_next_start_reads_new_pointer(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    repo.create_chat(course, "old_chat")
    initial = repo.root
    destination = tmp_path / "新数据位置"
    destination.mkdir()
    config = AppConfig(mode="test", data_dir=initial)
    app = create_app(config, repository=repo)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        response = client.post(
            "/api/v1/data/transfers",
            headers=headers,
            json={"operation_id": "move", "kind": "move", "parent": str(destination)},
        )
        assert response.status_code == 200
        ready = wait_operation(client, "move")
        assert ready["state"] == "ready"
        assert (
            client.post(
                "/api/v1/data/operations/move/schedule", headers=headers, json={}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/data").json()["pending_operation_id"] == "move"
        assert (
            client.post(
                "/api/v1/data/operations/move/unschedule", headers=headers, json={}
            ).status_code
            == 200
        )
        assert not client.get("/api/v1/data").json()["pending_switch"]
        assert (
            client.post(
                "/api/v1/data/operations/move/schedule", headers=headers, json={}
            ).status_code
            == 200
        )
    location = ProfileLocation(initial)
    with location.lease():
        location.apply_closed()
    new_app = create_app(config)
    assert new_app.state.config.data_dir == location.current()
    with TestClient(new_app, base_url="http://127.0.0.1:8765") as client:
        connect(client)
        assert client.get("/api/v1/chats/old_chat").status_code == 200
    original = Repository(initial)
    assert original.get_chat("old_chat")["chat_id"] == "old_chat"
    original.close()
