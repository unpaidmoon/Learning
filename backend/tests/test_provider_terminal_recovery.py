"""Original terminal bytes and a closed exact transport can settle an attempt."""

import json

import pytest
from test_execution_boundary import frame

from learning_app.execution.boundary import MessagesBoundary, OutboundLedger, UsageObserver
from learning_app.execution.native_tools import NativeCallRegistry


@pytest.mark.parametrize(
    "variant",
    ["complete", "partial", "open_network", "wrong_calls", "wrong_owner", "duplicate_terminal"],
)
def test_cold_terminal_recovery_is_read_only_exact_and_conservative(tmp_path, variant):
    path = tmp_path / "boundary"
    private = path / "private/boundary"
    private.mkdir(parents=True)
    journal = tmp_path / "native-calls.jsonl"
    registry = NativeCallRegistry(journal)
    scope = {
        "session_id": "owned_session",
        "run_id": "owned_run",
        "request_id": "owned_request",
        "epoch": 1,
    }
    registry.bind(scope)
    row = {
        "request_id": "provider_attempt",
        "generation_id": scope["run_id"],
        "native_session_id": scope["session_id"],
        "host_dispatch_id": "00000000-0000-0000-0000-000000000001",
        "mode": "official",
        "upstream_status": 200,
        "status": "upstream_unknown",
        "finished_ns": 1,
        "upstream_transport_closed": True,
        "upstream_watcher_closed": True,
    }
    ledger = OutboundLedger(path / "boundary-ledger.jsonl")
    ledger.append({"event": "request_received", **row})
    row["actual_outbound_attempt"] = ledger.dispatch(row["request_id"], scope["run_id"])
    registry.begin_attempt(row, row["host_dispatch_id"])
    data = frame(
        {
            "type": "message_start",
            "message": {"id": "original_message", "usage": {"input_tokens": 5}},
        }
    ) + frame({"type": "message_delta", "usage": {"output_tokens": 7}})
    terminal = frame({"type": "message_stop"})
    if variant != "partial":
        data += terminal
    if variant == "duplicate_terminal":
        data += terminal
    registry.feed(row, data)
    registry.finish_attempt(row, "unknown")
    registry.network_closed(row, downstream_closed=variant != "open_network")
    if variant == "wrong_calls":
        # A complete raw tool vector that was never captured in the journal
        # cannot authorize a fabricated effect or an inferred continuation.
        data = data.replace(terminal, b"") + (
            frame(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "uncaptured",
                        "name": "stage_result",
                        "input": {},
                    },
                }
            )
            + frame({"type": "content_block_stop", "index": 0})
            + terminal
        )
    if variant == "wrong_owner":
        row["generation_id"] = "different_run"
    observer = UsageObserver(row["request_id"])
    observer.feed(data)
    row["usage"] = observer.result()
    ledger.append({"event": "request_finished", **row})
    ledger.close()
    raw = private / "provider_attempt.response.sse"
    raw.write_bytes(data)
    before = raw.read_bytes()
    recovered_registry = NativeCallRegistry(journal)
    boundary = MessagesBoundary(
        path, mode="official", api_key="synthetic-key", native_registry=recovered_registry
    )
    try:
        attempt = recovered_registry.attempts["provider_attempt"]
        assert attempt["state"] == ("completed" if variant == "complete" else "unknown")
        assert boundary.actual_outbound_attempts == 1
        assert raw.read_bytes() == before and recovered_registry.bindings == {}
        if variant == "complete":
            assert boundary.records[0]["status_before_reconciliation"] == "upstream_unknown"
            assert boundary.records[0]["status"] == "completed"
            assert recovered_registry.output_settled(scope["run_id"])
            assert boundary.records[0]["usage"]["normalized"]["output_tokens"] == 7
        else:
            assert not recovered_registry.output_settled(scope["run_id"])
    finally:
        boundary.stop()
    # Reopening an already reconciled journal is idempotent and never resends.
    persisted = journal.read_bytes()
    again = MessagesBoundary(
        path, mode="official", api_key="synthetic-key", native_registry=NativeCallRegistry(journal)
    )
    try:
        assert again.actual_outbound_attempts == 1 and journal.read_bytes() == persisted
    finally:
        again.stop()
    events = [json.loads(line) for line in journal.read_text("utf-8").splitlines()]
    assert sum(event["event"] == "attempt_reconciled" for event in events) == (
        variant == "complete"
    )
