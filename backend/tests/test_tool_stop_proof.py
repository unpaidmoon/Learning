"""Validation of captured tool-stop proof; synthetic journal only."""

import copy

import pytest

from learning_app.execution.native_tools import NativeCallRegistry, ToolRejected


def facts(tmp_path):
    registry = NativeCallRegistry(tmp_path / "facts.jsonl")
    lease = {"run_id": "run", "session_id": "sid", "request_id": "request", "epoch": 1}
    terminal = {"turn": 1, "seq": 12, "reason": {"kind": "aborted", "reason": {"kind": "user"}}}
    call = {
        "run_id": "run",
        "attempt_id": "attempt",
        "native_turn": 1,
        "native_step": 2,
        "native_call_seq": 10,
        "call_id": "call",
        "tool_name": "read_nearby_subtitles",
        "args_sha256": "a" * 64,
    }
    registry.bindings["sid:10"] = call
    registry.attempts["attempt"] = {
        "run_id": "run",
        "attempt_id": "attempt",
        "state": "completed",
        "network": {
            "downstream_closed": True,
            "upstream_closed": True,
            "watcher_closed": True,
            "finished_ns": 1,
        },
    }
    proof = {
        **lease,
        "native_terminal": terminal,
        "call": {
            k: call[k]
            for k in (
                "native_turn",
                "native_step",
                "native_call_seq",
                "call_id",
                "tool_name",
                "args_sha256",
            )
        },
        "native_cursor": 12,
        "native_cut_sha256": "b" * 64,
        "native_events_sha256": "d" * 64,
        "native_header_sha256": "e" * 64,
        "result_seq": 11,
    }
    return registry, lease, terminal, proof


def test_completed_attempts_keep_their_original_state_when_tool_stop_is_proved(tmp_path):
    registry, lease, terminal, proof = facts(tmp_path)
    registry._prove_tool_stop(lease, terminal, "closed_cancelled", proof)
    assert registry.attempts["attempt"]["state"] == "completed"
    restored = NativeCallRegistry(registry.path)
    assert restored.tool_stops["run"] == proof


@pytest.mark.parametrize(
    "change",
    [
        "no_attempt",
        "wrong_owner",
        "wrong_call",
        "wrong_terminal",
        "bad_result",
        "network_open",
        "unknown_attempt",
        "no_proof",
    ],
)
def test_missing_or_unsettled_facts_never_become_a_tool_stop(tmp_path, change):
    registry, lease, terminal, proof = facts(tmp_path)
    proof = copy.deepcopy(proof)
    if change == "no_attempt":
        registry.attempts.clear()
    elif change == "wrong_owner":
        proof["epoch"] = 2
    elif change == "wrong_call":
        proof["call"]["args_sha256"] = "c" * 64
    elif change == "wrong_terminal":
        proof["native_terminal"]["seq"] = 14
    elif change == "bad_result":
        proof["result_seq"] = terminal["seq"]
    elif change == "network_open":
        registry.attempts["attempt"]["network"]["upstream_closed"] = False
    elif change == "unknown_attempt":
        registry.attempts["attempt"]["state"] = "unknown"
    elif change == "no_proof":
        proof = None
    with pytest.raises(ToolRejected):
        registry._prove_tool_stop(lease, terminal, "closed_cancelled", proof)
    assert registry.tool_stops == {}
