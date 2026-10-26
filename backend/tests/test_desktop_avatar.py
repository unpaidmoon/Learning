"""Native account pictures must load under the desktop's same-origin CSP."""

import httpx
import pytest
from fastapi.testclient import TestClient
from test_desktop import app_at, native_envelope, png

from learning_app.desktop_avatar import picture_reference, read_account_picture
from learning_app.errors import AppError


def test_native_profile_picture_uses_authenticated_same_origin_route(tmp_path, monkeypatch):
    profile = {"status": "ready", "value": {
        "id": "account-a", "name": "测试账号", "avatarUrl": "https://pictures.example/avatar.png",
    }}
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        gateway = app.state.desktop_gateway

        def upstream(request):
            return httpx.Response(200, json={"type": "server-response", "rpcId": "native-rpc",
                "result": {"ok": True, "value": profile}})

        gateway.client = httpx.AsyncClient(base_url="http://harness.invalid",
                                          transport=httpx.MockTransport(upstream))
        gateway._host = gateway.service.execution.host
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        result = client.post("/desktop/api/account/getProfile", headers=csrf,
                             json=native_envelope("account/getProfile", {})).json()
        picture = result["result"]["value"]["value"]["avatarUrl"]
        assert picture.startswith("/api/v1/desktop/account-avatar/"), picture
        assert "pictures.example" not in str(result)

        async def rpc_args(method, arguments):
            assert method == "account/getProfile"
            return profile

        monkeypatch.setattr(gateway.service.execution, "rpc_args", rpc_args, raising=False)
        calls = []

        def read_picture(url):
            calls.append(url)
            return png(), "image/png"

        monkeypatch.setattr("learning_app.desktop.read_account_picture", read_picture)
        response = client.get(picture)
        assert response.status_code == 200
        assert response.content == png()
        assert response.headers["content-type"] == "image/png"
        assert calls == ["https://pictures.example/avatar.png"]
        profile["value"]["id"] = "account-b"
        assert client.get(picture).status_code == 404
        assert len(calls) == 1
        client.cookies.clear()
        assert client.get(picture).status_code == 403


@pytest.mark.parametrize("status,media,content", [
    (200, "image/png", png()),
    (302, "image/png", b""),
    (200, "image/svg+xml", b"<svg/>"),
    (200, "image/png", b"x" * (2 * 1024 * 1024 + 1)),
], ids=["raster", "redirect", "svg", "oversize"])
def test_picture_fetch_is_bounded_anonymous_and_pins_public_address(
    monkeypatch, status, media, content,
):
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.url.host == "1.1.1.1"
        assert request.headers["host"] == "pictures.example"
        assert request.extensions["sni_hostname"] == "pictures.example"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(status, headers={"content-type": media,
                                              "location": "http://127.0.0.1/private"},
                              content=content)

    original = httpx.Client
    monkeypatch.setattr("learning_app.desktop_avatar.resolve_public", lambda *_: ["1.1.1.1"])
    monkeypatch.setattr("learning_app.desktop_avatar.httpx.Client", lambda **kwargs:
                        original(**kwargs, transport=httpx.MockTransport(upstream)))
    if status == 200 and media == "image/png" and len(content) < 2 * 1024 * 1024:
        assert read_account_picture("https://pictures.example/avatar.png") == (content, media)
    else:
        with pytest.raises(AppError):
            read_account_picture("https://pictures.example/avatar.png")
    assert len(requests) == 1


@pytest.mark.parametrize("url", [None, "http://pictures.example/a.png", "https://127.0.0.1/a", "file:///a"])
def test_missing_or_nonpublic_picture_uses_native_fallback(url):
    assert picture_reference({"status": "ready", "value": {"avatarUrl": url}}) is None
