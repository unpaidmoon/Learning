"""Opt-in checks against a controlled local HTTP service, never a real updater."""

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener

import httpx
import pytest
from pydantic import ValidationError

from learning_app.desktop_updates import DesktopUpdates, UpdateConfigureInput, UpdateValues
from learning_app.errors import AppError
from learning_app.workbench.external_http import PublicTextReader


def offer():
    return {
        "format": 1,
        "latest": {
            "release_id": "learning-workbench-win-20261025-r6",
            "download_url": "https://updates.example.org/archive.zip",
            "sha256": "a" * 64,
            "bytes": 123456,
        },
    }


@pytest.fixture
def controlled(tmp_path):
    state = {"requests": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            data = json.dumps(offer()).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def route(request):
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        opener = build_opener(ProxyHandler({}))
        from urllib.error import HTTPError

        try:
            response = opener.open(f"http://127.0.0.1:{server.server_port}/feed.json", timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return httpx.Response(
                response.status,
                stream=httpx.ByteStream(response.read()),
                headers={"Content-Type": "application/json"},
                request=request,
            )

    reader = PublicTextReader(
        resolver=lambda *_: ["93.184.216.34"],
        transport=httpx.MockTransport(route),
        media_types={"application/json"},
        schemes={"https"},
        allow_query=False,
    )
    release = tmp_path / "release"
    release.mkdir()
    (release / "desktop-release.json").write_text(
        json.dumps({"release_id": "learning-workbench-win-20261025-r5"}), "utf-8"
    )
    try:
        yield reader, state, release
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def settle(manager):
    async with asyncio.timeout(5):
        while manager.active:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_default_zero_requests_manual_check_is_same_operation_and_never_downloads(
    tmp_path, controlled
):
    reader, observed, release = controlled
    manager = DesktopUpdates(tmp_path / "data", tmp_path / "profile", release, reader=reader)
    manager.start()
    try:
        await asyncio.sleep(0.05)
        assert manager.status()["checking"] is False and observed["requests"] == []
        with pytest.raises(AppError) as error:
            await manager.check("unconfigured")
        assert error.value.code == "UPDATE_SOURCE_NOT_CONFIGURED"
        manager.configure(
            UpdateConfigureInput(
                operation_id="configure",
                expected_revision=0,
                values=UpdateValues(feed_url="https://updates.example.org/feed.json"),
            )
        )
        assert observed["requests"] == []
        await manager.check("manual_one")
        await settle(manager)
        complete = manager.reconcile("manual_one")
        assert complete["state"] == "completed" and complete["outcome"] == "new_version"
        assert (await manager.check("manual_one")) == complete
        assert observed["requests"] == ["/feed.json"]
        assert manager.status()["installs_updates"] is False
        assert not list(tmp_path.rglob("*.zip")) and not list(tmp_path.rglob("*.exe"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_automatic_is_explicit_and_failed_check_does_not_restart_until_next_day(
    tmp_path, controlled
):
    reader, observed, release = controlled
    clock = {"now": datetime.now(UTC)}
    manager = DesktopUpdates(
        tmp_path / "data", tmp_path / "profile", release, reader=reader, now=lambda: clock["now"]
    )
    manager.configure(
        UpdateConfigureInput(
            operation_id="enable_auto",
            expected_revision=0,
            values=UpdateValues(feed_url="https://updates.example.org/feed.json", automatic=True),
        )
    )
    observed["status"] = 503
    manager.start()
    try:
        async with asyncio.timeout(5):
            while not observed["requests"]:
                await asyncio.sleep(0.01)
        await settle(manager)
        assert manager.status()["last_check"]["state"] == "failed"
        assert not manager.due()
        manager.changed.set()
        await asyncio.sleep(0.05)
        assert len(observed["requests"]) == 1
        clock["now"] += timedelta(hours=25)
        observed["status"] = 200
        manager.changed.set()
        async with asyncio.timeout(5):
            while len(observed["requests"]) < 2:
                await asyncio.sleep(0.01)
        await settle(manager)
        assert manager.status()["last_check"]["state"] == "completed"
    finally:
        await manager.close()


def test_pending_network_attempt_after_crash_is_unknown_and_reconciliation_never_reissues(
    tmp_path, controlled
):
    reader, observed, release = controlled
    manager = DesktopUpdates(tmp_path / "data", tmp_path / "profile", release, reader=reader)
    manager.directory.mkdir(parents=True)
    record = {
        "kind": "check",
        "operation_id": "old_pending",
        "state": "running",
        "feed_url": "https://updates.example.org/feed.json",
        "started_at": datetime.now(UTC).isoformat(),
        "automatic": False,
        "network_events": [],
    }
    manager.file("old_pending").write_text(json.dumps(record), "utf-8")
    (manager.directory / "last-check.json").write_text(json.dumps(record), "utf-8")
    assert manager.status()["last_check"]["state"] == "unknown"
    assert manager.reconcile("old_pending")["state"] == "unknown"
    assert observed["requests"] == []


@pytest.mark.parametrize(
    "url",
    [
        "http://updates.example.org/feed.json",
        "https://user:password@example.org/feed",
        "https://example.org/feed?token=private",
        "https://127.0.0.1/feed",
    ],
)
def test_update_sources_reject_unsafe_or_credential_bearing_urls_before_io(url):
    with pytest.raises((AppError, ValidationError)):
        UpdateValues(feed_url=url)


def test_config_cas_and_identity_conflict_preserve_original_preference(tmp_path, controlled):
    reader, observed, release = controlled
    manager = DesktopUpdates(tmp_path / "data", tmp_path / "profile", release, reader=reader)
    value = UpdateConfigureInput(
        operation_id="same",
        expected_revision=0,
        values=UpdateValues(feed_url="https://updates.example.org/feed.json"),
    )
    saved = manager.configure(value)
    before = hashlib.sha256(manager.preferences_path.read_bytes()).hexdigest()
    assert manager.configure(value) == saved
    with pytest.raises(AppError):
        manager.configure(value.model_copy(update={"values": UpdateValues()}))
    with pytest.raises(AppError):
        manager.configure(value.model_copy(update={"operation_id": "stale"}))
    assert hashlib.sha256(manager.preferences_path.read_bytes()).hexdigest() == before
    assert observed["requests"] == []
