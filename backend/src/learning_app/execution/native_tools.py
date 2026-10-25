"""Private native-tool attribution and transport; Repository owns authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from .provider_tool_calls import ProviderToolCalls


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ToolRejected(Exception):
    """A known rejection, with no unsettled business side effect."""

    def __init__(self, code: str, status: int = 403, *, details: dict | None = None):
        self.code, self.status = code, status
        self.details = details


class KnownToolFailure(ToolRejected):
    """An exact immutable business failure receipt, safe to reconcile read-only."""


@dataclass(frozen=True)
class NativeCall:
    session_id: str
    run_id: str
    request_id: str
    epoch: int
    attempt_id: str
    dispatch_id: str
    tool_ordinal: int
    native_turn: int
    native_step: int
    native_call_seq: int
    call_id: str
    tool_name: str
    args_sha256: str


class RepositoryTools(Protocol):
    """Implement in existing Repository/Scope/TaskRunStore, transactionally.

    Neither headers nor NativeCall authorize source/task writes. The adapter
    must recheck the run's active epoch, course, exact tools, source handles,
    source versions, task/object/stage grants and CAS. Unknown effects reject
    assert_releasable. Stage deduplication uses a stable business operation id.
    """

    def authorize(self, call: NativeCall, arguments: dict[str, Any]) -> None: ...
    def invoke(self, call: NativeCall, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def assert_releasable(self, lease: dict[str, Any]) -> None: ...
    def reconcile_effect(self, call: NativeCall) -> dict[str, Any] | None: ...
    def remember_terminal(self, run_id: str, terminal: dict[str, Any]) -> None: ...
    def mark_released(self, receipt: dict[str, Any]) -> None: ...
    def retire_unstarted(self, lease: dict[str, Any]) -> dict[str, Any] | None: ...


class NativeCallRegistry:
    """Attribution/receipts only. This journal is private, not a business DB."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.scopes, self.current, self.released = {}, {}, set()
        self.retired_unstarted = set()
        self.release_receipts = {}
        self.tool_stops = {}
        self.attempts, self.dispatches, self.bindings, self.operations, self.provider_bindings = (
            {},
            {},
            {},
            {},
            {},
        )
        self.parsers = {}
        self.business_port = None
        self.run_locks = {}
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                self._fold(json.loads(line))

    def _record(self, event):
        with self.path.open("ab") as out:
            out.write((canonical(event) + "\n").encode())
            out.flush()
            os.fsync(out.fileno())
        self._fold(event)

    def _fold(self, event):
        kind = event["event"]
        if kind == "scope":
            scope = event["value"]
            self.scopes[scope["run_id"]] = scope
            self.current[scope["session_id"]] = scope["run_id"]
        elif kind == "release":
            self.released.add(event["run_id"])
            if event.get("receipt"):
                self.release_receipts[event["run_id"]] = event["receipt"]
        elif kind == "tool_stop_proved":
            self.tool_stops[event["run_id"]] = event["value"]
        elif kind == "retired_unstarted":
            self.retired_unstarted.add(event["run_id"])
        elif kind == "attempt":
            item = event["value"]
            self.attempts[item["attempt_id"]] = {**item, "calls": [], "state": "streaming"}
            self.dispatches[item["dispatch_id"]] = item["attempt_id"]
        elif kind == "provider_call":
            self.attempts[event["attempt_id"]]["calls"].append(event["value"])
        elif kind == "attempt_end":
            self.attempts[event["attempt_id"]]["state"] = event["state"]
        elif kind == "network_closed":
            self.attempts[event["attempt_id"]]["network"] = event["value"]
        elif kind == "attempt_reconciled":
            self.attempts[event["attempt_id"]]["state"] = event["state"]
            self.attempts[event["attempt_id"]]["reconciliation"] = event["value"]
        elif kind == "native_call":
            self.bindings[event["key"]] = event["value"]
            if event.get("not_dispatched") is True and event["key"] not in self.operations:
                self.operations[event["key"]] = {
                    "state": "not_dispatched",
                    "run_id": event["value"]["run_id"],
                }
            self.provider_bindings[
                f"{event['value']['attempt_id']}:{event['value']['tool_ordinal']}"
            ] = event["key"]
        elif kind == "operation":
            self.operations[event["key"]] = event["value"]

    def bind(self, scope):
        with self.lock:
            known = self.scopes.get(scope["run_id"])
            if known:
                if known != scope:
                    raise ToolRejected("NATIVE_SCOPE_IMMUTABLE", 409)
                return
            prior_id = self.current.get(scope["session_id"])
            if prior_id:
                prior = self.scopes[prior_id]
                if prior_id not in self.released and prior_id not in self.retired_unstarted:
                    raise ToolRejected("NATIVE_PREVIOUS_NOT_RELEASED", 409)
                if scope["epoch"] <= prior["epoch"]:
                    raise ToolRejected("NATIVE_EPOCH_NOT_INCREASING", 409)
            self._record({"event": "scope", "value": scope})

    def owner(self, value):
        scope = self.scopes.get(value.get("run_id"))
        if not scope or any(
            value.get(k) != scope[k] for k in ["session_id", "request_id", "epoch"]
        ):
            raise ToolRejected("NATIVE_OWNER_MISMATCH")
        if (
            scope["run_id"] in self.released | self.retired_unstarted
            or self.current.get(scope["session_id"]) != scope["run_id"]
        ):
            raise ToolRejected("NATIVE_LEASE_NOT_ACTIVE", 409)
        return scope

    def begin_attempt(self, row, dispatch_id):
        self._require_settled_effects(row["generation_id"], self.business_port)
        with self.lock:
            scope = self.scopes.get(row["generation_id"])
            if not scope or row.get("native_session_id") != scope["session_id"]:
                raise ToolRejected("NATIVE_ATTEMPT_SCOPE")
            self.owner(scope)
            if (
                not isinstance(dispatch_id, str)
                or not re.fullmatch(r"[a-f0-9-]{36}", dispatch_id)
                or dispatch_id in self.dispatches
            ):
                raise ToolRejected("NATIVE_DISPATCH_REUSED")
            value = {
                "attempt_id": row["request_id"],
                "dispatch_id": dispatch_id,
                "run_id": scope["run_id"],
                "session_id": scope["session_id"],
                "request_id": scope["request_id"],
                "epoch": scope["epoch"],
            }
            self._record({"event": "attempt", "value": value})
            self.parsers[row["request_id"]] = ProviderToolCalls()

    def feed(self, row, chunk):
        with self.lock:
            parser = self.parsers.get(row["request_id"])
            if parser is None:
                raise ToolRejected("NATIVE_ATTEMPT_PARSER_MISSING")
            try:
                for call in parser.feed(chunk):
                    self._record(
                        {"event": "provider_call", "attempt_id": row["request_id"], "value": call}
                    )
            except (ValueError, KeyError, UnicodeError):
                self.finish_attempt(row, "unknown")
                raise ConnectionAbortedError("NATIVE_PROVIDER_CALL_INVALID") from None

    def finish_attempt(self, row, state):
        with self.lock:
            if row["request_id"] in self.attempts:
                self._record(
                    {"event": "attempt_end", "attempt_id": row["request_id"], "state": state}
                )
                self.parsers.pop(row["request_id"], None)

    def network_closed(self, row, *, downstream_closed):
        with self.lock:
            if row["request_id"] not in self.attempts:
                return
            fixture = row["mode"] == "fixture"
            value = {
                "downstream_closed": downstream_closed,
                "upstream_closed": True if fixture else row.get("upstream_transport_closed"),
                "watcher_closed": True if fixture else row.get("upstream_watcher_closed"),
                "upstream_evidence": "fixture_no_upstream"
                if fixture
                else "original_socket_fileno_and_join",
                "finished_ns": row.get("finished_ns"),
                "status": row["status"],
                "cancel_observed": row.get("cancel_observed"),
                "fault_injected": row.get("fault_injected"),
                "timed_out": row.get("timed_out"),
                "upstream_status": row.get("upstream_status"),
            }
            self._record(
                {"event": "network_closed", "attempt_id": row["request_id"], "value": value}
            )

    def reconcile_provider_terminal(self, row, *, response_sha256, calls, usage):
        """Recover a captured provider terminal, never dispatch or invoke a tool."""
        with self.lock:
            attempt = self.attempts.get(row["request_id"])
            if not attempt or attempt["state"] not in {"streaming", "unknown"}:
                return None
            scope = self.scopes.get(attempt["run_id"])
            network = attempt.get("network", {})
            if (
                not scope
                or row.get("generation_id") != attempt["run_id"]
                or row.get("native_session_id") != attempt["session_id"]
                or row.get("host_dispatch_id") != attempt["dispatch_id"]
                or any(
                    attempt[key] != scope[key]
                    for key in ("run_id", "session_id", "request_id", "epoch")
                )
                or row.get("mode") != "official"
                or row.get("upstream_status") != 200
                or not row.get("actual_outbound_attempt")
                or row.get("fault_injected") is True
                or any(
                    network.get(key) is not True
                    for key in ("downstream_closed", "upstream_closed", "watcher_closed")
                )
                or not network.get("finished_ns")
                or network.get("fault_injected") is True
                or usage.get("settled") is not True
                or usage.get("message_stop_events") != 1
                or calls != attempt["calls"]
                or not re.fullmatch(r"[a-f0-9]{64}", response_sha256)
            ):
                return None
            proof = {
                "kind": "original_provider_terminal",
                "response_sha256": response_sha256,
                "native_call_vector_sha256": args_hash(calls),
                "network": network,
                "original_status": row["status"],
                "usage_sha256": args_hash(usage),
            }
            self._record(
                {
                    "event": "attempt_reconciled",
                    "attempt_id": attempt["attempt_id"],
                    "state": "completed",
                    "value": proof,
                }
            )
            return proof

    def reconcile_terminal(self, lease, native_terminal, port, *, tool_stop=None):
        # Called by the backend after storing actual native terminal evidence;
        # Repository must check this exact turn/seq/reason against its run.
        with self.lock:
            self.owner(lease)
            reason = native_terminal.get("reason", {})
            kind = reason.get("kind")
            if kind == "aborted" and reason.get("reason", {}).get("kind") == "user":
                classification = "closed_cancelled"
            elif kind == "error":
                classification = "closed_error"
            else:
                raise ToolRejected("NATIVE_TERMINAL_CLASSIFICATION_DENIED", 409)
            port.assert_releasable({**lease, "native_terminal": native_terminal})
            if any(
                v["run_id"] == lease["run_id"] and v["state"] != "completed"
                for v in self.operations.values()
            ):
                raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)
            affected = [
                v
                for v in self.attempts.values()
                if v["run_id"] == lease["run_id"] and v["state"] != "completed"
            ]
            if not affected:
                self._prove_tool_stop(lease, native_terminal, classification, tool_stop)
                return
            for attempt in affected:
                if attempt["state"] in {"closed_cancelled", "closed_error"}:
                    if (
                        attempt["state"] != classification
                        or attempt["reconciliation"]["native_terminal"] != native_terminal
                    ):
                        raise ToolRejected("NATIVE_TERMINAL_IMMUTABLE", 409)
                    continue
                net = attempt.get("network", {})
                if (
                    net.get("downstream_closed") is not True
                    or net.get("upstream_closed") is not True
                    or net.get("watcher_closed") is not True
                    or not net.get("finished_ns")
                ):
                    raise ToolRejected("NATIVE_NETWORK_NOT_CLOSED", 409)
                if attempt["calls"]:
                    raise ToolRejected("NATIVE_UNKNOWN_TOOL_VECTOR", 409)
                if classification == "closed_cancelled":
                    if (
                        net.get("status")
                        not in {"connection_closed", "upstream_unknown", "upstream_unsettled"}
                        or net.get("cancel_observed") is not True
                        or net.get("fault_injected") is True
                        or net.get("timed_out") is True
                    ):
                        raise ToolRejected("NATIVE_CANCEL_NOT_KNOWN", 409)
                elif net.get("status") not in {"fixture_http_500", "execution_policy_blocked"}:
                    raise ToolRejected("NATIVE_ERROR_ATTEMPT_UNKNOWN", 409)
            for attempt in affected:
                if attempt["state"] in {"closed_cancelled", "closed_error"}:
                    continue
                self._record(
                    {
                        "event": "attempt_reconciled",
                        "attempt_id": attempt["attempt_id"],
                        "state": classification,
                        "value": {
                            "native_terminal": native_terminal,
                            "kind": classification,
                            "network": attempt["network"],
                        },
                    }
                )

    def _prove_tool_stop(self, lease, terminal, classification, proof):
        proof = proof or self.tool_stops.get(lease["run_id"])
        attempts = [a for a in self.attempts.values() if a["run_id"] == lease["run_id"]]
        calls = [v for v in self.bindings.values() if v["run_id"] == lease["run_id"]]
        if (
            classification != "closed_cancelled"
            or not attempts
            or not calls
            or not isinstance(proof, dict)
        ):
            raise ToolRejected("NATIVE_TERMINAL_ATTEMPT_MISSING", 409)
        last = max(calls, key=lambda call: call["native_call_seq"])
        expected = {
            k: last[k]
            for k in (
                "native_turn",
                "native_step",
                "native_call_seq",
                "call_id",
                "tool_name",
                "args_sha256",
            )
        }
        if (
            any(proof.get(k) != v for k, v in lease.items())
            or proof.get("native_terminal") != terminal
            or proof.get("call") != expected
            or last["native_turn"] != terminal["turn"]
            or type(proof.get("result_seq")) is not int
            or not last["native_call_seq"] < proof["result_seq"] < terminal["seq"]
            or type(proof.get("native_cursor")) is not int
            or proof["native_cursor"] < terminal["seq"]
            or not isinstance(proof.get("native_cut_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", proof["native_cut_sha256"])
            or any(
                not isinstance(proof.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", proof[key])
                for key in ("native_events_sha256", "native_header_sha256")
            )
        ):
            raise ToolRejected("NATIVE_TOOL_STOP_PROOF_INVALID", 409)
        if any(
            a["state"] != "completed"
            or any(
                a.get("network", {}).get(key) is not True
                for key in ("downstream_closed", "upstream_closed", "watcher_closed")
            )
            or not a.get("network", {}).get("finished_ns")
            for a in attempts
        ):
            raise ToolRejected("NATIVE_NETWORK_NOT_CLOSED", 409)
        if attempts[-1]["attempt_id"] != last["attempt_id"]:
            raise ToolRejected("NATIVE_TOOL_STOP_ATTEMPT_MISMATCH", 409)
        known = self.tool_stops.get(lease["run_id"])
        if known is not None and known != proof:
            raise ToolRejected("NATIVE_TOOL_STOP_PROOF_IMMUTABLE", 409)
        if known is None:
            self._record({"event": "tool_stop_proved", "run_id": lease["run_id"], "value": proof})

    def release_evidence(self, lease, port):
        owner_keys = {"session_id", "run_id", "request_id", "epoch"}
        if set(lease) not in (owner_keys, owner_keys | {"native_terminal"}):
            raise ToolRejected("NATIVE_RELEASE_SHAPE", 422)
        self.assert_releasable({key: lease[key] for key in owner_keys}, port)
        if "native_terminal" in lease:
            port.assert_releasable(lease)
        with self.lock:
            return {
                "releasable": True,
                **(
                    {"tool_stop": self.tool_stops[lease["run_id"]]}
                    if lease["run_id"] in self.tool_stops
                    else {}
                ),
                "attempts": [
                    {
                        **{
                            key: a[key]
                            for key in (
                                "attempt_id",
                                "dispatch_id",
                                "run_id",
                                "session_id",
                                "request_id",
                                "epoch",
                                "state",
                            )
                        },
                        "network": dict(a.get("network", {})),
                        "reconciliation": a.get("reconciliation"),
                        "calls": [
                            {key: c[key] for key in ("ordinal", "call_id", "name", "args_sha256")}
                            for c in a["calls"]
                        ],
                    }
                    for a in self.attempts.values()
                    if a["run_id"] == lease["run_id"]
                ],
                "native_calls": [
                    dict(call)
                    for call in self.bindings.values()
                    if call["run_id"] == lease["run_id"]
                ],
                "terminal_attempts": [
                    {
                        "attempt_id": a["attempt_id"],
                        "dispatch_id": a["dispatch_id"],
                        "state": a["state"],
                        "native_terminal": a["reconciliation"]["native_terminal"],
                    }
                    for a in self.attempts.values()
                    if a["run_id"] == lease["run_id"]
                    and a["state"] in {"closed_cancelled", "closed_error"}
                ],
            }

    def validate_call(self, envelope):
        keys = set(NativeCall.__dataclass_fields__) | {"arguments"}
        if not isinstance(envelope, dict) or set(envelope) != keys:
            raise ToolRejected("NATIVE_CALL_SHAPE", 422)
        if any(
            type(envelope[k]) is not int or envelope[k] < 0
            for k in ["epoch", "tool_ordinal", "native_turn", "native_step", "native_call_seq"]
        ):
            raise ToolRejected("NATIVE_CALL_INTEGER", 422)
        if (
            not isinstance(envelope["arguments"], dict)
            or args_hash(envelope["arguments"]) != envelope["args_sha256"]
        ):
            raise ToolRejected("NATIVE_CALL_ARGUMENT_HASH", 422)
        with self.lock:
            scope = self.owner(envelope)
            if envelope["tool_name"] not in scope["allowed_tools"]:
                raise ToolRejected("NATIVE_TOOL_NOT_ALLOWED")
            attempt = self.attempts.get(envelope["attempt_id"])
            if not attempt or any(
                envelope[k] != attempt[k]
                for k in ["run_id", "session_id", "request_id", "epoch", "dispatch_id"]
            ):
                raise ToolRejected("NATIVE_EXACT_ATTEMPT_MISMATCH")
            if attempt["state"] not in {"streaming", "completed"}:
                raise ToolRejected("NATIVE_ATTEMPT_UNKNOWN", 409)
            ordinal = envelope["tool_ordinal"]
            if ordinal >= len(attempt["calls"]):
                raise ToolRejected("NATIVE_PROVIDER_CALL_MISSING")
            provider = attempt["calls"][ordinal]
            if (
                provider["call_id"] != envelope["call_id"]
                or provider["name"] != envelope["tool_name"]
                or provider["args_sha256"] != envelope["args_sha256"]
            ):
                raise ToolRejected("NATIVE_PROVIDER_CALL_MISMATCH")
            call = NativeCall(**{k: envelope[k] for k in NativeCall.__dataclass_fields__})
            key = f"{call.session_id}:{call.native_call_seq}"
            value = {k: envelope[k] for k in NativeCall.__dataclass_fields__}
            previous = self.bindings.get(key)
            if previous is not None and previous != value:
                raise ToolRejected("NATIVE_CALL_BINDING_CONFLICT", 409)
            reverse = self.provider_bindings.get(f"{call.attempt_id}:{call.tool_ordinal}")
            if reverse is not None and reverse != key:
                raise ToolRejected("NATIVE_PROVIDER_CALL_ALREADY_BOUND", 409)
            if previous is None:
                self._record(
                    {
                        "event": "native_call",
                        "key": key,
                        "value": value,
                        **({"not_dispatched": True} if call.tool_name == "stage_result" else {}),
                    }
                )
            return call, key

    def invoke(self, envelope, port):
        call, key = self.validate_call(envelope)
        with self.lock:
            serial = self.run_locks.setdefault(call.run_id, threading.RLock())
        with serial:
            return self._invoke_owned(envelope, port, call, key)

    def _invoke_owned(self, envelope, port, call, key):
        self._require_settled_effects(call.run_id, port, allow_not_dispatched=True)
        with self.lock:
            prior = self.operations.get(key)
            if prior and prior["state"] == "not_dispatched":
                prior = None
            if prior and prior["state"] != "completed":
                raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)
        # Cached source/result receipts still require current Repository scope.
        stage_call = call.tool_name == "stage_result"
        if not stage_call or prior is not None:
            port.authorize(call, envelope["arguments"])
        with self.lock:
            prior = self.operations.get(key)
            if prior and prior["state"] == "not_dispatched":
                prior = None
            if prior:
                if prior["state"] != "completed":
                    raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)
                if prior.get("error"):
                    raise ToolRejected(
                        prior["error"],
                        prior.get("error_status", 403),
                        details=prior.get("error_details"),
                    )
                return prior["result"]
            self._record(
                {
                    "event": "operation",
                    "key": key,
                    "value": {"state": "unknown", "run_id": call.run_id},
                }
            )
        try:
            if stage_call:
                port.authorize(call, envelope["arguments"])
            result = port.invoke(call, envelope["arguments"])
            canonical(result)
        except ToolRejected as error:
            with self.lock:
                self._record(
                    {
                        "event": "operation",
                        "key": key,
                        "value": {
                            "state": "completed",
                            "run_id": call.run_id,
                            "error": error.code,
                            "error_status": error.status,
                            "error_details": error.details,
                        },
                    }
                )
            raise
        except Exception:
            raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409) from None
        with self.lock:
            self._record(
                {
                    "event": "operation",
                    "key": key,
                    "value": {"state": "completed", "run_id": call.run_id, "result": result},
                }
            )
        return result

    def assert_releasable(self, lease, port):
        with self.lock:
            if set(lease) != {"session_id", "run_id", "request_id", "epoch"}:
                raise ToolRejected("NATIVE_RELEASE_SHAPE", 422)
            self.owner(lease)
            if any(
                v["run_id"] == lease["run_id"]
                and v["state"] not in {"completed", "closed_cancelled", "closed_error"}
                for v in self.attempts.values()
            ):
                raise ToolRejected("NATIVE_ATTEMPT_UNKNOWN", 409)
            if any(
                v["run_id"] == lease["run_id"] and v["state"] != "completed"
                for v in self.operations.values()
            ):
                raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)
            port.assert_releasable(lease)

    def reconcile_effects(self, run_id, port):
        """Recover committed receipts only. Absence remains unknown, never replay."""
        with self.lock:
            pending = [
                (key, self.bindings[key])
                for key, value in self.operations.items()
                if value["run_id"] == run_id and value["state"] == "unknown"
            ]
            pending.extend(
                (key, value)
                for key, value in self.bindings.items()
                if value["run_id"] == run_id
                and value["tool_name"] == "stage_result"
                and key not in self.operations
            )
        recovered = []
        for key, value in pending:
            try:
                result = port.reconcile_effect(NativeCall(**value))
            except KnownToolFailure as error:
                with self.lock:
                    self._record(
                        {
                            "event": "operation",
                            "key": key,
                            "value": {
                                "state": "completed",
                                "run_id": run_id,
                                "error": error.code,
                                "error_status": error.status,
                                "error_details": error.details,
                                "reconciled": True,
                            },
                        }
                    )
                recovered.append(key)
                continue
            if result is None:
                continue
            canonical(result)
            with self.lock:
                if self.operations.get(key, {}).get("state") == "completed":
                    continue
                self._record(
                    {
                        "event": "operation",
                        "key": key,
                        "value": {
                            "state": "completed",
                            "run_id": run_id,
                            "result": result,
                            "reconciled": True,
                        },
                    }
                )
                recovered.append(key)
        return recovered

    def reconcile_call(self, envelope, port):
        """Read one exact old receipt; this endpoint never invokes a tool."""
        call, key = self.validate_call(envelope)
        with self.lock:
            previous = self.operations.get(key)
            if previous is None:
                raise ToolRejected("NATIVE_OPERATION_NOT_FOUND", 404)
        self.reconcile_effects(call.run_id, port)
        with self.lock:
            known = self.operations[key]
            if known["state"] != "completed":
                raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)
            if known.get("error"):
                raise ToolRejected(
                    known["error"],
                    known.get("error_status", 403),
                    details=known.get("error_details"),
                )
            return known["result"]

    def output_settled(self, run_id):
        with self.lock:
            attempts = [item for item in self.attempts.values() if item["run_id"] == run_id]
            return (
                bool(attempts)
                and all(item["state"] == "completed" for item in attempts)
                and all(
                    item["state"] == "completed"
                    for item in self.operations.values()
                    if item["run_id"] == run_id
                )
                and all(
                    key in self.operations
                    for key, value in self.bindings.items()
                    if value["run_id"] == run_id and value["tool_name"] == "stage_result"
                )
            )

    def effects_settled(self, run_id, *, allow_not_dispatched=False):
        with self.lock:
            states = {"completed", "not_dispatched"} if allow_not_dispatched else {"completed"}
            return all(
                item["state"] in states
                for item in self.operations.values()
                if item["run_id"] == run_id
            ) and all(
                key in self.operations
                for key, value in self.bindings.items()
                if value["run_id"] == run_id and value["tool_name"] == "stage_result"
            )

    def _require_settled_effects(self, run_id, port, *, allow_not_dispatched=False):
        if self.effects_settled(run_id, allow_not_dispatched=allow_not_dispatched):
            return
        if port is not None:
            self.reconcile_effects(run_id, port)
        if not self.effects_settled(run_id, allow_not_dispatched=allow_not_dispatched):
            raise ToolRejected("NATIVE_OPERATION_UNKNOWN", 409)

    def transport_closed(self, run_id):
        with self.lock:
            return all(
                item.get("network", {}).get("downstream_closed") is True
                and item.get("network", {}).get("upstream_closed") is True
                and item.get("network", {}).get("watcher_closed") is True
                for item in self.attempts.values()
                if item["run_id"] == run_id
            )

    def release(self, receipt, port):
        lease = {k: receipt[k] for k in ["session_id", "run_id", "request_id", "epoch"]}
        with self.lock:
            if lease["run_id"] in self.released:
                return
            self.assert_releasable(lease, port)
            if receipt.get("released") is not True or receipt.get("durable") is not True:
                raise ToolRejected("NATIVE_RELEASE_RECEIPT")
            self._record({"event": "release", "run_id": lease["run_id"], "receipt": receipt})

    def retire_unstarted(self, lease, port):
        with self.lock:
            if any(item["run_id"] == lease["run_id"] for item in self.attempts.values()):
                return False
            receipt = port.retire_unstarted(lease)
            if receipt is None:
                return False
            if lease["run_id"] not in self.retired_unstarted:
                self._record(
                    {"event": "retired_unstarted", "run_id": lease["run_id"], "receipt": receipt}
                )
            return True


