import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig, load_config


def config_at(tmp_path, **values):
    return AppConfig(
        mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run", **values
    )


def connect(client):
    response = client.get("/api/v1/session")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return {"X-Learning-CSRF": response.json()["csrf_token"]}


def test_config_is_root_relative_and_explicit_values_win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    value = load_config(
        {"backend_port": 18765},
        environ={
            "LEARNING_APP_BACKEND_PORT": "18766",
            "LEARNING_APP_DATA_DIR": ".runtime/config-test",
        },
    )
    assert value.backend_port == 18765
    assert value.data_dir == PROJECT_ROOT / ".runtime/config-test"
    assert load_config(environ={"LEARNING_APP_BACKEND_PORT": "18766"}).backend_port == 18766
    with pytest.raises(ValueError):
        AppConfig(host="0.0.0.0")


def test_import_and_openapi_have_no_database_or_host_side_effect(tmp_path):
    config = config_at(tmp_path)
    app = create_app(config)
    assert app.openapi()["paths"]["/api/v1/health"]
    assert not config.data_dir.exists()
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_real_health_and_private_api_capability(tmp_path):
    with TestClient(create_app(config_at(tmp_path)), base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["capabilities"] == {
            "harness": "not_configured",
            "question_answering": "not_configured",
            "persistence": "ready",
            "video": "ready",
            "transcription": "not_implemented",
            "knowledge_base": "not_implemented",
        }
        assert client.get("/api/v1/courses").status_code == 403
        headers = connect(client)
        assert client.post("/api/v1/courses", json={"title": "数学"}).status_code == 403
        response = client.post("/api/v1/courses", headers=headers, json={"title": "数学"})
        assert response.status_code == 200
        assert client.get("/api/v1/courses").json()[0]["title"] == "数学"
        assert (
            client.post(
                "/api/v1/submissions",
                headers=headers,
                json={
                    "chat_id": "none",
                    "submission_id": "test",
                    "text": "问题",
                },
            ).json()["code"]
            == "EXECUTION_UNAVAILABLE"
        )
        assert client.get("/api/does-not-exist").status_code == 404


def test_origin_host_ws_and_unregistered_asset_are_denied(tmp_path):
    app = create_app(config_at(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        for changed in (
            {"Origin": "https://evil.invalid"},
            {"Host": "evil.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert client.get("/api/v1/courses", headers=changed).status_code == 403
            assert (
                client.post(
                    "/api/v1/courses", headers={**headers, **changed}, json={"title": "forbidden"}
                ).status_code
                == 403
            )
        assert client.get("/api/v1/courses").json() == []
        assert client.get("/api/v1/attachments/not_registered").status_code == 404
        with pytest.raises(Exception) as exc:
            with client.websocket_connect("/api/v1/ws", headers={"Origin": "https://evil.invalid"}):
                pass
        assert exc.value.code == 1008
        with pytest.raises(Exception) as exc:
            with client.websocket_connect(
                "/api/v1/ws", headers={"Origin": "http://127.0.0.1:8765"}
            ):
                pass
        assert exc.value.code == 1008


def test_confirmed_image_upload_hash_and_no_path_interface(tmp_path):
    with TestClient(create_app(config_at(tmp_path)), base_url="http://127.0.0.1:8765") as client:
        headers = connect(client)
        image = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(image, "PNG")
        payload = {
            "data_base64": base64.b64encode(image.getvalue()).decode(),
            "media_type": "image/png",
            "confirmed": True,
        }
        response = client.post("/api/v1/attachments", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert (
            client.get(f"/api/v1/attachments/{result['attachment_id']}").content == image.getvalue()
        )
        assert "path" not in result
        assert (
            client.post(
                "/api/v1/attachments",
                headers=headers,
                json={**payload, "path": "C:/Windows/win.ini"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/attachments", headers=headers, json={**payload, "confirmed": False}
            ).status_code
            == 422
        )


def test_built_static_api_boundary_and_missing_build(tmp_path):
    config = config_at(tmp_path)
    with pytest.raises(Exception, match="请先执行前端构建"):
        create_app(config.model_copy(update={"mode": "start", "static_dir": tmp_path / "missing"}))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>真实静态首页</h1>", encoding="utf-8")
    (dist / "asset.js").write_text("const x = 1;", encoding="utf-8")
    config = config.model_copy(update={"mode": "start", "static_dir": dist})
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        connect(client)
        assert "真实静态首页" in client.get("/").text
        assert client.get("/chat/somewhere").status_code == 200
        assert client.get("/asset.js").text == "const x = 1;"
        assert client.get("/missing.js").status_code == 404
        response = client.get("/api/missing")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
