"""Native theme persistence through the restricted real application RPC seam."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from test_desktop import app_at, native_envelope

from learning_app.desktop_theme import DesktopThemePreferences
from learning_app.errors import AppError


def change(value, revision=None):
    return {"ns": "ui-theme", "ops": [{"op": "set", "path": ["preference"], "value": value}],
            **({"expectedRevision": revision} if revision is not None else {})}


def test_native_theme_rpc_persists_across_restart_and_rejects_stale_writes(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}

        def call(method, value):
            response = client.post("/desktop/api/" + method,
                                   json=native_envelope(method, value), headers=csrf)
            assert response.status_code == 200
            return response.json()["result"]

        # Keep native pages while merging the application's durable appearance.
        # The fixture describes no real credentials or provider configuration.
        with patch.object(app.state.desktop_gateway, "native_read", AsyncMock(return_value={
            "writable": True, "hasDocument": True, "namespaces": [{"ns": "ui-chat"}],
        })):
            described = call("settings/describe", {})["value"]
            assert described["writable"] is True and described["hasDocument"] is False
            assert [row["ns"] for row in described["namespaces"]] == ["ui-chat", "ui-theme"]
            namespace = described["namespaces"][-1]
            assert namespace["value"]["preference"] == "system"
            first = call("settings/mutate", change("dark", 0))["value"]
            assert first["value"]["preference"] == "dark" and first["revision"] == 1
            assert call("settings/query", {"ns": "ui-theme"})["value"] == first
            conflict = call("settings/mutate", change("light", 0))
            assert conflict["ok"] is False and conflict["error"]["code"] == "settings/conflict"
            assert call("settings/query", {"ns": "ui-theme"})["value"]["value"]["preference"] == "dark"
            assert "private-token" not in str(described) and str(tmp_path) not in str(described)
        assert app.state.service.execution.starts == []

    restarted = app_at(tmp_path)
    with TestClient(restarted, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post("/desktop/api/settings/query", headers=csrf,
                               json=native_envelope("settings/query", {"ns": "ui-theme"})).json()
        namespace = response["result"]["value"]
        assert namespace["value"]["preference"] == "dark" and namespace["revision"] == 1
        preferences = restarted.state.desktop_gateway.theme
        preferences.handle("settings/mutate", change("light", 1))
        preferences.handle("settings/mutate", change("system", 2))
        assert preferences.handle("settings/query", {"ns": "ui-theme"})["value"]["preference"] == "system"


@pytest.mark.parametrize("payload", [
    {"ns": "credentials", "ops": [{"op": "set", "path": ["preference"], "value": "dark"}]},
    {"ns": "ui-theme", "ops": [{"op": "set", "path": ["fontSize"], "value": 23}]},
    {"ns": "ui-theme", "ops": [{"op": "set", "path": [], "value": {"preference": "dark"}}]},
    {"ns": "ui-theme", "ops": [{"op": "set", "path": ["preference"], "value": "custom-css"}]},
    {"ns": "ui-theme", "ops": [{"op": "set", "path": ["preference"], "value": "dark"}], "expectedRevision": True},
])
def test_theme_adapter_rejects_other_namespaces_paths_and_values(tmp_path, payload):
    preferences = DesktopThemePreferences(tmp_path)
    with pytest.raises(AppError):
        preferences.handle("settings/mutate", payload)
    assert preferences.handle("settings/query", {"ns": "ui-theme"})["value"]["preference"] == "system"
    assert not preferences.path.exists()


def test_theme_projection_boot_matches_durable_preference_and_failed_save_is_not_accepted(tmp_path):
    preferences = DesktopThemePreferences(tmp_path)
    preferences.handle("settings/mutate", change("dark", 0))
    html = '<style>:root{color-scheme:light}body{background-color:#fff;--dsh-boot-bg:#fff}</style>' + (
        '<body><script>const preference = "system"\n  const systemDark = preference === "system";</script></body>'
    )
    projected = preferences.project_index(html)
    assert 'const preference = "dark"' in projected
    assert "color-scheme:dark" in projected and "--dsh-boot-bg:#151517" in projected
    assert 'const preference = "system"' in html
    with patch("learning_app.desktop_theme.os.replace", side_effect=OSError("fixture write failure")):
        with pytest.raises(AppError) as failed:
            preferences.handle("settings/mutate", change("light", 1))
    assert failed.value.code == "DESKTOP_THEME_SAVE_FAILED"
    assert DesktopThemePreferences(tmp_path).handle("settings/query", {"ns": "ui-theme"})["value"]["preference"] == "dark"
    assert not list(preferences.path.parent.glob(".theme-*.tmp"))


def test_native_font_and_theme_changes_preserve_each_other(tmp_path):
    preferences = DesktopThemePreferences(tmp_path)
    first = preferences.handle("settings/mutate", {
        "ns": "ui-theme", "expectedRevision": 0,
        "ops": [{"op": "set", "path": ["fontSize"], "value": 16}],
    })
    preferences.handle("settings/mutate", change("dark", first["revision"]))
    reopened = DesktopThemePreferences(tmp_path).handle("settings/query", {"ns": "ui-theme"})
    assert reopened["value"]["fontSize"] == 16
    assert reopened["value"]["preference"] == "dark"
