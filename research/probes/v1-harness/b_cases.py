"""One bounded B observation, with no credentials, transport, or retry policy.

The caller owns the Peer and counted boundary. This helper sends at most one
prompt, reconciles from a real follow baseline, and returns private evidence.
Mathematical correctness remains a separate review; transport success is never
reported as a passed mathematical case.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import time
import uuid


CONTROL_TIMEOUT_SECONDS = 10
CANCEL_SETTLE_SECONDS = 10
POLL_SECONDS = 0.025


def _value(row):
    return row.get("wire", {}).get("value", {})


def _chunks(rows):
    return [row for row in rows if _value(row).get("type") == "assistant-stream"
            and _value(row).get("frame", {}).get("type") == "chunk"]


def _text_rows(rows):
    return [row for row in _chunks(rows)
            if _value(row)["frame"].get("chunk", {}).get("type") == "text-delta"]


def _activity_rows(rows):
    # Thinking is genuine model activity, although it is not useful answer
    # text. Waiting for a text block can miss an entire max-token generation.
    return [row for row in _chunks(rows)
            if _value(row)["frame"].get("chunk", {}).get("type") in {"text-delta", "reasoning-delta"}
            and _value(row)["frame"]["chunk"].get("text")]


def _terminals(rows):
    return [row for row in rows if _value(row).get("type") == "event"
            and _value(row).get("event", {}).get("type") == "turn/end"]


def _watermark(rows):
    values = [_value(row) for row in rows]
    candidates = [value.get("cursor") for value in values]
    candidates += [value.get("event", {}).get("seq") for value in values]
    return max([value for value in candidates if type(value) is int] or [0])


def _text_fields(value):
    if isinstance(value, dict):
        result = [value["text"]] if isinstance(value.get("text"), str) else []
        for key, child in value.items():
            if key != "text":
                result.extend(_text_fields(child))
        return result
    if isinstance(value, list):
        return [text for child in value for text in _text_fields(child)]
    return []


async def execute_case(peer, boundary, sid, stream, text, image, should_cancel,
                       case_id, deadline_seconds=180):
    """Return one private case record, retaining errors and a stop_group flag.

    A timeout starts before prompt submission. Cancellation and reconciliation
    have their own bounded cleanup allowance; neither starts a second prompt.
    No receipt/terminal is inferred from final answer text.
    """
    started_ns = time.time_ns()
    started = time.monotonic()
    deadline = started + deadline_seconds
    offset = len(peer.streams[stream])
    floor_seq = _watermark(peer.streams[stream])
    before = len(boundary.records)
    request_id = str(uuid.uuid4())
    errors = []
    receipt = None
    cancellation = {"sent": False, "cause": None, "rpc": None,
                    "active_before_cancel": False, "sent_ns": None}
    timeout_observed = False
    prompt_error = False
    reconciled = None
    baseline = None
    reconciliation_stream = None

    def current():
        return peer.streams[stream][offset:]

    def error(stage, exc):
        errors.append({"stage": stage, "type": type(exc).__name__, "message": str(exc)})

    async def cancel(cause):
        if cancellation["sent"]:
            return
        rows = current()
        cancellation.update(sent=True, cause=cause, sent_ns=time.time_ns(),
                            active_before_cancel=bool(_activity_rows(rows)) and not _terminals(rows))
        try:
            cancellation["rpc"] = await asyncio.wait_for(
                peer.rpc("session/cancel", {"sessionId": sid}), CONTROL_TIMEOUT_SECONDS)
        except Exception as exc:
            error("cancel", exc)

    try:
        if not 0 < deadline_seconds <= 180:
            raise ValueError("OBSERVATION_DEADLINE_MUST_BE_0_TO_180_SECONDS")
        try:
            receipt = await asyncio.wait_for(
                peer.prompt(sid, text, image, request_id=request_id),
                max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            timeout_observed = True
            prompt_error = True
            error("prompt_timeout", exc)
            await cancel("observation_timeout")
        except Exception as exc:
            prompt_error = True
            error("prompt", exc)
            # The request may have been accepted before its receipt was lost.
            await cancel("submission_receipt_unknown")

        if not prompt_error:
            while not _terminals(current()):
                if should_cancel and _activity_rows(current()):
                    await cancel("requested_active_stop")
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timeout_observed = True
                    await cancel("observation_timeout")
                    break
                await asyncio.sleep(min(POLL_SECONDS, remaining))

        settle_deadline = time.monotonic() + CANCEL_SETTLE_SECONDS
        while cancellation["sent"] and not _terminals(current()) and time.monotonic() < settle_deadline:
            await asyncio.sleep(POLL_SECONDS)

        # A new real baseline supplies its actual cursor, including a terminal
        # missed by the old observer. Never invent an afterCursor/throughSeq.
        try:
            reconciliation_stream = await asyncio.wait_for(peer.follow(sid), CONTROL_TIMEOUT_SECONDS)
            rows = peer.streams[reconciliation_stream]
            baseline = _value(rows[0]) if rows else None
            if not baseline or baseline.get("type") != "snapshot":
                raise ValueError("RECONCILIATION_BASELINE_NOT_SNAPSHOT")
            reconciled = await asyncio.wait_for(peer.rpc("session/page", {
                "address": {"kind": "session", "sessionId": sid},
                "throughSeq": _watermark(rows), "maxMessages": 100}), CONTROL_TIMEOUT_SECONDS)
        except Exception as exc:
            error("reconcile", exc)

        # A durable terminal can precede propagation of socket abort to the
        # proxy. Wait for this case's owned handlers before the next prompt.
        settle_deadline = time.monotonic() + CANCEL_SETTLE_SECONDS
        while time.monotonic() < settle_deadline:
            observations = boundary.records[before:]
            if observations and all(row.get("finished_ns") for row in observations):
                break
            if not observations and not cancellation["sent"]:
                break
            await asyncio.sleep(POLL_SECONDS)
    except Exception as exc:
        error("case", exc)

    rows = current()
    texts = _text_rows(rows)
    answer = "".join(_value(row)["frame"]["chunk"]["text"] for row in texts)
    observations = boundary.records[before:]
    page_events = [item.get("event", {}) for item in (reconciled or {}).get("records", [])
                   if item.get("type") == "event"]
    new_events = [event for event in page_events
                  if type(event.get("seq")) is int and event["seq"] > floor_seq]
    durable_ends = [event for event in new_events if event.get("type") == "turn/end"]
    terminal = durable_ends[-1] if durable_ends else None
    reason = terminal.get("data", {}).get("reason") if terminal else None
    reason_kind = reason.get("kind") if isinstance(reason, dict) else reason
    preserved = "".join(_text_fields([
        event.get("data", {}).get("message", {}) for event in new_events
        if event.get("type") == "assistant/message"]))
    if baseline:
        preserved += "".join(_text_fields(baseline.get("assistantStream", {})))
    partial_preserved = bool(answer) and answer in preserved
    activity = _activity_rows(rows)
    generated_content = "".join(_value(row)["frame"]["chunk"]["text"] for row in activity)
    partial_generated_preserved = bool(generated_content) and generated_content in preserved
    cancel_rpc = cancellation["rpc"]
    cancel_evidence = {
        "active_generation_observed_before_cancel": cancellation["active_before_cancel"],
        "cancel_rpc_accepted": isinstance(cancel_rpc, dict) and cancel_rpc.get("accepted") is True,
        "provider_boundary_abort_observed": any(row.get("cancel_observed") is True for row in observations),
        "durable_aborted_terminal": reason_kind == "aborted",
        "partial_generated_content_preserved_in_snapshot_or_page": partial_generated_preserved,
    }
    active_cancel_proven = (should_cancel and cancellation["cause"] == "requested_active_stop"
                            and all(cancel_evidence.values()))
    handlers_finished = bool(observations) and all(row.get("finished_ns") for row in observations)
    unsafe_boundary = any(row.get("status") in {"configuration_blocked", "budget_blocked"}
                          for row in observations)
    unknown_execution = any(row.get("status") in {"upstream_unknown", "upstream_unsettled", "dispatching", "received"}
                            and not (cancel_evidence["provider_boundary_abort_observed"] and reason_kind == "aborted")
                            for row in observations)
    terminal_known = terminal is not None and reconciled is not None
    stop_group = (not terminal_known or not handlers_finished or unsafe_boundary or unknown_execution
                  or prompt_error or bool(errors) or reason_kind not in {"completed", "aborted"})
    transport_status = "unknown" if stop_group else "passed"
    if should_cancel and not active_cancel_proven:
        transport_status = "unknown"
    observed_terminals = _terminals(rows)
    # Windows wall-clock samples can tie for adjacent frames. Preserve the
    # actual observer order as well as timestamps instead of inventing latency.
    live_text = (bool(texts) and bool(observed_terminals)
                 and rows.index(texts[0]) < rows.index(observed_terminals[0])
                 and texts[0]["received_ns"] <= observed_terminals[0]["received_ns"])
    private = Path(boundary.private)
    request_refs = []
    for row in observations:
        request_path = private / (row["request_id"] + ".request.json")
        response_path = private / (row["request_id"] + ".response.sse")
        request_refs.append({"request_id": row["request_id"],
                             "actual_outbound_attempt": row.get("actual_outbound_attempt"),
                             "request_ref": str(request_path) if request_path.is_file() else None,
                             "provider_sse_ref": str(response_path) if response_path.is_file() else None,
                             "body_sha256": row.get("body_sha256"), "images": row.get("images", []),
                             "usage": row.get("usage")})
    return {"case_id": case_id, "status": "unknown", "transport_status": transport_status,
            "math_review": "pending_review", "stop_group": stop_group,
            "session_id": sid, "planned_request_id": request_id, "receipt": receipt,
            "generation_ids": list(dict.fromkeys(_value(row)["frame"].get("attemptId") for row in _chunks(rows))),
            "requested_active_cancel": should_cancel, "cancel_sent": cancellation["sent"],
            "cancellation": cancellation, "active_cancel_evidence": cancel_evidence,
            "active_cancel_proven": active_cancel_proven, "observation_timed_out": timeout_observed,
            "observation_limit_seconds": deadline_seconds, "terminal_reason": reason,
            "transport_complete": terminal_known, "boundary_handlers_finished": handlers_finished,
            "real_text_delta_before_terminal": live_text, "partial_text_preserved": partial_preserved,
            "partial_generated_content_preserved": partial_generated_preserved,
            "activity_kinds": list(dict.fromkeys(_value(row)["frame"]["chunk"]["type"] for row in activity)),
            "answer": answer, "time_to_first_text_ms": (texts[0]["received_ns"] - started_ns) / 1e6 if texts else None,
            "elapsed_ms": (time.time_ns() - started_ns) / 1e6,
            "boundary_requests": observations, "actual_input_and_usage_refs": request_refs,
            "snapshot": baseline, "reconciled_page": reconciled, "reconciliation_stream": reconciliation_stream,
            "errors": errors, "evidence_refs": ["private/peer-frames.json", str(boundary.ledger.path)],
            "uncovered": (["Requested active cancellation was not proved by all five observations"]
                          if should_cancel and not active_cancel_proven else [])}
