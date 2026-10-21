"""Deterministic account-header transport tests; no official network calls."""

import http.client
import io
import json
from urllib.parse import urlsplit

from learning_app.execution.boundary import MessagesBoundary

TOKEN = "synthetic-private-account-grant"


def local_request(boundary, path, *, method="GET", body=None, headers=None):
    endpoint = urlsplit(boundary.url)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {"x-dsh-auth-token": TOKEN})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class Response:
    status = 200

    def __init__(self, body):
        self.body = io.BytesIO(body)

    def read(self, amount):
        return self.body.read(amount)

    def read1(self, amount):
        return self.body.read(amount)

    def getheader(self, _name, default=None):
        return "text/event-stream" if default else default

    def close(self):
        self.body.close()


def upstream_double(monkeypatch, body):
    calls = []

    class Upstream:
        def __init__(self, host, port, timeout):
            assert host == "api.deepseek.com" and port == 443
            self.sock = None

        def connect(self):
            pass

        def request(self, method, path, body=None, headers=None):
            calls.append((method, path, body, dict(headers or {})))

        def getresponse(self):
            return Response(body)

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Upstream)
    return calls


def test_account_catalog_forwards_header_only_and_does_not_count_model_attempt(
    tmp_path, monkeypatch
):
    calls = upstream_double(
        monkeypatch,
        json.dumps(
            {"data": [{"id": "synthetic-model", "input_modalities": ["text", "image"]}]}
        ).encode(),
    )
    boundary = MessagesBoundary(
        tmp_path, mode="official", credential_route="account", account_enabled=True
    ).start()
    try:
        status, _body = local_request(boundary, "/models")
        assert status == 200
        assert calls[0][0:2] == ("GET", "/models")
        assert calls[0][3]["x-dsh-auth-token"] == TOKEN
        assert "x-api-key" not in calls[0][3]
        assert boundary.account_model_modalities == {"synthetic-model": ["text", "image"]}
        assert boundary.actual_outbound_attempts == 0
        assert TOKEN not in json.dumps(boundary.records)
        assert TOKEN not in boundary.ledger.path.read_text()
        assert local_request(boundary, "/models?redirect=https://evil.invalid")[0] == 403
        assert local_request(boundary, "/v1/files")[0] == 403
        assert (
            local_request(
                boundary,
                "/models",
                headers={"x-dsh-auth-token": TOKEN, "Origin": "http://127.0.0.1"},
            )[0]
            == 403
        )
        assert len(calls) == 1
    finally:
        boundary.stop()


def test_account_message_route_preserves_attempt_and_usage_without_recording_header(
    tmp_path, monkeypatch
):
    frames = [
        {
            "type": "message_start",
            "message": {
                "id": "fixture-message",
                "model": "deepseek-flash",
                "usage": {"input_tokens": 10},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    wire = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
    calls = upstream_double(monkeypatch, wire)
    boundary = MessagesBoundary(
        tmp_path, mode="official", credential_route="account", account_enabled=True
    ).start()
    try:
        boundary.bind("generation-account", model="deepseek-flash", thinking="high")
        body = json.dumps(
            {
                "model": "deepseek-flash",
                "max_tokens": 16384,
                "thinking": {"type": "enabled"},
                "output_config": {"effort": "high"},
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "[[learning-generation:generation-account]]\n合成问题",
                    }
                ],
            }
        ).encode()
        status, _body = local_request(
            boundary,
            "/v1/messages",
            method="POST",
            body=body,
            headers={"x-dsh-auth-token": TOKEN, "Content-Type": "application/json"},
        )
        assert status == 200
        assert calls[0][0:2] == ("POST", "/anthropic/v1/messages")
        assert calls[0][3]["x-dsh-auth-token"] == TOKEN
        assert "x-api-key" not in calls[0][3]
        assert boundary.actual_outbound_attempts == 1
        boundary.wait_for_event("request_finished", generation_id="generation-account")
        record = boundary.records[0]
        assert record["credential_route"] == "account"
        assert record["usage"]["settled"] is True
        assert record["usage"]["normalized"]["input_tokens"] == 10
        assert record["usage"]["field_presence"]["reasoning_tokens"] is False
        assert TOKEN not in json.dumps(record)
        assert TOKEN not in boundary.ledger.path.read_text()
        assert TOKEN not in (boundary.private / f"{record['request_id']}.request.json").read_text()
    finally:
        boundary.stop()


def test_account_route_does_not_accept_api_key_fallback_or_unbound_generation(
    tmp_path, monkeypatch
):
    calls = upstream_double(monkeypatch, b"{}")
    boundary = MessagesBoundary(
        tmp_path,
        mode="official",
        api_key="synthetic-api-key",
        credential_route="account",
        account_enabled=True,
    ).start()
    try:
        body = json.dumps(
            {
                "stream": True,
                "messages": [{"role": "user", "content": "[[learning-generation:unknown]]"}],
            }
        ).encode()
        assert (
            local_request(
                boundary,
                "/v1/messages",
                method="POST",
                body=body,
                headers={"x-api-key": boundary.local_token, "Content-Type": "application/json"},
            )[0]
            == 403
        )
        assert (
            local_request(
                boundary,
                "/v1/messages",
                method="POST",
                body=body,
                headers={"x-dsh-auth-token": TOKEN, "Content-Type": "application/json"},
            )[0]
            == 403
        )
        assert boundary.actual_outbound_attempts == 0
        assert calls == []
    finally:
        boundary.stop()