class NativeToolTransport:
    def __init__(self, registry: NativeCallRegistry, port: RepositoryTools):
        self.registry, self.port = registry, port
        if registry.business_port is not None and registry.business_port is not port:
            raise RuntimeError("NATIVE_REPOSITORY_PORT_CONFLICT")
        registry.business_port = port
        self.token = secrets.token_urlsafe(32)
        self.events = []
        self.fault_hook = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, payload):
                raw = canonical(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.send_header("cache-control", "no-store")
                self.send_header("connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    # The original receipt is already durable. A stopped native
                    # caller may close its socket before this acknowledgment.
                    owner.events.append({"path": self.path, "response_disconnected": True})
                self.close_connection = True

            def do_POST(self):
                try:
                    if (
                        self.headers.get("Origin")
                        or self.headers.get("Host") != f"127.0.0.1:{owner.server.server_port}"
                        or not hmac.compare_digest(
                            self.headers.get("x-learning-business", ""), owner.token
                        )
                    ):
                        raise ToolRejected("NATIVE_BRIDGE_AUTH")
                    if self.path not in {
                        "/bridge/invoke",
                        "/bridge/release-check",
                        "/bridge/reconcile",
                    }:
                        raise ToolRejected("NATIVE_BRIDGE_ROUTE", 404)
                    lengths = self.headers.get_all("Content-Length", [])
                    if (
                        len(lengths) != 1
                        or self.headers.get("Transfer-Encoding")
                        or self.headers.get_content_type() != "application/json"
                    ):
                        raise ToolRejected("NATIVE_BRIDGE_BODY", 422)
                    length = int(lengths[0])
                    if not 0 < length <= 24 * 1024 * 1024:
                        raise ToolRejected("NATIVE_BRIDGE_SIZE", 413)
                    self.connection.settimeout(5)
                    value = json.loads(self.rfile.read(length))
                    if self.path == "/bridge/invoke":
                        result = owner.registry.invoke(value, owner.port)
                    elif self.path == "/bridge/reconcile":
                        result = owner.registry.reconcile_call(value, owner.port)
                    else:
                        result = owner.registry.release_evidence(value, owner.port)
                    owner.events.append({"path": self.path, "allowed": True})
                    mode = owner.fault_hook(self.path, value, result) if owner.fault_hook else None
                    if mode in {"disconnect", "truncate"}:
                        if mode == "truncate":
                            raw = canonical({"ok": True, "result": result}).encode()
                            self.send_response(200)
                            self.send_header("content-type", "application/json")
                            self.send_header("content-length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw[: max(1, len(raw) // 2)])
                            self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        self.close_connection = True
                        owner.events.append({"path": self.path, "fault_injected": mode})
                        return
                    self.reply(200, {"ok": True, "result": result})
                except ToolRejected as error:
                    owner.events.append({"path": self.path, "allowed": False, "code": error.code})
                    self.reply(
                        error.status,
                        {
                            "ok": False,
                            "error": {
                                "code": error.code,
                                **({"details": error.details} if error.details else {}),
                            },
                        },
                    )
                except (ValueError, TypeError, OSError):
                    self.reply(422, {"ok": False, "error": {"code": "NATIVE_BRIDGE_BODY"}})

            def do_GET(self):
                self.reply(404, {"error": {"code": "NATIVE_BRIDGE_ROUTE"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.url = f"http://127.0.0.1:{self.server.server_port}/bridge/invoke"
        self._started = False
        self._closed = False

    def start(self):
        if self._closed:
            raise RuntimeError("NATIVE_BRIDGE_CLOSED")
        if not self._started:
            self.thread.start()
            self._started = True
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._started:
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()
