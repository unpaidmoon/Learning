"""Local component checks. These do not claim Harness integration or real usage."""
from __future__ import annotations

import concurrent.futures
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fixture import BoundaryServer, BudgetLedger, UsageObserver


def sse(event):
    return ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()


class FixtureTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[3] / ".runtime" / "validation" / "v1-fixture-selftest"
        root.mkdir(parents=True, exist_ok=True)
        self.output = Path(tempfile.mkdtemp(prefix="check-", dir=root))
        self.server = BoundaryServer(self.output).start()

    def tearDown(self):
        self.server.stop()

    def request(self, path="/v1/messages", body=None, method="POST", headers=None):
        url = urlsplit(self.server.url)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        if body is None:
            body = {"model": "deepseek-flash", "stream": True, "max_tokens": 8192,
                    "thinking": {"type": "enabled"}, "output_config": {"effort": "high"},
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "synthetic"}]}]}
        encoded = json.dumps(body).encode() if isinstance(body, dict) else body
        connection.request(method, path, body=encoded,
                           headers={"Content-Type": "application/json", "x-api-key": self.server.local_token,
                                    **(headers or {})})
        return connection, connection.getresponse()

    def test_fixture_streams_before_terminal_without_network(self):
        self.server.set_scenario(text_chunks=["first", "second"], hold_seconds=0.4)
        with patch("fixture.http.client.HTTPSConnection", side_effect=AssertionError("A attempted internet")):
            connection, response = self.request()
            started = time.monotonic()
            data = b""
            while b'"text": "first"' not in data:
                data += response.readline()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertNotIn(b"message_stop", data)
            data += response.read()
            connection.close()
        self.assertIn(b"message_stop", data)
        self.assertEqual(self.server.actual_outbound_attempts, 0)

    def test_activity_cancel_observed(self):
        self.server.set_scenario(text_chunks=["active"], hold_seconds=10)
        connection, response = self.request()
        data = b""
        while b'"text": "active"' not in data:
            data += response.readline()
        response.close()
        connection.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.server.records[0].get("cancel_observed"):
            time.sleep(0.02)
        self.assertTrue(self.server.records[0].get("cancel_observed"))
        self.assertFalse(self.server.records[0]["usage"]["settled"])

    def test_usage_absence_zero_and_cumulative(self):
        observer = UsageObserver()
        observer.feed(sse({"type": "message_start", "message": {"usage": {"input_tokens": 0}}}))
        for amount in [3, 8, 8]:
            observer.feed(sse({"type": "message_delta", "usage": {"output_tokens": amount}}))
        self.assertFalse(observer.result()["settled"])
        self.assertIsNone(observer.result()["normalized"]["output_tokens"])
        observer.feed(sse({"type": "message_stop"}))
        observer.feed(sse({"type": "message_stop"}))
        observer.feed(sse({"type": "message_delta", "usage": {"output_tokens": 999}}))
        result = observer.result()
        self.assertEqual(result["normalized"]["input_tokens"], 0)
        self.assertEqual(result["normalized"]["output_tokens"], 8)
        self.assertIsNone(result["normalized"]["cache_read_input_tokens"])
        self.assertEqual(result["settlement_count"], 1)
        self.assertEqual(result["message_stop_events"], 2)
        self.assertEqual(result["post_terminal_events_ignored"], 1)

    def test_files_roundtrip_and_expiry_once(self):
        multipart = (b"--fixture\r\nContent-Disposition: form-data; name=\"file\"; filename=\"fixture.png\"\r\n"
                     b"Content-Type: image/png\r\n\r\nsynthetic-file-bytes\r\n--fixture--\r\n")
        connection, response = self.request("/v1/files", multipart,
                                            headers={"Content-Type": "multipart/form-data; boundary=fixture"})
        first = json.loads(response.read())
        connection.close()
        self.assertEqual(first["id"], "file-v1")
        self.server.set_scenario(fault="file_expired_once")
        connection, response = self.request()
        self.assertEqual(response.status, 400)
        self.assertIn(b"file-v1 expired", response.read())
        connection.close()
        connection, response = self.request()
        self.assertEqual(response.status, 200)
        self.assertIn(b"message_stop", response.read())
        connection.close()
        connection, response = self.request("/v1/files", b"", "GET")
        self.assertEqual(json.loads(response.read())["data"][0]["id"], "file-v1")
        connection.close()
        connection, response = self.request("/v1/files/file-v1", b"", "DELETE")
        self.assertEqual(json.loads(response.read())["type"], "file_deleted")
        connection.close()
        self.assertEqual(self.server.actual_outbound_attempts, 0)

    def test_reject_unapproved_endpoint(self):
        connection, response = self.request("http://example.invalid/v1/messages")
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assertEqual(self.server.actual_outbound_attempts, 0)

    def test_http_500_once_then_success_and_learning_retry_zero_failure(self):
        self.server.set_scenario(fault="http_500", fault_once=True)
        connection, response = self.request()
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(response.read())["error"]["type"], "api_error")
        connection.close()
        connection, response = self.request()
        self.assertEqual(response.status, 200)
        self.assertIn(b"message_stop", response.read())
        connection.close()
        self.server.set_scenario(fault="http_500")
        connection, response = self.request()
        self.assertEqual(response.status, 500)
        response.read()
        connection.close()
        self.assertEqual(len(self.server.records), 3)
        self.assertEqual(self.server.actual_outbound_attempts, 0)

    def test_budget_is_atomic_persistent_and_fails_closed(self):
        ledger_path = self.output / "budget-component-test.jsonl"
        ledger = BudgetLedger(ledger_path, 6)

        def reserve(number):
            try:
                return ledger.reserve(f"fixture-{number}")
            except RuntimeError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            numbers = list(pool.map(reserve, range(8)))
        self.assertEqual(sorted(number for number in numbers if number), list(range(1, 7)))
        with self.assertRaisesRegex(RuntimeError, "BUDGET_LEDGER_ALREADY_OWNED"):
            BudgetLedger(ledger_path, 6)
        ledger.close()
        restored = BudgetLedger(ledger_path, 6)
        self.assertEqual(restored.count, 6)
        with self.assertRaises(RuntimeError):
            restored.reserve("restart-cannot-increase-budget")
        with self.assertRaises(ValueError):
            BudgetLedger(self.output / "invalid.jsonl", 7)
        restored.close()

    def test_cross_origin_wrong_host_and_wrong_token_do_not_dispatch(self):
        for headers, expected in [
            ({"Origin": "https://untrusted.example"}, 403),
            ({"Host": "untrusted.example"}, 403),
            ({"x-api-key": "wrong-local-token"}, 401),
        ]:
            connection, response = self.request(headers=headers)
            self.assertEqual(response.status, expected)
            response.read()
            connection.close()
        self.assertEqual(self.server.actual_outbound_attempts, 0)
        self.assertEqual(self.server.records, [])

    def test_unlimited_counter_requires_authorization_and_survives_restart(self):
        path = self.output / "unlimited-counter-test.jsonl"
        with self.assertRaisesRegex(ValueError, "UNLIMITED_REQUIRES_AUTHORIZATION_REFERENCE"):
            BudgetLedger(path, None)
        with self.assertRaisesRegex(ValueError, "A_MUST_NOT_LOAD_KEY_OR_REAL_BUDGET"):
            BoundaryServer(self.output / "invalid-A-unlimited", mode="A", limit=None,
                           authorization_ref="fixture user authorization")
        counter = BudgetLedger(path, None, "fixture user authorizes autonomous V1 calls")
        try:
            self.assertEqual([counter.reserve(str(i)) for i in range(8)], list(range(1, 9)))
            with self.assertRaisesRegex(RuntimeError, "BUDGET_LEDGER_ALREADY_OWNED"):
                BudgetLedger(path, None, "fixture authorization")
        finally:
            counter.close()
        restored = BudgetLedger(path, None, "fixture authorization remains in force")
        try:
            self.assertEqual(restored.count, 8)
            self.assertEqual(restored.reserve("after-restart"), 9)
            self.assertEqual(restored.limit, None)
            self.assertIn('"attempt_limit": null', path.read_text())
        finally:
            restored.close()

    def test_restart_does_not_overwrite_private_requests(self):
        connection, response = self.request()
        response.read()
        connection.close()
        first_id = self.server.records[0]["request_id"]
        first_path = self.server.private / f"{first_id}.request.json"
        first_bytes = first_path.read_bytes()
        self.server.stop()
        self.server = BoundaryServer(self.output).start()
        connection, response = self.request()
        response.read()
        connection.close()
        self.assertNotEqual(self.server.records[0]["request_id"], first_id)
        self.assertEqual(first_path.read_bytes(), first_bytes)

    def test_cancel_during_connect_never_reopens_or_sends(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        class ConnectingDouble:
            sock = None

            def __init__(self, *args, **kwargs):
                self.auto_open = 1

            def connect(self):
                entered.set()
                release.wait(3)

            def request(self, *_args, **_kwargs):
                calls.append(self.auto_open)
                raise AssertionError("cancelled transport sent request")

            def close(self):
                return

        guarded = BoundaryServer(self.output / "connect-cancel", mode="B", limit=1, key="fixture-dummy").start()
        url = urlsplit(guarded.url)
        wire = {"model": "deepseek-flash", "stream": True, "max_tokens": 8192,
                "thinking": {"type": "enabled"}, "output_config": {"effort": "high"}, "messages": []}
        payload = json.dumps(wire).encode()
        raw = socket.create_connection((url.hostname, url.port), timeout=3)
        try:
            with patch("fixture.http.client.HTTPSConnection", ConnectingDouble):
                headers = (f"POST /v1/messages HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\n"
                           f"x-api-key: {guarded.local_token}\r\nContent-Length: {len(payload)}\r\n"
                           "Content-Type: application/json\r\n\r\n").encode()
                raw.sendall(headers + payload)
                self.assertTrue(entered.wait(2))
                raw.shutdown(socket.SHUT_RDWR)
                raw.close()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not guarded.records[0].get("cancel_observed"):
                    time.sleep(0.02)
                self.assertTrue(guarded.records[0].get("cancel_observed"))
                release.set()
                guarded.stop()
            self.assertEqual(calls, [])
            self.assertEqual(guarded.records[0]["status"], "upstream_unknown")
            self.assertIn("finished_ns", guarded.records[0])
        finally:
            release.set()
            raw.close()
            guarded.stop()

    def test_b_branch_with_fake_transport_counts_and_does_not_retry(self):
        # This is an in-process transport double, not a real B model invocation.
        calls = []

        class FakeResponse:
            status = 200

            def __init__(self):
                self.parts = iter([
                    sse({"type": "message_start", "message": {"usage": {"input_tokens": 4}}}),
                    sse({"type": "message_delta", "usage": {"output_tokens": 9}}),
                    sse({"type": "message_stop"}), b"",
                ])

            def getheader(self, _key, _default):
                return "text/event-stream"

            def read1(self, _size):
                return next(self.parts)

            def close(self):
                return

        class FakeConnection:
            sock = None

            def __init__(self, *args, **kwargs):
                self.args = args

            def connect(self):
                return

            def request(self, method, path, body, headers):
                calls.append((method, path, body, headers))

            def getresponse(self):
                return FakeResponse()

            def close(self):
                return

        original = self.server
        guarded = BoundaryServer(self.output / "fake-b", mode="B", limit=1, key="fixture-dummy").start()
        self.server = guarded
        try:
            with patch("fixture.http.client.HTTPSConnection", FakeConnection):
                connection, response = self.request()
                self.assertEqual(response.status, 200)
                self.assertIn(b"message_stop", response.read())
                connection.close()
                connection, response = self.request()
                self.assertEqual(response.status, 429)
                response.read()
                connection.close()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], "/anthropic/v1/messages")
            self.assertEqual(guarded.actual_outbound_attempts, 1)
            self.assertNotIn("fixture-dummy", guarded.ledger.path.read_text())
        finally:
            guarded.stop()
            self.server = original

    def test_stream_cancel_does_not_close_parser_from_watcher(self):
        release = threading.Event()
        wrong_thread_closes = []

        class UpstreamSocket:
            def settimeout(self, value): pass
            def shutdown(self, how): release.set()
            def close(self): release.set()

        class Response:
            status = 200
            reader = None
            sent = False
            def getheader(self, key, default): return "text/event-stream"
            def read1(self, size):
                self.reader = threading.get_ident()
                if not self.sent:
                    self.sent = True
                    return sse({"type": "message_start", "message": {"usage": {"input_tokens": 4}}}) + sse({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "active"}})
                release.wait(3)
                time.sleep(.05)
                raise OSError("cancelled upstream socket")
            def close(self):
                if self.reader is not None and self.reader != threading.get_ident():
                    wrong_thread_closes.append(threading.get_ident())
                    raise AttributeError("concurrent HTTPResponse parser close")

        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = UpstreamSocket()
                self.response = Response()
            def connect(self): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return self.response
            def close(self): self.response.close()

        guarded = BoundaryServer(self.output / "stream-parser-cancel", mode="B", limit=1, key="fixture-dummy").start()
        url = urlsplit(guarded.url)
        raw = socket.create_connection((url.hostname, url.port), timeout=3)
        wire = {"model": "deepseek-flash", "stream": True, "max_tokens": 8192,
                "thinking": {"type": "enabled"}, "output_config": {"effort": "high"}, "messages": []}
        body = json.dumps(wire).encode()
        try:
            with patch("fixture.http.client.HTTPSConnection", Connection):
                headers = (f"POST /v1/messages HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\n"
                           f"x-api-key: {guarded.local_token}\r\nContent-Length: {len(body)}\r\n"
                           "Content-Type: application/json\r\n\r\n").encode()
                raw.sendall(headers + body)
                received = b""
                while b"active" not in received:
                    received += raw.recv(4096)
                raw.shutdown(socket.SHUT_RDWR)
                raw.close()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not guarded.records[0].get("finished_ns"):
                    time.sleep(.02)
                guarded.stop()
            self.assertEqual(wrong_thread_closes, [])
            self.assertTrue(guarded.records[0]["cancel_observed"])
            self.assertEqual(guarded.records[0]["status"], "upstream_unknown")
            self.assertEqual(guarded.actual_outbound_attempts, 1)
        finally:
            release.set();raw.close();guarded.stop()

    def test_autonomous_B_counts_past_six_and_keeps_model_guard(self):
        calls = []
        class Response:
            status = 200
            def __init__(self):
                self.parts = iter([sse({"type":"message_start","message":{"usage":{"input_tokens":1}}}),
                                   sse({"type":"message_delta","usage":{"output_tokens":1}}),
                                   sse({"type":"message_stop"}),b""])
            def getheader(self, key, default): return "text/event-stream"
            def read1(self, size): return next(self.parts)
            def close(self): pass
        class Connection:
            sock = None
            def __init__(self, *args, **kwargs): pass
            def connect(self): pass
            def request(self, method, path, body, headers): calls.append((method,path))
            def getresponse(self): return Response()
            def close(self): pass
        with self.assertRaisesRegex(ValueError, "UNLIMITED_REQUIRES_AUTHORIZATION_REFERENCE"):
            BoundaryServer(self.output/'no-autonomous-grant',mode='B',limit=None,key='fixture-dummy')
        original = self.server
        guarded = BoundaryServer(self.output/'autonomous-fake-B',mode='B',limit=None,key='fixture-dummy',
                                 authorization_ref='fixture user grant of autonomous V1 calls').start()
        self.server=guarded
        try:
            with patch('fixture.http.client.HTTPSConnection',Connection):
                for _ in range(8):
                    conn,response=self.request();self.assertEqual(response.status,200);response.read();conn.close()
                conn,response=self.request(body={'model':'unapproved-model','stream':True,'max_tokens':8192,
                    'thinking':{'type':'enabled'},'output_config':{'effort':'high'},'messages':[]})
                self.assertEqual(response.status,403);response.read();conn.close()
            self.assertEqual(len(calls),8)
            self.assertEqual(guarded.actual_outbound_attempts,8)
            self.assertTrue(all(path=='/anthropic/v1/messages' for _,path in calls))
        finally:
            guarded.stop();self.server=original


if __name__ == "__main__":
    unittest.main()
