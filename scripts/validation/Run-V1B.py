"""V1-B coordinator with a user-handwriting handoff; not an application.

Reuses the A-tested execution components without changing their frozen source.
The optional local rehearsal never loads credentials or forwards Internet traffic.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "research/probes/v1-harness"
sys.path.insert(0, str(PROBE))
from runner import Host, Peer, save, source_digests, load_b_key
from fixture import BoundaryServer
from b_cases import execute_case
from samples import generate
import httpx


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def run(args):
    directory = REPO / ".runtime/validation" / args.run_id
    if directory.exists():
        raise ValueError("REFUSE_EXISTING_RUN_DIRECTORY")
    directory.mkdir(parents=True)
    gate = json.loads(Path(args.a_gate).read_text(encoding="utf-8"))
    review = json.loads(Path(args.human_review).read_text(encoding="utf-8"))
    expected = {f"V1-A{i:02d}" for i in range(1, 13)}
    if (len(gate["cases"]) != 12 or {c["case_id"] for c in gate["cases"]} != expected
            or any(c["status"] != "passed" or not all(c["assertions"].values()) for c in gate["cases"])
            or gate.get("real_model_attempts") != 0 or gate.get("source_changed_during_run") is not False
            or gate["source_digests"] != source_digests()):
        raise ValueError("A_GATE_NOT_PASSED_OR_SOURCE_CHANGED")
    if review.get("confirmed") is not True or not review.get("message_reference"):
        raise ValueError("HUMAN_REVIEW_MISSING")
    samples = generate(directory)
    for key in ["M01", "L02"]:
        if any(review["samples"][key].get(field) != samples[key][field]
               for field in ["sha256", "revision", "expected"]):
            raise ValueError("FROZEN_SAMPLE_MISMATCH")
    save(directory / "preflight.json", {"A_gate": str(Path(args.a_gate)),
         "source_digests": source_digests(), "coordinator_sha256": sha(__file__),
         "human_review": review, "fixture_check": args.fixture_check,
         "model": "deepseek-flash", "reasoning_effort": "high", "max_tokens": 8192,
         "attempt_limit": 0 if args.fixture_check else 6, "observation_seconds": 180})
    boundary = None
    host = None
    peer = None
    rows = []
    state = {"status": "starting", "fixture_check": args.fixture_check, "cases": []}

    def progress(**values):
        state.update(values)
        save(directory / "progress.json", state)
        print(json.dumps({k: state[k] for k in ["status", "cases", "attempts"] if k in state}), flush=True)

    try:
        if args.fixture_check:
            boundary = BoundaryServer(directory / "local-boundary", mode="A", limit=0)
        else:
            key = load_b_key()
            boundary = BoundaryServer(REPO / ".runtime/validation/v1-b-authorization-2026-10-21",
                                      mode="B", limit=6, key=key)
            del key
        boundary.start()
        host = Host(directory / "host", boundary.url, local_token=boundary.local_token)
        host.env["V1_STAGE"] = "B"
        await asyncio.to_thread(host.start)
        peer = await Peer(host).connect()
        c1, s1 = await peer.create("v1-b-c1")
        c2, s2 = await peer.create("v1-b-c2")
        c3, s3 = await peer.create("v1-b-c3")
        async with httpx.AsyncClient(trust_env=False, timeout=10) as control:
            response = await control.get(f"http://127.0.0.1:{host.control_port}/state",
                                        headers={"x-v1-control": host.control_token})
            response.raise_for_status()
            effective = response.json()
        save(directory / "effective-state.json", effective)
        if (effective.get("tools") != ["read_course_material"]
                or effective.get("networkGuardInstalled") is not True or not effective.get("activation")):
            raise ValueError("B_EFFECTIVE_PROFILE_UNPROVEN")
        stop = False
        for case, sid, stream, text, image, cancel in [
            ("V1-B01", c1, s1, "请读取确认题图并完整解答，保留公式和必要推导。", samples["M01"]["path"], False),
            ("V1-B02", c2, s2, "请读取确认题图并求特征值及相应特征向量，解释步骤。", samples["L02"]["path"], False),
            ("V1-B03", c3, s3, "这是我写的题目和过程。请检查哪里错了，沿着我的步骤解释。", None, False),
            ("V1-B04", c1, s1, "刚才分母应是2x³，其余不变。请按更正后的条件重新解答。", None, False),
            ("V1-B05", c1, s1, "请按更正后的题目详细推导，展开说明泰勒余项、洛必达和两种方法的适用条件，再比较步骤。", None, True),
            ("V1-B06", c1, s1, "按更正后的分母解释泰勒展开为什么可用，并给出最终极限。", None, False),
        ]:
            if stop or (not args.fixture_check and boundary.actual_outbound_attempts >= 6):
                row = {"case_id": case, "status": "not_run",
                       "uncovered": ["Prior execution unresolved or shared six-attempt budget exhausted"]}
                rows.append(row)
                save(directory / "cases" / (case + ".json"), row)
                continue
            if case == "V1-B03":
                if args.fixture_check:
                    image = samples["M01"]["path"]
                else:
                    manifest_path = Path(args.handwriting_manifest)
                    progress(status="waiting_for_user_handwriting", attempts=boundary.actual_outbound_attempts)
                    while not manifest_path.is_file():
                        await asyncio.sleep(0.25)
                    handwriting = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if handwriting.get("skip") is True:
                        row = {"case_id": case, "status": "not_run", "uncovered": ["User elected to omit real handwriting"]}
                        rows.append(row)
                        save(directory / "cases" / (case + ".json"), row)
                        continue
                    image = Path(handwriting["path"]).resolve()
                    private_root = (REPO / ".runtime/validation/v1-handwriting").resolve()
                    if (not image.is_relative_to(private_root) or handwriting.get("source_kind") != "user_handwriting"
                            or handwriting.get("user_confirmed_scope") is not True or sha(image) != handwriting["sha256"]
                            or image.suffix.lower() != ".png"):
                        raise ValueError("HANDWRITING_SCOPE_OR_HASH_INVALID")
                    save(directory / "private/handwriting-manifest.json", handwriting)
            progress(status="executing_" + case, attempts=boundary.actual_outbound_attempts)
            if args.fixture_check:
                boundary.set_scenario(text_chunks=["Local coordinator fixture ", case], delay=.08,
                                      hold_seconds=10 if cancel else 0)
            row = await execute_case(peer, boundary, sid, stream, text, image, cancel, case)
            row["human_expected_ref"] = review
            row["evidence_level"] = "local A fixture" if args.fixture_check else "real official model"
            rows.append(row)
            save(directory / "cases" / (case + ".json"), row)
            save(directory / "private/peer-frames.json", peer.frames)
            stop = row["stop_group"]
            state["cases"].append({"id": case, "transport": row["transport_status"],
                                   "active_cancel_proven": row["active_cancel_proven"]})
            progress(status="case_recorded", attempts=boundary.actual_outbound_attempts)
        progress(status="finished", attempts=boundary.actual_outbound_attempts)
    except Exception as error:
        save(directory / "failure.json", {"type": type(error).__name__, "message": str(error)})
        progress(status="failed", attempts=boundary.actual_outbound_attempts if boundary else 0)
        raise
    finally:
        if peer:
            save(directory / "private/peer-frames.json", peer.frames)
            await peer.close()
        if host:
            host.stop()
        if boundary:
            boundary.stop()
            save(directory / "boundary-records.json", boundary.records)
        save(directory / "b-results.json", rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--a-gate", required=True)
    parser.add_argument("--human-review", required=True)
    parser.add_argument("--handwriting-manifest")
    parser.add_argument("--fixture-check", action="store_true")
    args = parser.parse_args()
    if not args.fixture_check and not args.handwriting_manifest:
        parser.error("real B requires --handwriting-manifest (may arrive at the B03 handoff)")
    asyncio.run(run(args))
