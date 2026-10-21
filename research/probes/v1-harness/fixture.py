"""Loopback-only Messages fixture and a fail-closed, counted official proxy.

This is an isolated V1 probe component, not an application service. Mode A never
constructs a network client. Mode B requires explicit credentials and records a
durable reservation immediately before its sole upstream request operation.
Neither mode follows redirects or retries. Request bodies and SSE bytes stay in
the ignored private output directory; public records contain hashes and usage.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import secrets
import select
import socket
import struct
import sys
import threading
import time
import traceback
from typing import Any
from urllib.parse import urlsplit
import uuid


OFFICIAL_HOST = "api.deepseek.com"
OFFICIAL_PATH = "/anthropic/v1/messages"
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_summary(value: Any) -> list[dict[str, Any]]:
    """Only decode explicitly submitted base64 image blocks, never file paths."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("type") == "image":
            source = value.get("source", {})
            row: dict[str, Any] = {"source_type": source.get("type")}
            if source.get("type") == "base64":
                data = base64.b64decode(source.get("data", ""), validate=True)
                row.update(sha256=digest(data), bytes=len(data), mime=source.get("media_type"))
                if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
                    row["dimensions"] = list(struct.unpack(">II", data[16:24]))
            elif source.get("type") == "file":
                row["file_id"] = source.get("file_id")
            found.append(row)
        for nested in value.values():
            found.extend(image_summary(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(image_summary(nested))
    return found


class BudgetLedger:
    """Append-only fsync ledger; reload reservations so restarting cannot reset it."""

    def __init__(self, path: Path, limit: int | None, authorization_ref: str | None = None):
        if limit is None:
            if not isinstance(authorization_ref, str) or not authorization_ref.strip():
                raise ValueError("UNLIMITED_REQUIRES_AUTHORIZATION_REFERENCE")
        elif type(limit) is not int or not 0 <= limit <= 6:
            raise ValueError("REAL_ATTEMPT_LIMIT_MUST_BE_0_TO_6")
        self.path, self.limit = path, limit
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._owner_lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        if self._owner_lock.seek(0, os.SEEK_END) == 0:
            self._owner_lock.write(b"0")
            self._owner_lock.flush()
        self._owner_lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._owner_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._owner_lock.close()
            raise RuntimeError("BUDGET_LEDGER_ALREADY_OWNED") from error
        self.count = 0
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if entry.get("event") == "upstream_reserved":
                        self.count += 1
            if limit is not None and self.count > limit:
                raise ValueError("EXISTING_ATTEMPTS_EXCEED_LIMIT")
            if limit is None:
                self.append({"event": "authorization_scope", "attempt_limit": None,
                             "authorization_ref": authorization_ref})
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._owner_lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._owner_lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner_lock.fileno(), fcntl.LOCK_UN)
            self._owner_lock.close()

    def append(self, entry: dict[str, Any]) -> None:
        with self.lock:
            if self._closed:
                raise RuntimeError("BUDGET_LEDGER_CLOSED")
            self._append_owned(entry)

    def _append_owned(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"at_ns": time.time_ns(), **entry}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, request_id: str) -> int:
        with self.lock:
            if self.limit is not None and self.count >= self.limit:
                self.append({"event": "budget_blocked", "request_id": request_id, "limit": self.limit})
                raise RuntimeError("REAL_MODEL_BUDGET_EXHAUSTED")
            number = self.count + 1
            self.append({"event": "upstream_reserved", "request_id": request_id,
                         "attempt": number, "target": f"https://{OFFICIAL_HOST}{OFFICIAL_PATH}"})
            self.count = number
            return number


