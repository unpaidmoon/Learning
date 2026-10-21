"""Transport tests use loopback HTTP and deterministic provider doubles only."""

from __future__ import annotations

import base64
import http.client
import http.server
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from learning_app.execution.boundary import (
    OFFICIAL_HOST,
    OFFICIAL_PATH,
    MessagesBoundary,
    OutboundLedger,
    UsageObserver,
)


def frame(event: dict) -> bytes:
    return ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()


def request_body(*, generation_id: str | None = None, **overrides) -> dict:
    prefix = f"[[learning-generation:{generation_id}]]\n" if generation_id else ""
    return {
        "model": "deepseek-flash",
        "max_tokens": 16384,
        "thinking": {"type": "enabled"},
        "output_config": {"effort": "high"},
        "stream": True,
        "messages": [{"role": "user", "content": prefix + "synthetic question"}],
        **overrides,
    }


def without_watcher_polling(watcher_waiting: threading.Event):
    """Make the reader win deterministically; no time-based race guessing."""

    class NoPollEvent(threading.Event):
        def wait(self, timeout=None):
            if threading.current_thread().name == "learning-provider-io-watch" and timeout == 0.025:
                watcher_waiting.set()
                return super().wait()
            return super().wait(timeout)

    return NoPollEvent


class RunningRequest:
    def __init__(
        self,
        boundary: MessagesBoundary,
        body: dict | None = None,
        *,
        headers: dict | None = None,
        path: str = "/v1/messages",
        method: str = "POST",
    ):
        parsed = urlsplit(boundary.url)
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        self.socket: socket.socket | None = None
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.status: int | None = None
        self.body = b""
        self.error: BaseException | None = None
        self.interrupted = False

        def run() -> None:
            response = None
            try:
                data = json.dumps(body if body is not None else request_body()).encode()
                self.connection.request(
                    method,
                    path,
                    data,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": boundary.local_token,
                        **(headers or {}),
                    },
                )
                self.socket = self.connection.sock
                response = self.connection.getresponse()
                self.status = response.status
                self.ready.set()
                self.body = response.read()
            except BaseException as error:
                if not (self.interrupted and isinstance(error, OSError)):
                    self.error = error
                self.ready.set()
            finally:
                if response is not None:
                    response.close()
                self.connection.close()
                self.finished.set()

        self.thread = threading.Thread(target=run, name="test-local-http-reader", daemon=True)
        self.thread.start()

    def join(self) -> RunningRequest:
        if not self.finished.wait(7):
            raise AssertionError("local HTTP request did not finish")
        self.thread.join(timeout=1)
        if self.error is not None:
            raise self.error
        return self

    def interrupt(self) -> None:
        if not self.ready.wait(3):
            raise AssertionError("local HTTP headers not received")
        assert self.socket is not None
        self.interrupted = True
        self.socket.shutdown(socket.SHUT_RDWR)


