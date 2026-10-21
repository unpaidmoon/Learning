"""Private loopback Messages transport with an auditable outbound boundary.

Harness receives only a random local capability. The provider credential stays
in this module and can reach exactly one official HTTPS endpoint. Fixture mode
does not construct an upstream connection. Raw requests and provider events are
private runtime evidence; records contain identities, hashes and usage only.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import select
import socket
import struct
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

OFFICIAL_HOST = "api.deepseek.com"
OFFICIAL_PATH = "/anthropic/v1/messages"
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
)
MAX_REQUEST_BYTES = 24 * 1024 * 1024
GENERATION_MARKER = re.compile(r"\[\[learning-generation:([A-Za-z0-9_-]{1,256})\]\]")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_summary(value: Any) -> list[dict[str, Any]]:
    """Inspect supplied bytes only; no path, URL, file-id or frame resolution."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("type") in {"image", "document"}:
            source = value.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                raise ValueError("INLINE_CONTENT_REQUIRED")
            encoded = source.get("data")
            if not isinstance(encoded, str):
                raise ValueError("INVALID_INLINE_CONTENT")
            data = base64.b64decode(encoded, validate=True)
            row: dict[str, Any] = {
                "kind": value["type"],
                "source_type": "base64",
                "sha256": _sha256(data),
                "bytes": len(data),
                "mime": source.get("media_type"),
            }
            if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
                row["dimensions"] = list(struct.unpack(">II", data[16:24]))
            found.append(row)
        for nested in value.values():
            found.extend(_image_summary(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_image_summary(nested))
    return found


class OutboundLedger:
    """Exclusive, append-only dispatch accounting. Counts are never limits."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._owner = path.with_suffix(path.suffix + ".lock").open("a+b")
        if self._owner.seek(0, os.SEEK_END) == 0:
            self._owner.write(b"0")
            self._owner.flush()
        self._owner.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._owner.close()
            raise RuntimeError("BOUNDARY_LEDGER_ALREADY_OWNED") from error
        try:
            self.entries = (
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists()
                else []
            )
            self.count = sum(entry.get("event") == "dispatch_started" for entry in self.entries)
        except BaseException:
            self.close()
            raise

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("BOUNDARY_LEDGER_CLOSED")
            recorded = {"at_ns": time.time_ns(), **copy.deepcopy(entry)}
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(recorded, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.entries.append(recorded)
            return recorded

    def dispatch(self, request_id: str, generation_id: str | None) -> int:
        with self._lock:
            attempt = self.count + 1
            self.append(
                {
                    "event": "dispatch_started",
                    "request_id": request_id,
                    "generation_id": generation_id,
                    "attempt": attempt,
                    "target": f"https://{OFFICIAL_HOST}{OFFICIAL_PATH}",
                }
            )
            self.count = attempt
            return attempt

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._owner.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._owner.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._owner.fileno(), fcntl.LOCK_UN)
            self._owner.close()


class UsageObserver:
    """Preserve raw snapshots; replace cumulative fields and settle once."""

    def __init__(self, settlement_identity: str | None = None):
        self.buffer = b""
        self.raw: list[dict[str, Any]] = []
        self.values: dict[str, int | None] = {key: None for key in USAGE_FIELDS}
        self.present = dict.fromkeys(USAGE_FIELDS, False)
        self.complete = False
        self.message_stops = 0
        self.settlement_identity = settlement_identity
        self.provider_message_id: str | None = None
        self.provider_model: str | None = None
        self.post_terminal_events_ignored = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.buffer = (self.buffer + chunk).replace(b"\r\n", b"\n")
        observed: list[dict[str, Any]] = []
        while b"\n\n" in self.buffer:
            frame, self.buffer = self.buffer.split(b"\n\n", 1)
            payload = b"\n".join(
                line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:")
            )
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
            observed.append(event)
            message = event.get("message")
            if kind == "message_start" and isinstance(message, dict):
                self.provider_message_id = message.get("id")
                self.provider_model = message.get("model")
                if self.settlement_identity is None:
                    self.settlement_identity = self.provider_message_id
            usage = (
                message.get("usage")
                if kind == "message_start" and isinstance(message, dict)
                else event.get("usage")
            )
            if isinstance(usage, dict):
                self.raw.append({"event": kind, "usage": copy.deepcopy(usage)})
                for key in USAGE_FIELDS:
                    if key in usage:
                        self.present[key] = True
                        amount = usage[key]
                        self.values[key] = amount if type(amount) is int and amount >= 0 else None
            if kind == "message_stop":
                self.message_stops += 1
                self.complete = True
        return observed

    def result(self) -> dict[str, Any]:
        return {
            "source": "provider_sse_before_harness_projection",
            "raw": copy.deepcopy(self.raw),
            "field_presence": dict(self.present),
            "observed_cumulative": dict(self.values),
            "normalized": dict(self.values) if self.complete else dict.fromkeys(USAGE_FIELDS),
            "settled": self.complete,
            "settlement_count": int(self.complete),
            "message_stop_events": self.message_stops,
            "settlement_identity": self.settlement_identity,
            "provider_message_id": self.provider_message_id,
            "provider_model": self.provider_model,
            "post_terminal_events_ignored": self.post_terminal_events_ignored,
        }


class MessagesBoundary:
    """One owned, loopback-only Messages service.

    ``bind(generation_id)`` authorizes a stable ID. The application prefixes a
    user text block with ``[[learning-generation:<id>]]``; the most recent user
    marker binds all provider attempts, including tool continuations. The first
    marker within that user message wins so quoted older IDs cannot replace
    the application prefix. A marker-free request uses queued prebinding for
    local transport tests. File preflights and rejected requests consume none.

    Fixture scenarios accept text_chunks, thinking_chunks, usage_start,
    usage_updates, usage_end, stop_reason, drop_after, fault, delay and
    tool_calls. ``pause_after_event='thinking_delta', gate='continue'`` pauses
    after that real SSE event until ``release_event('continue')``. The event
    condition is observable with wait_for_event; no sleep is needed to inject
    a fault. ``gate_before_terminal`` can similarly name a terminal gate.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        mode: str = "fixture",
        api_key: str | None = None,
        model: str = "deepseek-flash",
        thinking: str = "high",
        max_tokens: int = 16384,
        timeout: float = 240.0,
    ):
        if mode not in {"fixture", "official"}:
            raise ValueError("INVALID_BOUNDARY_MODE")
        if mode == "fixture" and api_key is not None:
            raise ValueError("FIXTURE_MUST_NOT_LOAD_PROVIDER_KEY")
        if mode == "official" and (not isinstance(api_key, str) or not api_key.strip()):
            raise ValueError("OFFICIAL_KEY_REQUIRED")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
            raise ValueError("INVALID_MODEL")
        if not isinstance(thinking, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", thinking):
            raise ValueError("INVALID_THINKING")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("INVALID_MAX_TOKENS")
        if type(timeout) not in {int, float} or not 0 < timeout < float("inf"):
            raise ValueError("INVALID_TIMEOUT")
        self.run_dir = Path(run_dir)
        self.private = self.run_dir / "private" / "boundary"
        self.private.mkdir(parents=True, exist_ok=True)
        self.mode, self.model, self.thinking = mode, model, thinking
        self.max_tokens, self.timeout = max_tokens, float(timeout)
        self._api_key = api_key
        self.local_token = secrets.token_urlsafe(32)
        self.ledger = OutboundLedger(self.run_dir / "boundary-ledger.jsonl")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        previous: dict[str, dict[str, Any]] = {}
        for entry in self.ledger.entries:
            if entry.get("event") in {"request_received", "request_finished"}:
                previous[entry["request_id"]] = {
                    key: value for key, value in entry.items() if key not in {"event", "at_ns"}
                }
            elif entry.get("event") == "dispatch_started" and entry["request_id"] in previous:
                previous[entry["request_id"]].update(
                    actual_outbound_attempt=entry["attempt"],
                    dispatch_ns=entry["at_ns"],
                    status="dispatch_unknown",
                )
        for request_id, row in previous.items():
            if row.get("finished_ns") is not None:
                continue
            row.update(
                status="dispatch_unknown" if row.get("actual_outbound_attempt") else "interrupted",
                recovered_from_boundary_ledger=True,
            )
            observer = UsageObserver(request_id)
            raw_path = self.private / f"{request_id}.response.sse"
            if raw_path.is_file():
                with raw_path.open("rb") as raw:
                    while chunk := raw.read(65536):
                        observer.feed(chunk)
            row["usage"] = observer.result()
        self._records = list(previous.values())
        self._bindings: deque[str] = deque()
        self._bound_generations: set[str] = set()
        self._generation_selections: dict[str, tuple[str, str]] = {}
        self._scenario: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._gates: dict[str, threading.Event] = {}
        self._active_gates: dict[str, int] = {}
        self._stop = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._requests: set[threading.Thread] = set()
        self._sockets: set[socket.socket] = set()
        self._stopped = False
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                owner._handle(self)

            def _unsupported(self) -> None:
                if owner._authorize(self):
                    path = urlsplit(self.path).path
                    if path == "/v1/files" or path.startswith("/v1/files/"):
                        owner._reject(self, 404, "INLINE_ATTACHMENTS_ONLY")
                    else:
                        owner._reject(self, 403, "UNAPPROVED_ENDPOINT")

            do_GET = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_CONNECT = do_HEAD = (
                _unsupported
            )

        class OwnedServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request: socket.socket, client_address: Any) -> None:
                def execute() -> None:
                    try:
                        request.settimeout(min(15.0, owner.timeout))
                        self.process_request_thread(request, client_address)
                    finally:
                        with owner._lock:
                            owner._requests.discard(threading.current_thread())
                            owner._sockets.discard(request)

                worker = threading.Thread(
                    target=execute, name="learning-messages-request", daemon=True
                )
                with owner._lock:
                    owner._requests.add(worker)
                    owner._sockets.add(request)
                worker.start()

            def handle_error(self, request: socket.socket, client_address: Any) -> None:
                # Transport resets at request-line read contain no application
                # failure and must not dump headers or private bodies to stderr.
                owner._event("local_transport_error")

        try:
            self._server = OwnedServer(("127.0.0.1", 0), Handler)
        except BaseException:
            self.ledger.close()
            raise
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records)

    @property
    def actual_outbound_attempts(self) -> int:
        return self.ledger.count

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._events)

    def start(self) -> MessagesBoundary:
        if self._stopped:
            raise RuntimeError("BOUNDARY_ALREADY_STOPPED")
        if self._server_thread is None:
            self._server_thread = threading.Thread(
                target=lambda: self._server.serve_forever(poll_interval=0.05),
                name="learning-messages-boundary",
                daemon=True,
            )
            self._server_thread.start()
        return self

    def bind(
        self, generation_id: str, *, model: str | None = None, thinking: str | None = None
    ) -> None:
        if not isinstance(generation_id, str) or not generation_id or len(generation_id) > 256:
            raise ValueError("INVALID_GENERATION_ID")
        with self._lock:
            if self._stopped:
                raise RuntimeError("BOUNDARY_ALREADY_STOPPED")
            selection = (model or self.model, thinking or self.thinking)
            previous = self._generation_selections.get(generation_id)
            if previous is not None and previous != selection:
                raise ValueError("GENERATION_SELECTION_IMMUTABLE")
            self._generation_selections[generation_id] = selection
            self._bindings.append(generation_id)
            self._bound_generations.add(generation_id)

    def _resolve_generation(self, request: dict[str, Any]) -> str | None:
        messages = request.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("INVALID_MESSAGES")
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            texts = (
                [content]
                if isinstance(content, str)
                else [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if isinstance(content, list)
                else []
            )
            for text in texts:
                marker = GENERATION_MARKER.search(text) if isinstance(text, str) else None
                if marker is None:
                    continue
                generation_id = marker.group(1)
                if generation_id not in self._bound_generations:
                    raise ValueError("UNBOUND_GENERATION_MARKER")
                try:
                    self._bindings.remove(generation_id)
                except ValueError:
                    pass
                return generation_id
            tool_results_only = (
                isinstance(content, list)
                and bool(content)
                and all(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content
                )
            )
            if not tool_results_only:
                if self.mode == "official":
                    raise ValueError("GENERATION_MARKER_REQUIRED")
                return self._bindings.popleft() if self._bindings else None
        if self.mode == "official":
            raise ValueError("GENERATION_MARKER_REQUIRED")
        return self._bindings.popleft() if self._bindings else None

    def set_scenario(self, scenario: dict[str, Any]) -> None:
        if self.mode != "fixture":
            raise RuntimeError("SCENARIOS_REQUIRE_FIXTURE_MODE")
        if not isinstance(scenario, dict):
            raise ValueError("INVALID_FIXTURE_SCENARIO")
        with self._lock:
            self._scenario = copy.deepcopy(scenario)
            names = []
            if scenario.get("pause_after_event") is not None:
                names.append(str(scenario.get("gate", "continue")))
            if scenario.get("gate_before_terminal"):
                names.append(str(scenario["gate_before_terminal"]))
            for name in names:
                if not self._active_gates.get(name):
                    self._gates[name] = threading.Event()

    def release_event(self, name: str) -> None:
        with self._lock:
            self._gates.setdefault(name, threading.Event()).set()

    def wait_for_event(
        self, event: str, timeout: float = 10.0, *, generation_id: str | None = None, after: int = 0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for item in self._events:
                    if (
                        item["sequence"] > after
                        and item["event"] == event
                        and (generation_id is None or item.get("generation_id") == generation_id)
                    ):
                        return copy.deepcopy(item)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    raise TimeoutError(f"BOUNDARY_EVENT_NOT_OBSERVED:{event}")
                self._condition.wait(remaining)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            streams = list(self._sockets)
            for gate in self._gates.values():
                gate.set()
        for stream in streams:
            self._interrupt_socket(stream)
        if self._server_thread is not None:
            self._server.shutdown()
            self._server_thread.join(timeout=2)
        self._server.server_close()
        deadline = time.monotonic() + 20
        with self._lock:
            workers = list(self._requests)
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise RuntimeError("OWNED_BOUNDARY_REQUESTS_STILL_RUNNING")
        self.ledger.close()
        self._api_key = None
        self._stopped = True

    def _event(
        self, name: str, row: dict[str, Any] | None = None, **details: Any
    ) -> dict[str, Any]:
        with self._condition:
            item = {
                "sequence": len(self._events) + 1,
                "event": name,
                "at_ns": time.time_ns(),
                **details,
            }
            if row is not None:
                item.update(request_id=row["request_id"], generation_id=row["generation_id"])
            self._events.append(item)
            self._condition.notify_all()
            return item

    def _update(self, row: dict[str, Any], **values: Any) -> None:
        with self._lock:
            row.update(values)

    @staticmethod
    def _interrupt_socket(stream: socket.socket | None) -> None:
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
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
        error_type = {
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
        }.get(status, "api_error" if status >= 500 else "invalid_request_error")
        body = json.dumps(
            {"type": "error", "error": {"type": error_type, "message": code}}
        ).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    def _authorize(self, handler: http.server.BaseHTTPRequestHandler) -> bool:
        hosts = handler.headers.get_all("Host", [])
        tokens = handler.headers.get_all("x-api-key", [])
        if (
            handler.client_address[0] != "127.0.0.1"
            or handler.headers.get_all("Origin", [])
            or hosts != [f"127.0.0.1:{self._server.server_port}"]
        ):
            self.ledger.append({"event": "local_auth_blocked", "reason": "host_or_origin"})
            self._reject(handler, 403, "LOCAL_HOST_OR_ORIGIN_REJECTED")
            return False
        if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.local_token):
            self.ledger.append({"event": "local_auth_blocked", "reason": "local_token"})
            self._reject(handler, 401, "LOCAL_TOKEN_REQUIRED")
            return False
        return True

    def _handle(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        if not self._authorize(handler):
            return
        parsed = urlsplit(handler.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return self._reject(handler, 403, "UNAPPROVED_ENDPOINT")
        if parsed.path == "/v1/files" or parsed.path.startswith("/v1/files/"):
            # DeepSeek Files is deliberately not proxied. The fixed provider
            # falls back to the same user-confirmed bytes in Messages.
            self.ledger.append({"event": "files_inline_fallback", "actual_outbound_attempts": 0})
            return self._reject(handler, 404, "INLINE_ATTACHMENTS_ONLY")
        if parsed.path != "/v1/messages":
            return self._reject(handler, 403, "UNAPPROVED_ENDPOINT")
        lengths = handler.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or handler.headers.get_all("Transfer-Encoding", []):
            return self._reject(handler, 400, "EXACT_CONTENT_LENGTH_REQUIRED")
        try:
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                return self._reject(handler, 413, "INVALID_BODY_LENGTH")
            if handler.headers.get_content_type() != "application/json":
                return self._reject(handler, 400, "JSON_REQUIRED")
            body = handler.rfile.read(length)
            if len(body) != length:
                return self._reject(handler, 400, "INCOMPLETE_REQUEST")
            request = json.loads(body)
            if not isinstance(request, dict) or request.get("stream") is not True:
                return self._reject(handler, 400, "STREAMING_JSON_REQUIRED")
            images = _image_summary(request)
        except (ValueError, TypeError, OSError):
            return self._reject(handler, 400, "INVALID_REQUEST_OR_INLINE_CONTENT")
        with self._lock:
            try:
                generation_id = self._resolve_generation(request)
            except ValueError:
                self.ledger.append({"event": "generation_binding_rejected"})
                return self._reject(handler, 403, "GENERATION_BINDING_REJECTED")
            scenario = copy.deepcopy(self._scenario)
            if self._scenario.get("fault_once"):
                self._scenario.pop("fault", None)
                self._scenario.pop("fault_once", None)
            if self._scenario.get("tool_calls") and not self._scenario.get("repeat_tools", False):
                self._scenario.pop("tool_calls", None)
            row: dict[str, Any] = {
                "request_id": f"boundary-{uuid.uuid4().hex}",
                "generation_id": generation_id,
                "mode": self.mode,
                "received_ns": time.time_ns(),
                "body_sha256": _sha256(body),
                "body_bytes": len(body),
                "model": request.get("model"),
                "max_tokens": request.get("max_tokens"),
                "thinking": request.get("thinking"),
                "output_config": request.get("output_config"),
                "tools": [
                    tool.get("name") for tool in request.get("tools", []) if isinstance(tool, dict)
                ],
                "images": images,
                "actual_outbound_attempt": None,
                "status": "received",
            }
            self._records.append(row)
        observer = UsageObserver(row["request_id"])
        try:
            with (self.private / f"{row['request_id']}.request.json").open("xb") as raw:
                raw.write(body)
                raw.flush()
                os.fsync(raw.fileno())
            self.ledger.append({"event": "request_received", **row})
            self._event("request_received", row)
            if self.mode == "fixture":
                self._fixture(handler, request, scenario, row, observer)
            else:
                self._forward(handler, request, body, row, observer)
        except (http.client.HTTPException, OSError, ValueError) as error:
            controlled = (
                isinstance(error, ConnectionAbortedError) and str(error) == "CONTROLLED_TRUNCATION"
            )
            self._update(
                row,
                status="fixture_truncated" if controlled else "connection_closed",
                error_type=type(error).__name__,
                fault_injected=controlled,
                cancel_observed=not controlled and not self._stop.is_set(),
            )
        finally:
            self._update(row, finished_ns=time.time_ns(), usage=observer.result())
            self.ledger.append({"event": "request_finished", **copy.deepcopy(row)})
            self._event("request_finished", row, status=row["status"])
            handler.close_connection = True

    def _gate(
        self, handler: http.server.BaseHTTPRequestHandler, row: dict[str, Any], name: str
    ) -> None:
        with self._lock:
            gate = self._gates.setdefault(name, threading.Event())
            self._active_gates[name] = self._active_gates.get(name, 0) + 1
        self._event("gate_waiting", row, gate=name)
        try:
            while not gate.wait(0.025):
                if self._stop.is_set() or self._disconnected(handler.connection):
                    raise ConnectionAbortedError("FIXTURE_CLIENT_DISCONNECTED")
            if self._stop.is_set() or self._disconnected(handler.connection):
                raise ConnectionAbortedError("FIXTURE_CLIENT_DISCONNECTED")
            self._event("gate_released", row, gate=name)
        finally:
            with self._lock:
                self._active_gates[name] -= 1

    def _observe(self, observer: UsageObserver, chunk: bytes, row: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for event in observer.feed(chunk):
            name = event.get("type", "unknown_provider_event")
            names.append(name)
            self._event(name, row)
            delta = event.get("delta")
            if name == "content_block_delta" and isinstance(delta, dict):
                kind = delta.get("type")
                if kind in {"text_delta", "thinking_delta"}:
                    names.append(kind)
                    self._event(kind, row)
                    with self._lock:
                        row.setdefault("delta_emitted_ns", []).append(time.time_ns())
            self._update(row, usage=observer.result())
        return names

    def _fixture(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        request: dict[str, Any],
        scenario: dict[str, Any],
        row: dict[str, Any],
        observer: UsageObserver,
    ) -> None:
        fault = scenario.get("fault")
        if fault == "drop_response":
            self._update(row, status="response_dropped")
            self._event("response_dropped", row)
            self._interrupt_socket(handler.connection)
            return
        if fault == "http_500":
            self._update(row, status="fixture_http_500")
            return self._reject(handler, 500, "CONTROLLED_FIXTURE_FAILURE")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        self._event("response_headers", row, status=200)
        event_count = 0
        paused = False
        with (self.private / f"{row['request_id']}.response.sse").open("xb") as raw:

            def emit(event: dict[str, Any]) -> None:
                nonlocal event_count, paused
                if self._stop.is_set() or self._disconnected(handler.connection):
                    raise ConnectionAbortedError("FIXTURE_CLIENT_DISCONNECTED")
                if scenario.get("drop_after") is not None and event_count >= scenario["drop_after"]:
                    self._event("fault_injected", row, fault="drop_after")
                    raise ConnectionAbortedError("CONTROLLED_TRUNCATION")
                payload = (
                    f"event: {event['type']}\ndata: "
                    + json.dumps(event, ensure_ascii=False)
                    + "\n\n"
                ).encode()
                handler.wfile.write(payload)
                handler.wfile.flush()
                raw.write(payload)
                raw.flush()
                names = self._observe(observer, payload, row)
                event_count += 1
                pause_after = scenario.get("pause_after_event")
                if not paused and (pause_after in names or pause_after == event_count):
                    paused = True
                    self._gate(handler, row, str(scenario.get("gate", "continue")))

            start: dict[str, Any] = {
                "id": row["request_id"],
                "type": "message",
                "role": "assistant",
                "model": request.get("model", self.model),
                "content": [],
                "stop_reason": None,
            }
            usage_start = scenario.get("usage_start", {"input_tokens": 17, "output_tokens": 0})
            if usage_start is not None:
                start["usage"] = usage_start
            emit({"type": "message_start", "message": start})
            index = 0
            for kind, pieces in (
                ("thinking", scenario.get("thinking_chunks", [])),
                ("text", scenario.get("text_chunks", ["这是", "本地测试回答。"])),
            ):
                if not pieces:
                    continue
                field = "thinking" if kind == "thinking" else "text"
                emit(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": kind, field: ""},
                    }
                )
                for piece in pieces:
                    delay = max(0.0, float(scenario.get("delay", 0.0)))
                    if delay and self._stop.wait(delay):
                        raise ConnectionAbortedError("FIXTURE_STOPPED")
                    emit(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": f"{kind}_delta", field: piece},
                        }
                    )
                emit({"type": "content_block_stop", "index": index})
                index += 1
            for tool in scenario.get("tool_calls", []):
                emit(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool.get("id", f"tool-{index}"),
                            "name": tool["name"],
                            "input": {},
                        },
                    }
                )
                emit(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tool.get("input", {})),
                        },
                    }
                )
                emit({"type": "content_block_stop", "index": index})
                index += 1
            if scenario.get("gate_before_terminal"):
                self._gate(handler, row, str(scenario["gate_before_terminal"]))
            for usage in scenario.get("usage_updates", []):
                emit({"type": "message_delta", "delta": {}, "usage": usage})
            end: dict[str, Any] = {
                "type": "message_delta",
                "delta": {
                    "stop_reason": scenario.get(
                        "stop_reason", "tool_use" if scenario.get("tool_calls") else "end_turn"
                    ),
                    "stop_sequence": None,
                },
            }
            usage_end = scenario.get("usage_end", {"output_tokens": 23})
            if usage_end is not None:
                end["usage"] = usage_end
            emit(end)
            emit({"type": "message_stop"})
            self._update(row, status="completed", terminal_emitted_ns=time.time_ns())

    def _configuration_matches(
        self, request: dict[str, Any], generation_id: str | None = None
    ) -> bool:
        with self._lock:
            model, effort = self._generation_selections.get(
                generation_id, (self.model, self.thinking)
            )
        disabled = effort in {"disabled", "off", "none"}
        thinking = request.get("thinking")
        output = request.get("output_config")
        return (
            request.get("model") == model
            and type(request.get("max_tokens")) is int
            and request["max_tokens"] == self.max_tokens
            and isinstance(thinking, dict)
            and thinking.get("type") == ("disabled" if disabled else "enabled")
            and (disabled or (isinstance(output, dict) and output.get("effort") == effort))
        )

    def _forward(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        request: dict[str, Any],
        body: bytes,
        row: dict[str, Any],
        observer: UsageObserver,
    ) -> None:
        if not self._configuration_matches(request, row.get("generation_id")):
            self._update(row, status="configuration_blocked")
            return self._reject(handler, 403, "MODEL_CONFIGURATION_MISMATCH")
        upstream = http.client.HTTPSConnection(OFFICIAL_HOST, 443, timeout=min(15.0, self.timeout))
        upstream.auto_open = 0
        transport_socket: socket.socket | None = None
        response: http.client.HTTPResponse | None = None
        done, cancelled = threading.Event(), threading.Event()
        deadline = time.monotonic() + self.timeout

        def interrupt_io() -> None:
            while not done.wait(0.025):
                timed_out = time.monotonic() >= deadline
                if timed_out or self._stop.is_set() or self._disconnected(handler.connection):
                    cancelled.set()
                    self._update(
                        row,
                        cancel_observed=not timed_out,
                        timed_out=timed_out,
                        disconnect_ns=time.time_ns(),
                    )
                    self._event("io_interrupted", row, timed_out=timed_out)
                    # Only interrupt the socket. The reader owns HTTPResponse's
                    # mutable parser and closes both parser and connection.
                    self._interrupt_socket(transport_socket)
                    return

        watcher = threading.Thread(
            target=interrupt_io, name="learning-provider-io-watch", daemon=True
        )
        watcher.start()
        try:
            upstream.connect()
            # getresponse() detaches HTTPConnection.sock for Connection: close
            # while HTTPResponse.makefile still owns active IO. Keep the exact
            # connected socket so cancellation also interrupts that parser.
            transport_socket = upstream.sock
            if cancelled.is_set() or self._stop.is_set():
                raise ConnectionAbortedError("CANCELLED_BEFORE_DISPATCH")
            if upstream.sock is not None:
                upstream.sock.settimeout(self.timeout)
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "anthropic-version": "2023-06-01",
                "x-api-key": self._api_key,
            }
            beta = handler.headers.get("anthropic-beta")
            if beta is not None and len(beta) <= 512 and re.fullmatch(r"[A-Za-z0-9_,. -]+", beta):
                headers["anthropic-beta"] = beta
            attempt = self.ledger.dispatch(row["request_id"], row["generation_id"])
            self._update(
                row,
                actual_outbound_attempt=attempt,
                dispatch_ns=time.time_ns(),
                status="dispatching",
            )
            self._event("dispatch_started", row, attempt=attempt)
            # The only operation that can send a model request. There is no
            # redirect, retry, proxy environment, destination or re-open path.
            upstream.request("POST", OFFICIAL_PATH, body=body, headers=headers)
            response = upstream.getresponse()
            self._update(row, upstream_status=response.status, response_headers_ns=time.time_ns())
            self._event("response_headers", row, status=response.status)
            if 300 <= response.status < 400:
                self._update(row, status="upstream_redirect_blocked")
                return self._reject(handler, 502, "UPSTREAM_REDIRECT_BLOCKED")
            handler.send_response(response.status)
            handler.send_header(
                "Content-Type", response.getheader("Content-Type", "application/octet-stream")
            )
            handler.send_header("Connection", "close")
            handler.end_headers()
            with (self.private / f"{row['request_id']}.response.sse").open("xb") as raw:
                while not self._stop.is_set():
                    chunk = response.read1(16384)
                    if not chunk:
                        break
                    raw.write(chunk)
                    raw.flush()
                    self._observe(observer, chunk, row)
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            self._update(row, status="completed" if observer.complete else "upstream_unsettled")
        except (http.client.HTTPException, OSError) as error:
            self._update(row, status="upstream_unknown", error_type=type(error).__name__)
            # A downstream write can fail before the watcher's next poll. Read
            # the actual peer state before our own shutdown: an upstream error
            # with a live downstream remains unknown, never an inferred cancel.
            if not self._stop.is_set() and self._disconnected(handler.connection):
                self._update(
                    row,
                    cancel_observed=True,
                    disconnect_ns=time.time_ns(),
                    cancel_evidence="reader_observed_downstream_disconnect",
                )
                self._event(
                    "downstream_disconnect_observed",
                    row,
                    evidence="reader_observed_downstream_disconnect",
                )
            self._interrupt_socket(handler.connection)
        finally:
            done.set()
            if response is not None:
                response.close()
            upstream.close()
            watcher.join(timeout=1)