class UsageObserver:
    """Track cumulative values by replacement and settle once at message_stop."""

    def __init__(self, settlement_identity: str | None = None):
        self.buffer = b""
        self.raw: list[dict[str, Any]] = []
        self.values: dict[str, int | None] = {key: None for key in USAGE_FIELDS}
        self.present: dict[str, bool] = {key: False for key in USAGE_FIELDS}
        self.complete = False
        self.message_stops = 0
        self.settlement_identity = settlement_identity
        self.provider_message_id = None
        self.post_terminal_events_ignored = 0

    def feed(self, chunk: bytes) -> None:
        self.buffer = (self.buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            frame, self.buffer = self.buffer.split(b"\n\n", 1)
            payload = b"\n".join(line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:"))
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if self.complete:
                if kind == "message_stop":
                    self.message_stops += 1
                else:
                    self.post_terminal_events_ignored += 1
                continue
            if kind == "message_start" and isinstance(event.get("message"), dict):
                self.provider_message_id = event["message"].get("id", self.provider_message_id)
                if self.settlement_identity is None:
                    self.settlement_identity = self.provider_message_id
            usage = event.get("message", {}).get("usage") if kind == "message_start" else event.get("usage")
            if isinstance(usage, dict):
                self.raw.append({"event": kind, "usage": usage})
                for key in USAGE_FIELDS:
                    if key in usage:
                        self.present[key] = True
                        amount = usage[key]
                        self.values[key] = amount if type(amount) is int and amount >= 0 else None
            if kind == "message_stop":
                self.message_stops += 1
                self.complete = True

    def result(self) -> dict[str, Any]:
        return {"source": "provider_sse_observed_before_harness_projection", "raw": copy.deepcopy(self.raw),
                "field_presence": dict(self.present), "observed_cumulative": dict(self.values),
                "normalized": dict(self.values) if self.complete else {key: None for key in USAGE_FIELDS},
                "settled": self.complete, "settlement_count": 1 if self.complete else 0,
                "message_stop_events": self.message_stops, "settlement_identity": self.settlement_identity,
                "provider_message_id": self.provider_message_id,
                "post_terminal_events_ignored": self.post_terminal_events_ignored}


class BoundaryServer:
    """Single test-owned loopback server. Call set_scenario before each A prompt.

    A scenario keys: text_chunks, thinking_chunks, tool_calls [{name,input,id}],
    delay, hold_seconds, usage_start, usage_end, usage_updates, drop_after (number
    of SSE events), fault (drop_response/http_500/file_expired), stop_reason.
    Defaults return a valid short text response. A file_expired fault returns a
    400 file-not-found error once; its next model attempt uses the normal script.
    B accepts only POST /v1/messages with model deepseek-flash, high reasoning and
    max_tokens<=8192; it strips all caller authentication and supplies its own.
    """

    def __init__(self, output_dir: str | Path, mode: str = "A", limit: int | None = 0,
                 key: str | None = None, authorization_ref: str | None = None):
        if mode not in {"A", "B"}:
            raise ValueError("INVALID_BOUNDARY_MODE")
        if mode == "A" and (key is not None or limit != 0):
            raise ValueError("A_MUST_NOT_LOAD_KEY_OR_REAL_BUDGET")
        if mode == "B" and (not key or (limit is not None and not 1 <= limit <= 6)):
            raise ValueError("B_REQUIRES_EXPLICIT_KEY_AND_LIMIT")
        self.output_dir = Path(output_dir)
        self.private = self.output_dir / "private" / "boundary"
        self.private.mkdir(parents=True, exist_ok=True)
        self.mode, self.limit, self._key = mode, limit, key
        self.local_token = secrets.token_urlsafe(32)
        self.ledger = BudgetLedger(self.output_dir / "boundary-ledger.jsonl", limit, authorization_ref)
        self._records: list[dict[str, Any]] = []
        self._file_records: list[dict[str, Any]] = []
        self._files: dict[str, dict[str, Any]] = {}
        self._next_file = 1
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._scenario: dict[str, Any] = {}
        self._scenario_local_count = 0
        self._active_sockets: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._request_threads: set[threading.Thread] = set()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_POST(self):
                owner._serve_request(self, owner._handle)

            def do_GET(self):
                owner._serve_request(self, self._control)

            def _control(self, handler):
                if not owner._authorize(self):
                    return
                if urlsplit(self.path).path.startswith("/v1/files"):
                    return owner._file_request(self)
                owner._reject(self, 403, "UNAPPROVED_ENDPOINT")

            do_DELETE = do_GET
            do_PUT = do_GET
            do_CONNECT = do_GET

        class OwnedHTTPServer(http.server.ThreadingHTTPServer):
            def process_request(self, request, client_address):
                # Register before start, so stop cannot miss an accepted thread
                # that has not yet reached its request handler.
                worker = threading.Thread(target=self.process_request_thread,
                                          args=(request, client_address), daemon=True)
                with owner._lock:
                    owner._request_threads.add(worker)
                worker.start()

            def handle_error(self, request, client_address):
                error = sys.exception()
                frames = traceback.extract_tb(error.__traceback__) if error is not None else []
                if isinstance(error, ConnectionResetError) and any(frame.name == "handle_one_request" for frame in frames):
                    owner.ledger.append({"event": "peer_transport_reset", "error_type": "ConnectionResetError",
                                         "errno": error.errno, "scope": "http_request_line_read"})
                    return
                super().handle_error(request, client_address)

        self._server = OwnedHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records)

    @property
    def actual_outbound_attempts(self) -> int:
        return self.ledger.count

    @property
    def file_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._file_records)

    def start(self) -> "BoundaryServer":
        worker = threading.Thread(target=self._server.serve_forever, daemon=True)
        worker.start()
        self._threads.append(worker)
        return self

    def set_scenario(self, scenario: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if self.mode != "A":
            raise RuntimeError("SCENARIOS_ONLY_ALLOWED_IN_A")
        with self._lock:
            self._scenario = copy.deepcopy({**(scenario or {}), **kwargs})
            self._scenario_local_count = 0

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sockets = list(self._active_sockets)
        for stream in sockets:
            self._close_socket(stream)
        self._server.shutdown()
        self._server.server_close()
        for worker in self._threads:
            worker.join(timeout=2)
        deadline = time.monotonic() + 20
        with self._lock:
            request_threads = list(self._request_threads)
        for worker in request_threads:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in request_threads):
            raise RuntimeError("OWNED_BOUNDARY_REQUESTS_STILL_RUNNING")
        self.ledger.close()
        self._key = None

    def _serve_request(self, handler, action):
        worker = threading.current_thread()
        with self._lock:
            self._request_threads.add(worker)
        try:
            handler.connection.settimeout(15)
            action(handler)
        finally:
            with self._lock:
                self._request_threads.discard(worker)

    def _authorize(self, handler) -> bool:
        expected_host = f"127.0.0.1:{self._server.server_port}"
        hosts = handler.headers.get_all("Host", [])
        tokens = handler.headers.get_all("x-api-key", [])
        if handler.headers.get("Origin") is not None or hosts != [expected_host]:
            self.ledger.append({"event": "local_auth_blocked", "reason": "origin_or_host"})
            self._reject(handler, 403, "LOCAL_ORIGIN_OR_HOST_REJECTED")
            return False
        if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.local_token):
            self.ledger.append({"event": "local_auth_blocked", "reason": "local_token"})
            self._reject(handler, 401, "LOCAL_TOKEN_REQUIRED")
            return False
        return True

    @staticmethod
    def _close_socket(stream: socket.socket | None) -> None:
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _disconnected(stream: socket.socket) -> bool:
        try:
            readable, _, _ = select.select([stream], [], [], 0)
            return bool(readable) and stream.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    @staticmethod
    def _reject(handler: http.server.BaseHTTPRequestHandler, status: int, code: str) -> None:
        error_type = ("api_error" if status >= 500 else {401: "authentication_error", 403: "permission_error",
                      404: "not_found_error", 429: "rate_limit_error"}.get(status, "invalid_request_error"))
        payload = json.dumps({"type": "error", "error": {"type": error_type, "message": code}}).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
        except (OSError, ValueError):
            pass
        handler.close_connection = True

    def _update(self, row: dict[str, Any], **values: Any) -> None:
        with self._lock:
            row.update(values)

    def _handle(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        if not self._authorize(handler):
            return
        parsed = urlsplit(handler.path)
        if parsed.path.startswith("/v1/files") and not parsed.scheme and not parsed.netloc:
            return self._file_request(handler)
        if parsed.scheme or parsed.netloc or parsed.path != "/v1/messages" or parsed.query:
            self.ledger.append({"event": "endpoint_blocked", "method": "POST", "path_sha256": digest(handler.path.encode())})
            return self._reject(handler, 403, "UNAPPROVED_ENDPOINT")
        try:
            length = int(handler.headers.get("Content-Length", "-1"))
            if not 0 < length <= 24 * 1024 * 1024:
                return self._reject(handler, 413, "INVALID_BODY_LENGTH")
            handler.connection.settimeout(15)
            body = handler.rfile.read(length)
            request = json.loads(body)
            if not isinstance(request, dict) or request.get("stream") is not True:
                return self._reject(handler, 400, "STREAMING_JSON_REQUIRED")
            images = image_summary(request)
        except (ValueError, OSError):
            return self._reject(handler, 400, "INVALID_REQUEST")
        with self._lock:
            request_id = f"boundary-{uuid.uuid4().hex}"
            scenario = copy.deepcopy(self._scenario)
            self._scenario_local_count += 1
            local_number = self._scenario_local_count
            if self._scenario.get("fault_once") or self._scenario.get("fault") in {"file_expired", "file_expired_once"}:
                self._scenario.pop("fault", None)
                self._scenario.pop("fault_once", None)
            if scenario.get("tool_calls") and not scenario.get("repeat_tools", False):
                self._scenario.pop("tool_calls", None)
            for image in images:
                if image.get("source_type") == "file":
                    file = self._files.get(image.get("file_id"))
                    if file:
                        image["resolved_sha256"] = file["sha256"]
                        image["resolved_bytes"] = file["size_bytes"]
            row: dict[str, Any] = {"request_id": request_id, "mode": self.mode,
                "received_ns": time.time_ns(), "body_sha256": digest(body), "body_bytes": len(body),
                "model": request.get("model"), "max_tokens": request.get("max_tokens"),
                "thinking": request.get("thinking"), "output_config": request.get("output_config"),
                "tools": [tool.get("name") for tool in request.get("tools", [])],
                "images": images, "actual_outbound_attempt": None, "status": "received"}
            self._records.append(row)
            self._active_sockets.add(handler.connection)
        with (self.private / f"{request_id}.request.json").open("xb") as raw_request:
            raw_request.write(body)
        self.ledger.append({"event": "local_request_received", **row})
        observer = UsageObserver(row["request_id"])
        try:
            if self.mode == "A":
                local_limit = scenario.get("local_request_limit")
                if local_limit is not None and local_number > local_limit:
                    self._update(row, status="local_budget_blocked", local_request_number=local_number)
                    self.ledger.append({"event": "local_budget_blocked", "request_id": request_id,
                                        "limit": local_limit, "local_request_number": local_number,
                                        "actual_outbound_model_attempts": 0})
                    self._reject(handler, 429, "LOCAL_FIXTURE_MODEL_BUDGET_EXHAUSTED")
                else:
                    self._fixture(handler, request, scenario, row, observer)
            else:
                self._forward(handler, request, body, row, observer)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, ValueError) as error:
            injected = isinstance(error, ConnectionAbortedError) and str(error) == "CONTROLLED_TRUNCATION"
            self._update(row, status="fixture_truncated" if injected else "connection_closed",
                         error_type=type(error).__name__, disconnect_ns=time.time_ns(),
                         cancel_observed=not injected and not self._stop.is_set(), fault_injected=injected)
        finally:
            self._update(row, finished_ns=time.time_ns(), usage=observer.result())
            self.ledger.append({"event": "local_request_finished", **copy.deepcopy(row)})
            with self._lock:
                self._active_sockets.discard(handler.connection)
            handler.close_connection = True

    def _fixture(self, handler, request, scenario, row, observer):
        fault = scenario.get("fault")
        if fault == "drop_response":
            self._update(row, status="response_dropped")
            return self._close_socket(handler.connection)
        if fault == "http_500":
            self._update(row, status="fixture_http_500")
            return self._reject(handler, 500, "CONTROLLED_FIXTURE_FAILURE")
        if fault in {"file_expired", "file_expired_once"}:
            images = image_summary(request)
            file_id = next((image["file_id"] for image in images if image.get("file_id")), "file-v1")
            payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": f"{file_id} expired"}}).encode()
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            self._update(row, status="fixture_file_expired")
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        event_count = 0
        with (self.private / f"{row['request_id']}.response.sse").open("xb") as raw:
            def emit(event):
                nonlocal event_count
                if self._stop.is_set() or self._disconnected(handler.connection):
                    raise ConnectionAbortedError("FIXTURE_CLIENT_DISCONNECTED")
                if scenario.get("drop_after") is not None and event_count >= scenario["drop_after"]:
                    raise ConnectionAbortedError("CONTROLLED_TRUNCATION")
                payload = (f"event: {event['type']}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
                handler.wfile.write(payload)
                handler.wfile.flush()
                raw.write(payload)
                raw.flush()
                observer.feed(payload)
                event_count += 1
                if event["type"] == "content_block_delta":
                    with self._lock:
                        row.setdefault("delta_emitted_ns", []).append(time.time_ns())

            usage_start = scenario.get("usage_start", {"input_tokens": 17, "output_tokens": 0})
            start = {"id": row["request_id"], "type": "message", "role": "assistant", "model": request.get("model", "deepseek-flash"), "content": [], "stop_reason": None}
            if usage_start is not None:
                start["usage"] = usage_start
            emit({"type": "message_start", "message": start})
            block_index = 0
            for kind, pieces in (("thinking", scenario.get("thinking_chunks", [])),
                                 ("text", scenario.get("text_chunks", ["Fixture formula: ", "\\(1/3\\)", "."]))):
                if not pieces:
                    continue
                emit({"type": "content_block_start", "index": block_index,
                      "content_block": {"type": kind, "thinking" if kind == "thinking" else "text": ""}})
                for piece in pieces:
                    if self._stop.wait(scenario.get("delay", 0.06)):
                        raise ConnectionAbortedError("FIXTURE_STOPPED")
                    emit({"type": "content_block_delta", "index": block_index,
                          "delta": {"type": "thinking_delta" if kind == "thinking" else "text_delta", "thinking" if kind == "thinking" else "text": piece}})
                emit({"type": "content_block_stop", "index": block_index})
                block_index += 1
            for tool in scenario.get("tool_calls", []):
                emit({"type": "content_block_start", "index": block_index,
                      "content_block": {"type": "tool_use", "id": tool.get("id", f"tool-{block_index}"), "name": tool["name"], "input": {}}})
                emit({"type": "content_block_delta", "index": block_index,
                      "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool.get("input", {}))}})
                emit({"type": "content_block_stop", "index": block_index})
                block_index += 1
            until = time.monotonic() + scenario.get("hold_seconds", 0)
            while time.monotonic() < until:
                if self._stop.wait(0.05):
                    raise ConnectionAbortedError("FIXTURE_STOPPED")
                if self._disconnected(handler.connection):
                    raise ConnectionAbortedError("FIXTURE_CLIENT_DISCONNECTED")
            for usage in scenario.get("usage_updates", []):
                emit({"type": "message_delta", "delta": {}, "usage": usage})
            end = {"type": "message_delta", "delta": {"stop_reason": scenario.get("stop_reason", "tool_use" if scenario.get("tool_calls") else "end_turn"), "stop_sequence": None}}
            usage_end = scenario.get("usage_end", {"output_tokens": 23})
            if usage_end is not None:
                end["usage"] = usage_end
            emit(end)
            emit({"type": "message_stop"})
            self._update(row, status="completed", terminal_emitted_ns=time.time_ns())

    def _file_request(self, handler):
        parsed = urlsplit(handler.path)
        valid_root = parsed.path == "/v1/files"
        file_id = parsed.path.removeprefix("/v1/files/") if parsed.path.startswith("/v1/files/") else None
        if parsed.scheme or parsed.netloc or (not valid_root and (not file_id or "/" in file_id)):
            return self._reject(handler, 403, "UNAPPROVED_FILES_ENDPOINT")
        with self._lock:
            file_request_id = f"files-{uuid.uuid4().hex}"
            row = {"file_request_id": file_request_id, "method": handler.command,
                   "path": parsed.path, "mode": self.mode, "received_ns": time.time_ns(),
                   "actual_outbound_model_attempts": 0}
            self._file_records.append(row)
        self.ledger.append({"event": "local_files_request", **row})
        if self.mode == "B":
            self._update(row, status="files_not_supported_inline_fallback")
            return self._reject(handler, 404, "FILES_NOT_SUPPORTED_INLINE_ONLY")
        status, payload = 200, None
        try:
            if handler.command == "POST" and valid_root:
                length = int(handler.headers.get("Content-Length", "-1"))
                if not 0 < length <= 24 * 1024 * 1024:
                    return self._reject(handler, 413, "INVALID_FILE_BODY_LENGTH")
                body = handler.rfile.read(length)
                content_type = handler.headers.get("Content-Type", "")
                message = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                part = next((part for part in message.iter_parts()
                             if part.get_param("name", header="content-disposition") == "file"), None)
                if part is None:
                    return self._reject(handler, 400, "MULTIPART_FILE_REQUIRED")
                data = part.get_payload(decode=True)
                if not isinstance(data, bytes):
                    return self._reject(handler, 400, "INVALID_FILE_PAYLOAD")
                with self._lock:
                    new_id = f"file-v{self._next_file}"
                    self._next_file += 1
                    stored = {"id": new_id, "type": "file", "mime_type": part.get_content_type(),
                              "size_bytes": len(data), "created_at": datetime.now(timezone.utc).isoformat(),
                              "filename": part.get_filename() or "fixture.png", "sha256": digest(data)}
                    self._files[new_id] = stored
                with (self.private / f"{file_request_id}.upload").open("xb") as raw_upload:
                    raw_upload.write(data)
                self._update(row, file_id=new_id, sha256=stored["sha256"], bytes=len(data), status="created")
                payload = {key: value for key, value in stored.items() if key != "sha256"}
            elif handler.command == "GET" and valid_root:
                with self._lock:
                    items = [{key: value for key, value in file.items() if key != "sha256"} for file in self._files.values()]
                payload = {"data": items, "has_more": False, "first_id": items[0]["id"] if items else None,
                           "last_id": items[-1]["id"] if items else None}
            elif handler.command == "GET" and file_id in self._files:
                payload = {key: value for key, value in self._files[file_id].items() if key != "sha256"}
            elif handler.command == "DELETE" and file_id in self._files:
                with self._lock:
                    self._files.pop(file_id)
                payload = {"id": file_id, "type": "file_deleted"}
            else:
                status = 404
                payload = {"type": "error", "error": {"type": "not_found_error", "message": "fixture file not found"}}
        except (ValueError, OSError, StopIteration):
            return self._reject(handler, 400, "INVALID_FILES_REQUEST")
        encoded = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(encoded)
        self._update(row, status_code=status, finished_ns=time.time_ns())
        handler.close_connection = True

    def _forward(self, handler, request, body, row, observer):
        # Fixed host/path, no environment proxy, redirects, forwarded credential,
        # files endpoint, retry, or caller-provided destination is supported.
        if request.get("model") != "deepseek-flash":
            self._update(row, status="configuration_blocked")
            return self._reject(handler, 403, "UNAPPROVED_MODEL")
        if type(request.get("max_tokens")) is not int or not 1 <= request["max_tokens"] <= 8192:
            self._update(row, status="configuration_blocked")
            return self._reject(handler, 403, "UNAPPROVED_OUTPUT_LIMIT")
        effort = request.get("output_config", {}).get("effort")
        if effort != "high":
            self._update(row, status="configuration_blocked")
            return self._reject(handler, 403, "UNAPPROVED_REASONING_EFFORT")
        if request.get("thinking", {}).get("type") != "enabled":
            self._update(row, status="configuration_blocked")
            return self._reject(handler, 403, "THINKING_MUST_BE_ENABLED")
        try:
            attempt = self.ledger.reserve(row["request_id"])
        except RuntimeError:
            self._update(row, status="budget_blocked")
            return self._reject(handler, 429, "REAL_MODEL_BUDGET_EXHAUSTED")
        self._update(row, actual_outbound_attempt=attempt, dispatch_ns=time.time_ns(), status="dispatching")
        upstream = http.client.HTTPSConnection(OFFICIAL_HOST, 443, timeout=15)
        # A cancellation closes sock and may set it to None. Prevent send() from
        # silently opening a fresh socket after that cancellation.
        upstream.auto_open = 0
        done = threading.Event()
        cancelled = threading.Event()
        response = None

        def cancel_watcher():
            while not done.wait(0.05):
                if self._stop.is_set() or self._disconnected(handler.connection):
                    cancelled.set()
                    self._update(row, cancel_observed=True, disconnect_ns=time.time_ns())
                    self._close_socket(upstream.sock)
                    # Interrupt IO only. HTTPResponse and HTTPConnection own
                    # mutable chunk-parser state; closing them from here races
                    # read1() and can clear response.fp mid-parse. The reader's
                    # finally block exclusively closes those parser objects.
                    return

        watcher = threading.Thread(target=cancel_watcher, daemon=True)
        watcher.start()
        try:
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                       "anthropic-version": "2023-06-01", "x-api-key": self._key}
            beta = handler.headers.get("anthropic-beta")
            if beta:
                headers["anthropic-beta"] = beta
            upstream.connect()
            if cancelled.is_set():
                raise ConnectionAbortedError("CLIENT_CANCELLED_BEFORE_DISPATCH")
            if upstream.sock is not None:
                upstream.sock.settimeout(180)
            if cancelled.is_set() or self._stop.is_set():
                raise ConnectionAbortedError("CLIENT_CANCELLED_BEFORE_REQUEST")
            upstream.request("POST", OFFICIAL_PATH, body=body, headers=headers)
            response = upstream.getresponse()
            self._update(row, upstream_status=response.status, response_headers_ns=time.time_ns())
            handler.send_response(response.status)
            handler.send_header("Content-Type", response.getheader("Content-Type", "application/octet-stream"))
            handler.send_header("Connection", "close")
            handler.end_headers()
            with (self.private / f"{row['request_id']}.response.sse").open("xb") as raw:
                while not self._stop.is_set():
                    chunk = response.read1(16384)
                    if not chunk:
                        break
                    raw.write(chunk)
                    raw.flush()
                    observer.feed(chunk)
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    with self._lock:
                        row.setdefault("forwarded_chunk_ns", []).append(time.time_ns())
            self._update(row, status="completed" if observer.complete else "upstream_unsettled")
        except (http.client.HTTPException, OSError) as error:
            self._update(row, status="upstream_unknown", error_type=type(error).__name__)
            self._close_socket(handler.connection)
        finally:
            done.set()
            upstream.close()
            watcher.join(timeout=1)