class ProviderSocket:
    def __init__(self):
        self.interrupted = threading.Event()
        self.timeout = None
        self.interrupt_threads: list[str] = []

    def shutdown(self, how: int) -> None:
        self.interrupt_threads.append(threading.current_thread().name)
        self.interrupted.set()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class ProviderResponse:
    def __init__(
        self,
        transport: ProviderConnection,
        *,
        status: int = 200,
        chunks: list[bytes] | None = None,
        wait_for_interrupt: bool = False,
    ):
        self.transport = transport
        self.status = status
        self.chunks = list(chunks or [])
        self.wait_for_interrupt = wait_for_interrupt
        self.read_entered = threading.Event()
        self.closed_on: str | None = None

    def getheader(self, name: str, default=None):
        return "text/event-stream" if name.lower() == "content-type" else default

    def read1(self, size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.wait_for_interrupt:
            self.read_entered.set()
            if not self.transport.sock.interrupted.wait(4):
                raise TimeoutError("test provider IO was not interrupted")
            raise ConnectionAbortedError("test provider socket interrupted")
        return b""

    def close(self) -> None:
        self.closed_on = threading.current_thread().name


class ProviderConnection:
    def __init__(
        self,
        *,
        status: int = 200,
        chunks: list[bytes] | None = None,
        wait_for_interrupt: bool = False,
    ):
        self.sock = ProviderSocket()
        self.auto_open = 1
        self.response = ProviderResponse(
            self, status=status, chunks=chunks, wait_for_interrupt=wait_for_interrupt
        )
        self.requests: list[dict] = []
        self.closed_on: str | None = None

    def connect(self) -> None:
        pass

    def request(self, method: str, path: str, *, body: bytes, headers: dict) -> None:
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

    def getresponse(self) -> ProviderResponse:
        return self.response

    def close(self) -> None:
        self.closed_on = threading.current_thread().name


class UsageObserverTests(unittest.TestCase):
    def test_cumulative_replacement_missing_fields_and_single_settlement(self):
        observer = UsageObserver("attempt-identity")
        data = b"".join(
            [
                frame(
                    {
                        "type": "message_start",
                        "message": {
                            "id": "provider-message",
                            "model": "chosen",
                            "usage": {"input_tokens": 13, "output_tokens": 0},
                        },
                    }
                ),
                frame({"type": "message_delta", "usage": {"output_tokens": 2}}),
                frame({"type": "message_delta", "usage": {"output_tokens": 7}}),
                frame({"type": "message_stop"}),
                frame({"type": "message_stop"}),
                frame({"type": "message_delta", "usage": {"output_tokens": 100}}),
            ]
        )
        # Exercise incomplete frames and CRLF split between reads.
        data = data.replace(b"\n", b"\r\n")
        for byte in data:
            observer.feed(bytes([byte]))
        result = observer.result()
        self.assertEqual(result["normalized"]["output_tokens"], 7)
        self.assertEqual(result["normalized"]["input_tokens"], 13)
        self.assertIsNone(result["normalized"]["reasoning_tokens"])
        self.assertFalse(result["field_presence"]["reasoning_tokens"])
        self.assertEqual(result["message_stop_events"], 2)
        self.assertEqual(result["settlement_count"], 1)
        self.assertEqual(len(result["raw"]), 3)
        self.assertEqual(result["post_terminal_events_ignored"], 1)
        self.assertEqual(result["settlement_identity"], "attempt-identity")

    def test_unsettled_usage_keeps_observed_snapshot_but_no_final_values(self):
        observer = UsageObserver()
        observer.feed(
            frame(
                {
                    "type": "message_start",
                    "message": {
                        "id": "id",
                        "usage": {"input_tokens": 0, "output_tokens": True, "reasoning_tokens": -1},
                    },
                }
            )
        )
        result = observer.result()
        self.assertEqual(result["observed_cumulative"]["input_tokens"], 0)
        self.assertIsNone(result["observed_cumulative"]["output_tokens"])
        self.assertIsNone(result["observed_cumulative"]["reasoning_tokens"])
        self.assertTrue(result["field_presence"]["output_tokens"])
        self.assertTrue(all(value is None for value in result["normalized"].values()))
        self.assertFalse(result["settled"])


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learning-boundary-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def boundary(self, **kwargs) -> MessagesBoundary:
        boundary = MessagesBoundary(self.root, **kwargs).start()
        self.addCleanup(boundary.stop)
        return boundary

    def test_fixture_streams_actual_events_and_waits_at_explicit_gate_without_network(self):
        with patch(
            "learning_app.execution.boundary.http.client.HTTPSConnection",
            side_effect=AssertionError("fixture attempted provider connection"),
        ):
            boundary = self.boundary()
            boundary.bind("generation-stream")
            boundary.set_scenario(
                {
                    "thinking_chunks": ["reason"],
                    "text_chunks": ["first", "second"],
                    "pause_after_event": "text_delta",
                    "gate": "resume",
                }
            )
            request = RunningRequest(boundary)
            boundary.wait_for_event("gate_waiting", generation_id="generation-stream")
            self.assertFalse(request.finished.is_set())
            self.assertFalse(any(event["event"] == "message_stop" for event in boundary.events))
            boundary.release_event("resume")
            response = request.join()
            boundary.wait_for_event("request_finished", generation_id="generation-stream")
        self.assertEqual(response.status, 200)
        self.assertIn(b'"text": "first"', response.body)
        self.assertIn(b'"text": "second"', response.body)
        self.assertEqual(boundary.actual_outbound_attempts, 0)
        row = boundary.records[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["generation_id"], "generation-stream")
        self.assertEqual(row["usage"]["normalized"]["output_tokens"], 23)
        self.assertEqual(len(row["delta_emitted_ns"]), 3)

    def test_fixture_cancellation_retains_partial_events_and_unknown_usage(self):
        boundary = self.boundary()
        boundary.bind("generation-cancel")
        boundary.set_scenario(
            {
                "thinking_chunks": ["active reasoning"],
                "text_chunks": ["partial answer"],
                "pause_after_event": "text_delta",
                "gate": "never-release",
            }
        )
        request = RunningRequest(boundary)
        boundary.wait_for_event("gate_waiting", generation_id="generation-cancel")
        request.interrupt()
        request.join()
        boundary.wait_for_event("request_finished", generation_id="generation-cancel")
        row = boundary.records[0]
        self.assertTrue(row["cancel_observed"])
        self.assertFalse(row["usage"]["settled"])
        self.assertTrue(all(value is None for value in row["usage"]["normalized"].values()))
        evidence = (boundary.private / f"{row['request_id']}.response.sse").read_bytes()
        self.assertIn(b"partial answer", evidence)
        self.assertNotIn(b"message_stop", evidence)

    def test_fixture_missing_usage_is_unknown_after_terminal(self):
        boundary = self.boundary()
        boundary.set_scenario({"usage_start": None, "usage_end": None})
        RunningRequest(boundary).join()
        boundary.wait_for_event("request_finished")
        usage = boundary.records[0]["usage"]
        self.assertTrue(usage["settled"])
        self.assertEqual(usage["raw"], [])
        self.assertFalse(any(usage["field_presence"].values()))
        self.assertTrue(all(value is None for value in usage["normalized"].values()))

    def test_host_origin_and_capability_fail_closed(self):
        boundary = self.boundary()
        for headers, status in [
            ({"x-api-key": "wrong"}, 401),
            ({"Host": "evil.example"}, 403),
            ({"Origin": "http://127.0.0.1"}, 403),
            ({"Origin": "null"}, 403),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(RunningRequest(boundary, headers=headers).join().status, status)
        self.assertEqual(boundary.records, [])
        self.assertEqual(boundary.actual_outbound_attempts, 0)

    def test_duplicate_host_or_token_and_chunked_body_are_rejected(self):
        boundary = self.boundary()
        parsed = urlsplit(boundary.url)
        for extra, expected in [
            (f"Host: 127.0.0.1:{parsed.port}\r\n", 403),
            (f"x-api-key: {boundary.local_token}\r\n", 401),
            ("Transfer-Encoding: chunked\r\n", 400),
        ]:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as connection:
                data = (
                    f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{parsed.port}\r\n"
                    f"x-api-key: {boundary.local_token}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: 2\r\n{extra}\r\n{{}}"
                ).encode()
                connection.sendall(data)
                self.assertIn(f" {expected} ".encode(), connection.recv(4096).split(b"\r\n")[0])
        self.assertEqual(boundary.records, [])

    def test_unapproved_endpoint_and_remote_image_never_resolve(self):
        boundary = self.boundary()
        self.assertEqual(
            RunningRequest(boundary, path="http://example.test/v1/messages").join().status, 403
        )
        self.assertEqual(RunningRequest(boundary, path="/v1/messages?forward=1").join().status, 403)
        image = {"type": "image", "source": {"type": "url", "url": "http://example.test/frame.png"}}
        self.assertEqual(
            RunningRequest(boundary, request_body(messages=[{"role": "user", "content": [image]}]))
            .join()
            .status,
            400,
        )
        self.assertEqual(boundary.actual_outbound_attempts, 0)
        self.assertEqual(boundary.records, [])

    def test_files_fallback_does_not_consume_generation_binding(self):
        boundary = self.boundary()
        boundary.bind("image-generation")
        self.assertEqual(RunningRequest(boundary, path="/v1/files").join().status, 404)
        image_data = b"user-confirmed-image-bytes"
        image = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(image_data).decode(),
            },
        }
        RunningRequest(
            boundary, request_body(messages=[{"role": "user", "content": [image]}])
        ).join()
        boundary.wait_for_event("request_finished")
        row = boundary.records[0]
        self.assertEqual(row["generation_id"], "image-generation")
        self.assertEqual(row["images"][0]["bytes"], len(image_data))
        self.assertNotIn("data", row["images"][0])
        self.assertNotIn(image_data, boundary.ledger.path.read_bytes())

    def test_fault_injection_is_observable_and_does_not_retry(self):
        boundary = self.boundary()
        boundary.bind("truncated")
        boundary.set_scenario({"text_chunks": ["one", "two"], "drop_after": 3})
        request = RunningRequest(boundary).join()
        boundary.wait_for_event("fault_injected", generation_id="truncated")
        boundary.wait_for_event("request_finished", generation_id="truncated")
        self.assertEqual(request.status, 200)
        self.assertEqual(len(boundary.records), 1)
        self.assertEqual(boundary.records[0]["status"], "fixture_truncated")
        self.assertFalse(boundary.records[0]["usage"]["settled"])

    def test_markers_bind_interleaved_requests_and_repeated_provider_attempts(self):
        boundary = self.boundary()
        boundary.bind("first-generation")
        boundary.bind("second-generation")
        first = {"role": "user", "content": "[[learning-generation:first-generation]] first"}
        second = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[[learning-generation:second-generation]] second quotes [[learning-generation:first-generation]]",
                }
            ],
        }
        RunningRequest(boundary, request_body(messages=[first, second])).join()
        boundary.wait_for_event("request_finished", generation_id="second-generation")
        RunningRequest(boundary, request_body(messages=[first])).join()
        boundary.wait_for_event("request_finished", generation_id="first-generation")
        RunningRequest(
            boundary,
            request_body(
                messages=[
                    first,
                    second,
                    {"role": "assistant", "content": "tool call"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}
                        ],
                    },
                ]
            ),
        ).join()
        self.assertEqual(
            [row["generation_id"] for row in boundary.records],
            ["second-generation", "first-generation", "second-generation"],
        )

    def test_unbound_marker_is_rejected_without_consuming_valid_binding(self):
        boundary = self.boundary()
        boundary.bind("allowed")
        result = RunningRequest(
            boundary,
            request_body(
                messages=[
                    {"role": "user", "content": "[[learning-generation:unbound]] not permitted"}
                ]
            ),
        ).join()
        self.assertEqual(result.status, 403)
        RunningRequest(boundary).join()
        boundary.wait_for_event("request_finished", generation_id="allowed")
        self.assertEqual(len(boundary.records), 1)

    def test_changing_scenario_preserves_an_active_named_gate(self):
        boundary = self.boundary()
        boundary.bind("first")
        boundary.set_scenario({"pause_after_event": "text_delta", "gate": "first-gate"})
        first = RunningRequest(boundary)
        boundary.wait_for_event("gate_waiting", generation_id="first")
        boundary.set_scenario({"text_chunks": ["new request"]})
        boundary.bind("second")
        RunningRequest(boundary).join()
        boundary.wait_for_event("request_finished", generation_id="second")
        self.assertFalse(first.finished.is_set())
        boundary.release_event("first-gate")
        first.join()
        boundary.wait_for_event("request_finished", generation_id="first")

    def test_official_dispatch_is_fixed_counted_and_uses_explicit_configuration(self):
        terminal = (
            frame(
                {
                    "type": "message_start",
                    "message": {
                        "id": "official-msg",
                        "model": "selected-model",
                        "usage": {"input_tokens": 3},
                    },
                }
            )
            + frame({"type": "message_delta", "usage": {"output_tokens": 5}})
            + frame({"type": "message_stop"})
        )
        transports: list[ProviderConnection] = []

        def factory(*args, **kwargs):
            self.assertEqual(args, (OFFICIAL_HOST, 443))
            self.assertEqual(kwargs["timeout"], 15.0)
            transport = ProviderConnection(chunks=[terminal])
            transports.append(transport)
            return transport

        boundary = self.boundary(
            mode="official",
            api_key="synthetic-provider-secret",
            model="selected-model",
            thinking="medium",
            max_tokens=32768,
            timeout=300,
        )
        with patch(
            "learning_app.execution.boundary.http.client.HTTPSConnection", side_effect=factory
        ):
            # More than six requests verifies that the historical experiment
            # ceiling does not survive as a product limit.
            for index in range(8):
                boundary.bind(f"generation-{index}")
                RunningRequest(
                    boundary,
                    request_body(
                        generation_id=f"generation-{index}",
                        model="selected-model",
                        max_tokens=32768,
                        output_config={"effort": "medium"},
                    ),
                    headers={"Authorization": "Bearer caller-secret"},
                ).join()
                boundary.wait_for_event("request_finished", generation_id=f"generation-{index}")
        self.assertEqual(boundary.actual_outbound_attempts, 8)
        self.assertEqual(
            [row["actual_outbound_attempt"] for row in boundary.records], list(range(1, 9))
        )
        for transport in transports:
            self.assertEqual(transport.auto_open, 0)
            self.assertEqual(len(transport.requests), 1)
            sent = transport.requests[0]
            self.assertEqual(sent["path"], OFFICIAL_PATH)
            self.assertEqual(sent["headers"]["x-api-key"], "synthetic-provider-secret")
            self.assertNotIn("Authorization", sent["headers"])
            self.assertEqual(transport.sock.timeout, 300)
        self.assertNotIn(b"synthetic-provider-secret", boundary.ledger.path.read_bytes())
        self.assertNotIn(b"caller-secret", boundary.ledger.path.read_bytes())

    def test_configuration_mismatch_blocks_before_outbound(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        boundary.bind("configuration")
        with patch("learning_app.execution.boundary.http.client.HTTPSConnection") as constructor:
            result = RunningRequest(
                boundary,
                request_body(generation_id="configuration", output_config={"effort": "low"}),
            ).join()
        self.assertEqual(result.status, 403)
        constructor.assert_not_called()
        self.assertEqual(boundary.actual_outbound_attempts, 0)

    def test_generation_selection_is_immutable_and_isolates_concurrent_choices(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        boundary.bind("course-a", model="deepseek-v4-pro", thinking="none")
        boundary.bind("course-b", model="deepseek-flash", thinking="low")
        with self.assertRaisesRegex(ValueError, "GENERATION_SELECTION_IMMUTABLE"):
            boundary.bind("course-a", model="deepseek-flash", thinking="low")
        requests = [
            request_body(generation_id="course-a", model="deepseek-v4-pro",
                         thinking={"type": "disabled"}, output_config={}),
            request_body(generation_id="course-b", output_config={"effort": "low"}),
        ]
        transports = []

        def factory(*args, **kwargs):
            transport = ProviderConnection(chunks=[frame({"type": "message_stop"})])
            transports.append(transport)
            return transport

        with patch("learning_app.execution.boundary.http.client.HTTPSConnection",
                   side_effect=factory):
            for request in reversed(requests):
                self.assertEqual(RunningRequest(boundary, request).join().status, 200)
            # The first generation must keep its own disabled-thinking contract.
            result = RunningRequest(boundary, request_body(
                generation_id="course-a", model="deepseek-v4-pro",
                output_config={"effort": "low"})).join()
            self.assertEqual(result.status, 403)
        self.assertEqual(len(transports), 2)
        self.assertEqual(boundary.actual_outbound_attempts, 2)

    def test_redirect_is_blocked_without_second_request(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        boundary.bind("redirect")
        transport = ProviderConnection(status=307)
        with patch(
            "learning_app.execution.boundary.http.client.HTTPSConnection", return_value=transport
        ):
            result = RunningRequest(boundary, request_body(generation_id="redirect")).join()
            boundary.wait_for_event("request_finished")
        self.assertEqual(result.status, 502)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(boundary.records[0]["status"], "upstream_redirect_blocked")

    def test_cancel_interrupts_io_and_reader_exclusively_closes_provider_parser(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        boundary.bind("official-cancel")
        chunks = [
            frame(
                {
                    "type": "message_start",
                    "message": {"id": "partial", "usage": {"input_tokens": 9}},
                }
            ),
            frame(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "active"},
                }
            ),
        ]
        transport = ProviderConnection(chunks=chunks, wait_for_interrupt=True)
        with patch(
            "learning_app.execution.boundary.http.client.HTTPSConnection", return_value=transport
        ):
            request = RunningRequest(boundary, request_body(generation_id="official-cancel"))
            boundary.wait_for_event("thinking_delta", generation_id="official-cancel")
            self.assertTrue(transport.response.read_entered.wait(3))
            request.interrupt()
            request.join()
            boundary.wait_for_event("request_finished", generation_id="official-cancel")
        row = boundary.records[0]
        self.assertEqual(transport.sock.interrupt_threads, ["learning-provider-io-watch"])
        self.assertEqual(transport.response.closed_on, "learning-messages-request")
        self.assertEqual(transport.closed_on, "learning-messages-request")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(row["status"], "upstream_unknown")
        self.assertTrue(row["cancel_observed"])
        self.assertFalse(row["usage"]["settled"])

    def test_total_timeout_interrupts_blocked_provider_read_without_retry(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key", timeout=0.2)
        boundary.bind("timeout")
        transport = ProviderConnection(wait_for_interrupt=True)
        with patch(
            "learning_app.execution.boundary.http.client.HTTPSConnection", return_value=transport
        ):
            RunningRequest(boundary, request_body(generation_id="timeout")).join()
            boundary.wait_for_event("request_finished")
        self.assertTrue(boundary.records[0]["timed_out"])
        self.assertEqual(len(transport.requests), 1)
        self.assertFalse(boundary.records[0]["usage"]["settled"])

    def test_official_requires_current_user_marker_not_historical_binding(self):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        boundary.bind("known-history")
        bodies = [
            request_body(),
            request_body(generation_id="unbound"),
            request_body(
                messages=[
                    {
                        "role": "user",
                        "content": "[[learning-generation:known-history]] old question",
                    },
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new unmarked question"},
                ]
            ),
        ]
        with patch("learning_app.execution.boundary.http.client.HTTPSConnection") as constructor:
            for body in bodies:
                self.assertEqual(RunningRequest(boundary, body).join().status, 403)
        constructor.assert_not_called()
        self.assertEqual(boundary.actual_outbound_attempts, 0)
        self.assertEqual(boundary.records, [])

    def test_recovery_projects_durable_dispatch_and_observed_partial_usage(self):
        private = self.root / "private" / "boundary"
        private.mkdir(parents=True)
        (private / "interrupted-request.response.sse").write_bytes(
            frame(
                {
                    "type": "message_start",
                    "message": {"id": "provider-partial", "usage": {"input_tokens": 19}},
                }
            )
        )
        ledger = OutboundLedger(self.root / "boundary-ledger.jsonl")
        ledger.append(
            {
                "event": "request_received",
                "request_id": "interrupted-request",
                "generation_id": "crashed-generation",
                "mode": "official",
                "status": "received",
                "actual_outbound_attempt": None,
            }
        )
        ledger.dispatch("interrupted-request", "crashed-generation")
        ledger.close()
        boundary = self.boundary()
        self.assertEqual(boundary.actual_outbound_attempts, 1)
        row = boundary.records[0]
        self.assertEqual(row["actual_outbound_attempt"], 1)
        self.assertEqual(row["status"], "dispatch_unknown")
        self.assertTrue(row["recovered_from_boundary_ledger"])
        self.assertEqual(row["usage"]["observed_cumulative"]["input_tokens"], 19)
        self.assertFalse(row["usage"]["settled"])
        self.assertIsNone(row["usage"]["normalized"]["input_tokens"])

    def test_real_http_connection_close_parser_io_is_cancelled(self):
        provider_disconnected = threading.Event()
        parser_detached_socket = threading.Event()

        class ProviderHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(
                    frame(
                        {
                            "type": "content_block_delta",
                            "delta": {
                                "type": "thinking_delta",
                                "thinking": "actual HTTP parser active",
                            },
                        }
                    )
                )
                self.wfile.flush()
                self.connection.settimeout(3)
                try:
                    if self.connection.recv(1) == b"":
                        provider_disconnected.set()
                except TimeoutError:
                    pass
                except (ConnectionError, OSError):
                    provider_disconnected.set()

        provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        provider.daemon_threads = True
        server_thread = threading.Thread(
            target=lambda: provider.serve_forever(poll_interval=0.05), daemon=True
        )
        server_thread.start()

        class RealParserConnection(http.client.HTTPConnection):
            def getresponse(self):
                response = super().getresponse()
                if response.will_close and self.sock is None:
                    parser_detached_socket.set()
                return response

        boundary = self.boundary(mode="official", api_key="synthetic-key", timeout=5)
        boundary.bind("real-parser")
        upstream = RealParserConnection("127.0.0.1", provider.server_port, timeout=5)
        try:
            with patch(
                "learning_app.execution.boundary.http.client.HTTPSConnection", return_value=upstream
            ):
                request = RunningRequest(boundary, request_body(generation_id="real-parser"))
                boundary.wait_for_event("thinking_delta", generation_id="real-parser")
                self.assertTrue(parser_detached_socket.wait(1))
                request.interrupt()
                boundary.wait_for_event("request_finished", timeout=1, generation_id="real-parser")
                request.join()
                self.assertTrue(provider_disconnected.wait(1))
            self.assertTrue(boundary.records[0]["cancel_observed"])
            self.assertEqual(boundary.actual_outbound_attempts, 1)
            self.assertFalse(boundary.records[0]["usage"]["settled"])
        finally:
            provider.shutdown()
            provider.server_close()
            server_thread.join(timeout=1)

    def test_ledger_exclusive_owner_persists_count_without_limit(self):
        path = self.root / "owned-ledger.jsonl"
        ledger = OutboundLedger(path)
        try:
            for index in range(9):
                self.assertEqual(ledger.dispatch(f"request-{index}", "generation"), index + 1)
            with self.assertRaisesRegex(RuntimeError, "ALREADY_OWNED"):
                OutboundLedger(path)
        finally:
            ledger.close()
        recovered = OutboundLedger(path)
        try:
            self.assertEqual(recovered.count, 9)
            self.assertEqual(recovered.dispatch("request-9", "next"), 10)
        finally:
            recovered.close()

    def _reader_failure_before_watcher(self, *, disconnect_peer: bool, upstream_read: bool = False):
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        downstream, peer = socket.socketpair()
        self.addCleanup(downstream.close)
        self.addCleanup(peer.close)
        watcher_waiting = threading.Event()
        event_type = without_watcher_polling(watcher_waiting)

        class Writer:
            def write(self, data):
                if not watcher_waiting.wait(2):
                    raise AssertionError("watcher did not reach its controlled wait")
                if disconnect_peer:
                    peer.shutdown(socket.SHUT_RDWR)
                    peer.close()
                raise ConnectionAbortedError("controlled downstream write failure")

            def flush(self):
                pass

        class Handler:
            connection = downstream
            headers = {}
            wfile = Writer()

            def send_response(self, status):
                pass

            def send_header(self, name, value):
                pass

            def end_headers(self):
                pass

        transport = ProviderConnection(
            chunks=[
                frame(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "partial"},
                    }
                )
            ]
        )
        if upstream_read:

            def fail_read(size):
                if not watcher_waiting.wait(2):
                    raise AssertionError("watcher did not reach its controlled wait")
                raise ConnectionAbortedError("controlled upstream read failure")

            transport.response.read1 = fail_read
        row = {"request_id": "reader-race", "generation_id": "race-generation"}
        body = request_body(generation_id="race-generation")
        with (
            patch("learning_app.execution.boundary.threading.Event", event_type),
            patch(
                "learning_app.execution.boundary.http.client.HTTPSConnection",
                return_value=transport,
            ),
        ):
            boundary._forward(
                Handler(), body, json.dumps(body).encode(), row, UsageObserver("reader-race")
            )
        self.assertTrue(watcher_waiting.is_set())
        self.assertEqual(transport.sock.interrupt_threads, [])
        self.assertEqual(row["status"], "upstream_unknown")
        self.assertEqual(row["error_type"], "ConnectionAbortedError")
        return row

    def test_reader_write_failure_observes_disconnected_peer_before_watcher_poll(self):
        row = self._reader_failure_before_watcher(disconnect_peer=True)
        self.assertTrue(row["cancel_observed"])
        self.assertEqual(row["cancel_evidence"], "reader_observed_downstream_disconnect")

    def test_reader_write_error_with_live_peer_does_not_infer_cancel(self):
        row = self._reader_failure_before_watcher(disconnect_peer=False)
        self.assertFalse(row.get("cancel_observed", False))
        self.assertNotIn("cancel_evidence", row)

    def test_upstream_read_error_with_live_peer_stays_unknown(self):
        row = self._reader_failure_before_watcher(disconnect_peer=False, upstream_read=True)
        self.assertFalse(row.get("cancel_observed", False))
        self.assertNotIn("cancel_evidence", row)

    def test_real_http_reader_write_detects_disconnect_before_watcher_poll(self):
        provider_waiting = threading.Event()
        send_next = threading.Event()
        watcher_waiting = threading.Event()
        write_failed = threading.Event()
        client_closed = threading.Event()
        event_type = without_watcher_polling(watcher_waiting)

        class ProviderHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(
                    frame(
                        {
                            "type": "content_block_delta",
                            "delta": {
                                "type": "text_delta",
                                "text": "partial body already delivered",
                            },
                        }
                    )
                )
                self.wfile.flush()
                provider_waiting.set()
                if send_next.wait(3):
                    try:
                        self.wfile.write(
                            frame(
                                {
                                    "type": "content_block_delta",
                                    "delta": {"type": "text_delta", "text": "next chunk " * 1000},
                                }
                            )
                            * 8
                        )
                        self.wfile.flush()
                    except OSError:
                        pass

        provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        provider.daemon_threads = True
        server_thread = threading.Thread(
            target=lambda: provider.serve_forever(poll_interval=0.05), daemon=True
        )
        server_thread.start()
        boundary = self.boundary(mode="official", api_key="synthetic-key", timeout=5)
        boundary.bind("reader-http-race")
        original_handle = boundary._handle

        def track_downstream_write(handler):
            original_writer = handler.wfile

            class TrackedWriter:
                def write(self, data):
                    try:
                        return original_writer.write(data)
                    except OSError:
                        if client_closed.is_set():
                            write_failed.set()
                        raise

                def __getattr__(self, name):
                    return getattr(original_writer, name)

            handler.wfile = TrackedWriter()
            original_handle(handler)

        upstream = http.client.HTTPConnection("127.0.0.1", provider.server_port, timeout=5)
        try:
            with (
                patch("learning_app.execution.boundary.threading.Event", event_type),
                patch(
                    "learning_app.execution.boundary.http.client.HTTPSConnection",
                    return_value=upstream,
                ),
                patch.object(boundary, "_handle", side_effect=track_downstream_write),
            ):
                request = RunningRequest(boundary, request_body(generation_id="reader-http-race"))
                boundary.wait_for_event("text_delta", generation_id="reader-http-race")
                self.assertTrue(provider_waiting.wait(1))
                self.assertTrue(watcher_waiting.wait(1))
                self.assertTrue(request.ready.wait(1))
                request.socket.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("HH" if os.name == "nt" else "ii", 1, 0),
                )
                request.interrupt()
                request.join()
                client_closed.set()
                send_next.set()
                boundary.wait_for_event(
                    "request_finished", timeout=2, generation_id="reader-http-race"
                )
            self.assertTrue(write_failed.is_set())
            row = boundary.records[0]
            self.assertTrue(row["cancel_observed"])
            self.assertEqual(row["cancel_evidence"], "reader_observed_downstream_disconnect")
            self.assertEqual(row["status"], "upstream_unknown")
            self.assertEqual(boundary.actual_outbound_attempts, 1)
            self.assertFalse(row["usage"]["settled"])
            evidence = (boundary.private / f"{row['request_id']}.response.sse").read_bytes()
            self.assertIn(b"partial body already delivered", evidence)
        finally:
            send_next.set()
            provider.shutdown()
            provider.server_close()
            server_thread.join(timeout=1)

    def test_stop_interrupts_owned_fixture_and_releases_port(self):
        boundary = self.boundary()
        boundary.set_scenario({"pause_after_event": "text_delta", "gate": "stop"})
        request = RunningRequest(boundary)
        boundary.wait_for_event("gate_waiting")
        address = ("127.0.0.1", urlsplit(boundary.url).port)
        boundary.stop()
        request.join()
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.5)
        self.assertFalse(any(worker.is_alive() for worker in boundary._requests))

    def test_validation_and_official_scenario_protection(self):
        for kwargs in [
            {"mode": "fixture", "api_key": "secret"},
            {"mode": "official"},
            {"max_tokens": 0},
            {"max_tokens": True},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"model": "http://evil.example"},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MessagesBoundary(self.root, **kwargs)
        boundary = self.boundary(mode="official", api_key="synthetic-key")
        with self.assertRaisesRegex(RuntimeError, "SCENARIOS_REQUIRE_FIXTURE_MODE"):
            boundary.set_scenario({})


if __name__ == "__main__":
    unittest.main()
