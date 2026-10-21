"""Deterministic fake Peer/boundary checks; never starts Harness or a network."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import b_cases


class FakeBoundary:
    def __init__(self):
        self._records = []
        self.private = Path(".runtime/private/boundary")
        self.ledger = SimpleNamespace(path=Path(".runtime/boundary-ledger.jsonl"))

    @property
    def records(self):
        return copy.deepcopy(self._records)


class FakePeer:
    def __init__(self, boundary, mode="normal"):
        self.boundary, self.mode = boundary, mode
        self.streams = {"initial": [{"received_ns": time.time_ns(), "wire": {
            "value": {"type": "snapshot", "cursor": 10}}}]}
        self.events = []
        self.prompt_calls = 0
        self.cancel_calls = 0

    def event(self, name, data, seq):
        event = {"type": name, "seq": seq, "data": data}
        self.events.append(event)
        self.streams["initial"].append({"received_ns": time.time_ns(), "wire": {
            "value": {"type": "event", "event": event}}})

    def finish(self, reason):
        kind = "reasoning" if self.mode == "reasoning_active" else "text"
        self.event("assistant/message", {"message": {"content": [{"type": kind, "text": "partial formula"}]}}, 12)
        self.event("turn/end", {"reason": {"kind": reason}}, 13)
        self.boundary._records[0].update(status="completed", finished_ns=time.time_ns())

    async def prompt(self, sid, text, image, request_id=None):
        self.prompt_calls += 1
        if self.mode == "slow_receipt":
            await asyncio.sleep(0.2)
        self.boundary._records.append({"request_id": "fake-request", "status": "received",
                                      "actual_outbound_attempt": 1, "usage": {"settled": False}})
        self.streams["initial"].append({"received_ns": time.time_ns(), "wire": {"value": {
            "type": "assistant-stream", "frame": {"type": "chunk", "attemptId": "C1:1",
            "chunk": {"type": "reasoning-delta" if self.mode == "reasoning_active" else "text-delta", "text": "partial formula"}}}}})
        if self.mode == "receipt_lost":
            raise ConnectionError("acknowledgement lost")
        if self.mode in {"normal", "page_failure"}:
            self.finish("completed")
        return {"session_id": sid, "request_id": request_id}

    async def rpc(self, method, request):
        if method == "session/cancel":
            self.cancel_calls += 1
            if self.mode == "cancel_failure":
                raise ConnectionError("cancel link lost")
            if self.mode == "rejected_cancel":
                self.finish("completed")
                return {"accepted": False}
            if self.boundary._records:
                self.finish("aborted")
                self.boundary._records[0].update(status="upstream_unknown", cancel_observed=True)
            return {"accepted": True}
        if method == "session/page":
            if self.mode == "page_failure":
                raise ConnectionError("page unavailable")
            return {"records": [{"type": "event", "event": event} for event in self.events]}
        raise AssertionError(method)

    async def follow(self, sid):
        self.streams["reconciled"] = [{"received_ns": time.time_ns(), "wire": {"value": {
            "type": "snapshot", "cursor": 13, "assistantStream": {}}}}]
        return "reconciled"


class BCaseTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, mode, should_cancel=False, deadline=0.1):
        boundary = FakeBoundary()
        peer = FakePeer(boundary, mode)
        with patch.multiple(b_cases, CANCEL_SETTLE_SECONDS=0.04, CONTROL_TIMEOUT_SECONDS=0.04, POLL_SECONDS=0.001):
            row = await b_cases.execute_case(peer, boundary, "C1", "initial", "synthetic", None,
                                             should_cancel, "V1-B-test", deadline)
        self.assertEqual(peer.prompt_calls, 1)
        return row, peer

    async def test_normal_transport_does_not_claim_math_pass(self):
        row, peer = await self.execute("normal")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["transport_status"], "passed")
        self.assertFalse(row["stop_group"])
        self.assertTrue(row["real_text_delta_before_terminal"])
        self.assertIsNone(row["actual_input_and_usage_refs"][0]["provider_sse_ref"])
        self.assertEqual(peer.cancel_calls, 0)

    async def test_activity_cancel_requires_all_five_observations(self):
        row, peer = await self.execute("active", True)
        self.assertTrue(all(row["active_cancel_evidence"].values()))
        self.assertTrue(row["active_cancel_proven"])
        self.assertFalse(row["stop_group"])
        self.assertEqual(peer.cancel_calls, 1)

    async def test_rejected_cancel_is_not_active_cancel_success(self):
        row, peer = await self.execute("rejected_cancel", True)
        self.assertFalse(row["active_cancel_proven"])
        self.assertEqual(row["transport_status"], "unknown")
        self.assertFalse(row["active_cancel_evidence"]["cancel_rpc_accepted"])

    async def test_reasoning_only_generation_is_active_and_preserved(self):
        row, peer = await self.execute("reasoning_active", True)
        self.assertEqual(row["cancellation"]["cause"], "requested_active_stop")
        self.assertTrue(row["active_cancel_proven"])
        self.assertTrue(row["partial_generated_content_preserved"])
        self.assertEqual(row["answer"], "")
        self.assertFalse(row["partial_text_preserved"])
        self.assertEqual(row["activity_kinds"], ["reasoning-delta"])
        self.assertEqual(peer.cancel_calls, 1)

    async def test_prompt_timeout_includes_receipt_wait(self):
        row, peer = await self.execute("slow_receipt", deadline=0.015)
        self.assertTrue(row["observation_timed_out"])
        self.assertEqual(row["cancellation"]["cause"], "observation_timeout")
        self.assertTrue(row["stop_group"])
        self.assertEqual(peer.cancel_calls, 1)

    async def test_lost_receipt_retains_case_and_never_resubmits(self):
        row, peer = await self.execute("receipt_lost")
        self.assertIsNone(row["receipt"])
        self.assertEqual(row["errors"][0]["stage"], "prompt")
        self.assertEqual(row["case_id"], "V1-B-test")
        self.assertTrue(row["stop_group"])
        self.assertEqual(peer.cancel_calls, 1)

    async def test_cancel_failure_retains_unknown_and_stops_group(self):
        row, peer = await self.execute("cancel_failure", True)
        self.assertTrue(row["stop_group"])
        self.assertFalse(row["transport_complete"])
        self.assertEqual(row["errors"][0]["stage"], "cancel")

    async def test_page_failure_cannot_reuse_old_terminal(self):
        row, peer = await self.execute("page_failure")
        self.assertTrue(row["stop_group"])
        self.assertFalse(row["transport_complete"])
        self.assertEqual(row["errors"][0]["stage"], "reconcile")


if __name__ == "__main__":
    unittest.main()
