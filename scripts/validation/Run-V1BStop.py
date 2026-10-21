"""One explicitly reconciled B05 retest, sharing the original six-attempt ledger."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "research/probes/v1-harness"
sys.path.insert(0, str(PROBE))
from runner import Host, Peer, save, source_digests, load_b_key
from fixture import BoundaryServer
from b_cases import execute_case


async def main(args):
    source = Path(args.source_run).resolve()
    assert source.is_relative_to(REPO / ".runtime/validation")
    prior = json.loads((source / "cases/V1-B05.json").read_text(encoding="utf-8"))
    assert prior["terminal_reason"] == {"kind": "max-tokens"}
    assert all(r["status"] == "completed" and r["usage"]["settled"] for r in prior["boundary_requests"])
    gate = json.loads((REPO / ".runtime/validation/v1-a-final-20261021-r3/a-gate.json").read_text(encoding="utf-8"))
    changed = {name for name, value in source_digests().items() if gate["source_digests"].get(name) != value}
    assert changed == {"b_cases.py", "test_b_cases.py"}, changed
    local = json.loads((REPO / ".runtime/validation/v1-b-stop-fix-local/results.json").read_text(encoding="utf-8"))
    assert local["source_digests"] == source_digests() and local["real_attempts"] == 0
    assert local["cases"][0]["active_cancel_proven"] and local["cases"][1]["transport_status"] == "passed"
    directory = REPO / ".runtime/validation" / args.run_id
    if directory.exists():
        raise ValueError("REFUSE_EXISTING_RETEST_DIRECTORY")
    directory.mkdir(parents=True)
    boundary = None;host = None;peer = None
    try:
        if args.fixture_check:
            boundary = BoundaryServer(directory / "boundary", mode="A", limit=0)
        else:
            ledger = REPO / ".runtime/validation/v1-b-authorization-2026-10-21/boundary-ledger.jsonl"
            entries = [json.loads(s) for s in ledger.read_text(encoding="utf-8").splitlines()]
            assert sum(e["event"] == "upstream_reserved" for e in entries) == 5
            boundary = BoundaryServer(ledger.parent, mode="B", limit=6, key=load_b_key())
            assert boundary.actual_outbound_attempts == 5
        boundary.start()
        host = Host(directory / "host", boundary.url, local_token=boundary.local_token)
        # Preserve the original evidence. Resume a private copy of that exact
        # completed Session, rather than replacing/replaying its answer.
        shutil.copytree(source / "host/home", host.home, dirs_exist_ok=True)
        host.env["V1_STAGE"] = "B"
        await asyncio.to_thread(host.start)
        peer = await Peer(host).connect()
        stream = await peer.follow("v1-b-c1")
        baseline = peer.streams[stream][0]["wire"]["value"]
        assert baseline["header"]["id"] == "v1-b-c1" and baseline["cursor"] >= 31
        assert not baseline["assistantStream"].get("activeAttempt")
        inbox = baseline["projections"]["values"]["inbox"]
        assert not inbox["next-turn"] and not inbox["next-step"]
        save(directory / "private/reconciled-before-retest.json", baseline)
        if args.fixture_check:
            boundary.set_scenario(thinking_chunks=["Restored reasoning-only active generation."],
                                  text_chunks=[], delay=.15, hold_seconds=10)
        save(directory / "patch-provenance.json", {"old_A_source_digests": gate["source_digests"],
             "current_source_digests": source_digests(), "only_changed_components": sorted(changed),
             "reason": "B observer waited for answer text and missed a reasoning-only max-token generation",
             "local_regression": ".runtime/validation/v1-b-stop-fix-local/results.json",
             "prior_case_terminal": prior["terminal_reason"], "is_unknown_request_retry": False,
             "binding_epoch": 2, "coordinator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        row = await execute_case(peer, boundary, "v1-b-c1", stream,
            "请按更正后的题目详细推导，展开说明泰勒余项、洛必达和两种方法的适用条件，再比较步骤。",
            None, True, "V1-B05-retest")
        row.update(retest_of="V1-B05", binding_epoch=2, fixture_check=args.fixture_check)
        save(directory / "case-result.json", row)
        print(json.dumps({"transport": row["transport_status"], "active_cancel_evidence": row["active_cancel_evidence"],
                          "active_cancel_proven": row["active_cancel_proven"], "activity_kinds": row["activity_kinds"],
                          "terminal": row["terminal_reason"], "total_attempts": boundary.actual_outbound_attempts}), flush=True)
    finally:
        if peer:save(directory / "private/peer-frames.json", peer.frames);await peer.close()
        if host:host.stop()
        if boundary:boundary.stop();save(directory / "boundary-records.json", boundary.records)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture-check", action="store_true")
    asyncio.run(main(parser.parse_args()))
