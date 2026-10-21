"""Exercise formal application modules with synthetic, isolated P0/V2 data.

No research/probe module is imported. A recorded boundary event or a fault hook
triggers every interruption. Runtime evidence stays below .runtime/validation.
The exported result contains synthetic identities, hashes and field presence.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
MATRIX = json.loads((ROOT / "tests/fixtures/v2/cases.json").read_text("utf-8"))
HISTORY = json.loads((ROOT / "contracts/history-example.json").read_text("utf-8"))
CRASH_EXIT = 86


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def synthetic_attachment() -> bytes:
    return base64.b64decode(
        HISTORY["confirmed_attachment"]["fixture_base64"], validate=True
    )


def make_repository(case_dir: Path):
    from learning_app.storage import Repository

    repository = Repository(case_dir / "business")
    if not repository.list_courses():
        repository.create_course("合成数学课程 A", course_id="course_a")
        repository.create_course("合成数学课程 B", course_id="course_b")
        repository.create_chat("course_a", chat_id="chat_c1")
        repository.create_chat("course_b", chat_id="chat_other")
    return repository


def install_history(repository, case_dir: Path) -> None:
    source = HISTORY["source"]
    source_path = case_dir / source["display_name"]
    source_path.write_text(
        source["versions"][0]["fixture_content_utf8"], encoding="utf-8"
    )
    repository.register_source(
        "course_a",
        source_path,
        asset_id=source["asset_id"],
        display_name=source["display_name"],
        source_version="SOURCE-v1",
    )
    repository.stage_attachment(
        synthetic_attachment(),
        "image/png",
        confirmed=True,
        attachment_id="attach_crop_a1",
    )


def check(condition: Any, name: str, evidence: dict, **observed: Any) -> None:
    evidence.setdefault("assertions", []).append(
        {"name": name, "passed": bool(condition), **observed}
    )
    if not condition:
        raise AssertionError(name)


def crash_marker(case_dir: Path, point: str, details: Any = None) -> None:
    write_json(
        case_dir / "fault-event.json",
        {
            "event": point,
            "details": details,
            "worker_pid": os.getpid(),
            "at_ns": time.time_ns(),
            "action": "os._exit",
            "exit_code": CRASH_EXIT,
        },
    )
    os._exit(CRASH_EXIT)


def run_worker(case_dir: Path, case_id: str, *extra: str) -> dict:
    """Only the child created here can crash; its exit is the synchronization."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        case_id,
        "--case-dir",
        str(case_dir.resolve()),
        *extra,
    ]
    with (
        (case_dir / "worker.stdout.txt").open("w", encoding="utf-8") as stdout,
        (case_dir / "worker.stderr.txt").open("w", encoding="utf-8") as stderr,
    ):
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            timeout=45,
            check=False,
            **options,
        )
    marker_path = case_dir / "fault-event.json"
    return {
        "exit_code": completed.returncode,
        "fault_event": json.loads(marker_path.read_text("utf-8"))
        if marker_path.exists()
        else None,
    }


def worker(case_id: str, case_dir: Path) -> int:
    from learning_app.config import AppConfig
    from learning_app.execution.types import ExecutionBinding, NormalizedEvent
    from learning_app.service import GenerationService
    from learning_app.storage import Repository

    if not case_dir.resolve().is_relative_to((ROOT / ".runtime/validation").resolve()):
        raise ValueError("WORKER_DIRECTORY_OUTSIDE_VALIDATION_ROOT")
    targets = {"V2-01": "attachment_before_write", "V2-02": "attachment_after_publish"}
    if case_id in targets:

        def hook(point: str, details: dict):
            if point == targets[case_id]:
                crash_marker(case_dir, point, details)

        repository = Repository(case_dir / "business", fault_hook=hook)
        repository.stage_attachment(
            synthetic_attachment(), "image/png", attachment_id="attach_fault"
        )
        return 2
    if case_id == "V2-03":

        async def committed_before_dispatch():
            repository = Repository(case_dir / "business")

            def hook(point: str, details: dict):
                if point == "after_submission_committed_before_dispatch":
                    crash_marker(case_dir, point, details)

            service = GenerationService(
                repository, config_for(case_dir), execution=object(), fault_hook=hook
            )
            service.execution_ready = True
            await service.submit(question("sub_v2_03"))

        asyncio.run(committed_before_dispatch())
        return 2
    if case_id == "V2-06":
        payload = json.loads((case_dir / "terminal-to-ingest.json").read_text("utf-8"))

        def hook(point: str, details: dict):
            if point == "after_terminal_observed_before_ingest":
                crash_marker(case_dir, point, details)

        repository = Repository(case_dir / "business")
        service = GenerationService(
            repository,
            AppConfig(**payload["config"]),
            execution=object(),
            fault_hook=hook,
        )
        service._ingest(
            ExecutionBinding.from_dict(payload["binding"]),
            NormalizedEvent(**payload["event"]),
        )
        return 2
    raise ValueError("UNKNOWN_WORKER_CASE")


def config_for(case_dir: Path):
    from learning_app.config import AppConfig

    return AppConfig(
        data_dir=case_dir / "business",
        runtime_dir=case_dir / "service",
        mode="test",
        harness_enabled=False,
        model="deepseek-flash",
        thinking="high",
        max_output_tokens=16384,
        model_timeout_seconds=60,
    )


def question(
    submission_id: str,
    *,
    chat_id: str = "chat_c1",
    text: str = "计算 1+1，并给出说明。",
    attachments: list[str] | None = None,
) -> dict:
    return {
        "submission_id": submission_id,
        "chat_id": chat_id,
        "text": text,
        "model": "deepseek-flash",
        "thinking": "high",
        "attachment_ids": attachments or [],
    }


async def finish_task(service, generation_id: str, timeout: float = 30) -> None:
    task = service.tasks[generation_id]
    await asyncio.wait_for(asyncio.shield(task), timeout)


@asynccontextmanager
async def formal_host(case_dir: Path, evidence: dict):
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.service import GenerationService

    repository = make_repository(case_dir)
    adapter = ExecutionAdapter(ROOT, case_dir / "execution", mode="fixture", timeout=60)
    service = None
    try:
        await adapter.open()
        service = GenerationService(repository, config_for(case_dir), execution=adapter)
        await service.open()
        evidence["sqlite"] = repository.runtime_info()
        evidence["host_artifact"] = {
            "runtime_sha256": "f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365",
            "mode": "actual_fixed_Host_local_provider_fixture_no_cloud",
        }
        yield repository, adapter, service
    finally:
        if service:
            await service.close()
        evidence["cleanup"] = await adapter.close()
        repository.close()


async def native_evidence(adapter, repository, generation_id: str) -> dict:
    generation = repository.get_generation(generation_id)
    binding = generation.get("adapter_metadata", {})
    session_id = binding.get("session_id")
    if not session_id or adapter.peer is None:
        return {"status": "not_bound"}
    stream, snapshot = await adapter.peer.follow(session_id)
    try:
        page = await adapter.peer.page(session_id, snapshot)
        records = [
            r["event"] for r in page.get("records", []) if r.get("type") == "event"
        ]
        return {
            "session_id": session_id,
            "epoch": binding["epoch"],
            "request_id": binding["request_id"],
            "native_floor": binding["native_floor"],
            "native_turn": binding["native_turn"],
            "cursor": snapshot.get("cursor"),
            "active_attempt": snapshot.get("assistantStream", {}).get("activeAttempt"),
            "events": [
                {
                    "seq": e.get("seq"),
                    "type": e.get("type"),
                    "turn": e.get("data", {}).get("turn"),
                }
                for e in records
            ],
            "page_sha256": sha256(
                json.dumps(page, sort_keys=True, default=str).encode()
            ),
        }
    finally:
        await adapter.peer.unfollow(stream)


async def collect_evidence(repository, adapter, generation_id: str) -> dict:
    return {
        "business": repository.evidence(generation_id),
        "native": await native_evidence(adapter, repository, generation_id),
        "attachments": repository.attachment_inventory(),
        "boundary": {
            "records": adapter.boundary.records,
            "events": adapter.boundary.events,
            "actual_outbound_attempts": adapter.boundary.actual_outbound_attempts,
        },
    }


async def case_attachment_write(case_dir: Path, evidence: dict) -> None:
    from learning_app.storage import StorageError

    repository = make_repository(case_dir)
    evidence["sqlite"] = repository.runtime_info()
    repository.close()
    crashed = run_worker(case_dir, "V2-01")
    check(
        crashed["exit_code"] == CRASH_EXIT
        and crashed["fault_event"]["event"] == "attachment_before_write",
        "worker_crashed_at_confirmed_before_write_hook",
        evidence,
    )
    repository = make_repository(case_dir)
    original_open = Path.open

    def fail_partial_open(path, *args, **kwargs):
        if path.suffix == ".partial":
            raise OSError(28, "synthetic staging I/O failure")
        return original_open(path, *args, **kwargs)

    code = None
    try:
        with patch.object(Path, "open", fail_partial_open):
            repository.stage_attachment(
                synthetic_attachment(), "image/png", attachment_id="attach_disk_failure"
            )
    except StorageError as exc:
        code = exc.code
    try:
        check(
            code == "ATTACHMENT_WRITE_FAILED",
            "injected_disk_io_failure_has_stable_error",
            evidence,
            code=code,
        )
        counts = repository.counts()
        check(
            counts["messages"] == 0
            and counts["attachments"] == 0
            and counts["submissions"] == 0,
            "failure_does_not_publish_messages_or_attachments",
            evidence,
        )
        inventory = repository.attachment_inventory()
        check(
            not inventory["unregistered_object_hashes"]
            and inventory["partial_file_count"] == 0,
            "no_safe_published_asset_after_write_failure",
            evidence,
        )
        evidence.update(
            fault_event=crashed,
            business={"counts": counts},
            attachments=inventory,
            input_sha256=sha256(synthetic_attachment()),
            actual_dispatches={
                "local_messages": 0,
                "real_model": 0,
                "measurement": "no_execution_component_created",
            },
            usage={"status": "unknown", "fields": {}},
            scope="actual_storage_worker_os_crash_and_injected_filesystem_OSError",
        )
    finally:
        repository.close()


async def case_attachment_published(case_dir: Path, evidence: dict) -> None:
    repository = make_repository(case_dir)
    repository.close()
    crashed = run_worker(case_dir, "V2-02")
    check(
        crashed["exit_code"] == CRASH_EXIT
        and crashed["fault_event"]["event"] == "attachment_after_publish",
        "worker_crashed_after_atomic_asset_publish_before_metadata",
        evidence,
    )
    repository = make_repository(case_dir)
    try:
        inventory = repository.attachment_inventory()
        digest = sha256(synthetic_attachment())
        check(
            digest in inventory["unregistered_object_hashes"],
            "recovery_identifies_orphan_asset_by_hash",
            evidence,
        )
        check(
            inventory["automatic_deletions"] == 0,
            "recovery_does_not_delete_confirmed_asset",
            evidence,
        )
        check(
            repository.counts()["messages"] == 0
            and repository.counts()["attachments"] == 0,
            "no_dangling_published_message",
            evidence,
        )
        repository.stage_attachment(
            synthetic_attachment(), "image/png", attachment_id="attach_after_recovery"
        )
        submission = repository.publish_submission(
            "chat_c1",
            "恢复后主动提交。",
            "sub_v2_02",
            attachment_ids=["attach_after_recovery"],
        )
        check(
            repository.resolve_attachment("attach_after_recovery")["content_hash"]
            == digest,
            "same_safe_asset_reused_without_loss",
            evidence,
        )
        evidence.update(
            fault_event=crashed,
            business={"counts": repository.counts(), "submission": submission},
            attachments_before=inventory,
            attachments_after=repository.attachment_inventory(),
            input_sha256=digest,
            actual_dispatches={
                "local_messages": 0,
                "real_model": 0,
                "measurement": "no_execution_component_created",
            },
            usage={"status": "unknown", "fields": {}},
            scope="actual_storage_worker_os_crash_after_atomic_publish",
        )
    finally:
        repository.close()


async def case_committed_pending(case_dir: Path, evidence: dict) -> None:
    repository = make_repository(case_dir)
    repository.close()
    crashed = run_worker(case_dir, "V2-03")
    check(
        crashed["exit_code"] == CRASH_EXIT,
        "business_service_worker_crashed_after_commit",
        evidence,
    )
    marker = crashed["fault_event"]
    check(
        marker["event"] == "after_submission_committed_before_dispatch",
        "exact_pre_dispatch_window_confirmed",
        evidence,
    )
    generation_id = marker["details"]["generation_id"]
    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        await finish_task(service, generation_id)
        generation = repository.get_generation(generation_id)
        check(
            generation["state"] == "completed",
            "known_undispatched_pending_resumed",
            evidence,
        )
        check(
            repository.counts()["messages"] == 2
            and repository.counts()["submissions"] == 1,
            "restart_keeps_exactly_original_business_messages",
            evidence,
        )
        check(
            len(adapter.boundary.records) == 1,
            "only_one_actual_local_provider_request",
            evidence,
        )
        evidence.update(
            fault_event=crashed,
            **await collect_evidence(repository, adapter, generation_id),
        )


class InjectedObservationStop(RuntimeError):
    """Stop a validation observer at an acknowledged event, keeping its Host alive."""


async def case_receipt_lost(case_dir: Path, evidence: dict) -> None:
    import httpx
    from learning_app.service import GenerationService

    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        adapter.boundary.set_scenario(
            {"text_chunks": ["原执行的回答。"], "gate_before_terminal": "finish"}
        )
        original_rpc = adapter.peer.rpc
        receipt = {}

        async def lose_prompt_receipt(method, value):
            result = await original_rpc(method, value)
            if method == "session/prompt":
                receipt.update(
                    event="actual_Host_prompt_accepted_response_discarded",
                    accepted=result.get("accepted"),
                    session_id=value["sessionId"],
                    request_id=value["requestId"],
                    at_ns=time.time_ns(),
                )
                raise httpx.ReadError("synthetic accepted prompt receipt loss")
            return result

        adapter.peer.rpc = lose_prompt_receipt
        submitted = await service.submit(question("sub_v2_04"))
        gid = submitted["generation_id"]
        await finish_task(service, gid)
        adapter.peer.rpc = original_rpc
        check(
            receipt.get("accepted") is True,
            "actual_Host_accepted_before_receipt_loss",
            evidence,
        )
        check(
            repository.get_generation(gid)["state"] == "unknown",
            "lost_receipt_keeps_unknown_without_reprompt",
            evidence,
        )
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "gate_waiting", generation_id=gid
        )
        adapter.boundary.release_event("finish")
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "request_finished", generation_id=gid
        )
        await service.close()
        restarted = GenerationService(
            repository, config_for(case_dir), execution=adapter
        )
        try:
            await restarted.open()
            await finish_task(restarted, gid)
            check(
                repository.get_generation(gid)["state"] == "completed",
                "original_execution_terminal_reconciled",
                evidence,
            )
            check(
                len(adapter.boundary.records) == 1,
                "receipt_loss_never_reprompts",
                evidence,
            )
            before = repository.counts()
            await restarted.reconcile(gid)
            check(
                repository.counts()["usage_records"] == before["usage_records"],
                "repeat_recovery_does_not_duplicate_usage",
                evidence,
            )
            evidence.update(
                fault_event=receipt, **await collect_evidence(repository, adapter, gid)
            )
        finally:
            await restarted.close()


async def case_stream_disconnect(case_dir: Path, evidence: dict) -> None:
    from learning_app.service import GenerationService

    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        committed = asyncio.Event()

        def observed(name, details):
            if name == "event_after_commit" and details.get("kind") == "body":
                committed.set()

        repository.fault_hook = observed
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["保留的第一段。", "恢复后的第二段。"],
                "pause_after_event": "text_delta",
                "gate": "continue",
            }
        )
        submitted = await service.submit(question("sub_v2_05"))
        gid = submitted["generation_id"]
        await asyncio.wait_for(committed.wait(), 15)
        partial = repository.get_generation(gid)["body"]
        check(bool(partial), "actual_increment_persisted_before_disconnect", evidence)
        evidence["fault_event"] = {
            "event": "business_body_commit_then_peer_disconnect",
            "body_sha256": sha256(partial.encode()),
            "at_ns": time.time_ns(),
        }
        await adapter.peer.disconnect()
        await finish_task(service, gid)
        check(
            repository.get_generation(gid)["body"] == partial,
            "disconnect_preserves_partial_body",
            evidence,
        )
        adapter.boundary.release_event("continue")
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "request_finished", generation_id=gid
        )
        await service.close()
        restarted = GenerationService(
            repository, config_for(case_dir), execution=adapter
        )
        try:
            await restarted.open()
            await finish_task(restarted, gid)
            generation = repository.get_generation(gid)
            check(
                generation["state"] == "completed",
                "snapshot_page_recovers_original_terminal",
                evidence,
            )
            check(
                generation["body"] == "保留的第一段。恢复后的第二段。",
                "snapshot_deduplicates_partial_stream",
                evidence,
                body=generation["body"],
            )
            check(
                len(adapter.boundary.records) == 1,
                "observer_reconnect_never_regenerates",
                evidence,
            )
            evidence.update(**await collect_evidence(repository, adapter, gid))
        finally:
            await restarted.close()


async def case_terminal_crash(case_dir: Path, evidence: dict) -> None:
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.service import GenerationService

    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        terminal = {}

        def intercept(name, details):
            if name == "after_terminal_observed_before_ingest":
                terminal.update(details)
                raise InjectedObservationStop(
                    "actual durable Host terminal before business ingest"
                )

        service.faults.hook = intercept
        adapter.boundary.set_scenario({"text_chunks": ["终态已落在真实 Host。"]})
        submitted = await service.submit(question("sub_v2_06"))
        gid = submitted["generation_id"]
        try:
            await finish_task(service, gid)
        except InjectedObservationStop:
            pass
        check(
            terminal.get("kind") == "terminal"
            and terminal.get("native_seq") is not None,
            "real_Host_durable_terminal_observed_before_business_commit",
            evidence,
        )
        binding = service._binding_for(gid)
        write_json(
            case_dir / "terminal-to-ingest.json",
            {
                "config": config_for(case_dir).model_dump(mode="json"),
                "binding": binding.to_dict(),
                "event": terminal,
            },
        )
        await service.close()
        crashed = run_worker(case_dir, "V2-06")
        check(
            crashed["exit_code"] == CRASH_EXIT
            and crashed["fault_event"]["event"]
            == "after_terminal_observed_before_ingest",
            "formal_business_ingest_worker_os_crash_in_exact_terminal_window",
            evidence,
        )
        check(
            repository.get_generation(gid)["state"]
            not in {"completed", "cancelled", "failed", "truncated"},
            "business_terminal_not_committed_by_crashed_worker",
            evidence,
            state=repository.get_generation(gid)["state"],
        )
        original_pid = adapter.host.process.pid
        original_cleanup = await adapter.close()
        replacement = ExecutionAdapter(
            ROOT, case_dir / "execution", mode="fixture", timeout=60
        )
        await replacement.open()
        restarted = GenerationService(
            repository, config_for(case_dir), execution=replacement
        )
        try:
            await restarted.open()
            await finish_task(restarted, gid)
            check(
                repository.get_generation(gid)["state"] == "completed",
                "restart_reconciles_original_native_terminal",
                evidence,
            )
            check(
                replacement.host.process.pid != original_pid,
                "full_fixed_Host_process_restarted_from_persistent_home",
                evidence,
            )
            before = repository.counts()
            await restarted.reconcile(gid)
            await restarted.reconcile(gid)
            after = repository.counts()
            check(
                after["messages"] == before["messages"]
                and after["usage_records"] == before["usage_records"],
                "repeated_terminal_recovery_does_not_duplicate_messages_or_usage",
                evidence,
            )
            check(
                len(replacement.boundary.records) == 1,
                "terminal_crash_never_reprompts",
                evidence,
            )
            evidence.update(
                fault_event=crashed,
                **await collect_evidence(repository, replacement, gid),
                full_Host_restart={
                    "original_pid": original_pid,
                    "replacement_pid": replacement.host.process.pid,
                    "original_cleanup": original_cleanup,
                },
                scope="actual fixed Host durable terminal; formal business-ingest worker os crash; full Host restart from same home before original-execution reconciliation",
            )
        finally:
            await restarted.close()
            evidence["replacement_cleanup"] = await replacement.close()


async def case_usage_missing(case_dir: Path, evidence: dict) -> None:
    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["正文已完整生成，但输出用量未提供。"],
                "usage_start": {"input_tokens": 17},
                "usage_end": None,
            }
        )
        submitted = await service.submit(question("sub_v2_07"))
        gid = submitted["generation_id"]
        await finish_task(service, gid)
        view = service.generation_view(gid)
        check(
            view["state"] == "completed" and bool(view["body"]),
            "body_terminal_independent_of_missing_usage",
            evidence,
        )
        check(
            view["usage"]["output_tokens"] is None
            and view["usage"]["status"] == "unknown",
            "missing_output_usage_stays_unknown_not_zero",
            evidence,
            usage=view["usage"],
        )
        before = len(adapter.boundary.records)
        await service.reconcile(gid)
        check(
            len(adapter.boundary.records) == before == 1,
            "usage_reconciliation_does_not_reanswer",
            evidence,
        )
        evidence.update(
            fault_event={"event": "fixture_omitted_terminal_output_usage"},
            **await collect_evidence(repository, adapter, gid),
        )


def replay_stored_event(repository, event: dict) -> dict:
    payload = event["payload"]
    return repository.ingest_event(
        event["generation_id"],
        binding_id=event["binding_id"],
        epoch=event["epoch"],
        event_id=event["event_id"],
        seq=event["seq"],
        kind=event["kind"],
        text=payload["text"],
        status=payload["status"],
        raw_usage=payload["raw_usage"],
        usage_source=payload["usage_source"],
        usage_fields=payload["usage_fields"],
        settlement_id=payload["settlement_id"],
        data=payload["data"],
    )


async def case_duplicates(case_dir: Path, evidence: dict) -> None:
    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["幂等回答。"],
                "usage_updates": [{"output_tokens": 23}, {"output_tokens": 23}],
                "usage_end": {"output_tokens": 23},
            }
        )
        payload = question("sub_v2_08")
        first, duplicate = await asyncio.gather(
            service.submit(payload), service.submit(dict(payload))
        )
        check(
            first["generation_id"] == duplicate["generation_id"]
            and first["created"] != duplicate["created"],
            "double_submit_creates_one_logical_generation",
            evidence,
        )
        gid = first["generation_id"]
        await finish_task(service, gid)
        saved = repository.evidence(gid)
        counts = repository.counts()
        replays = [replay_stored_event(repository, event) for event in saved["events"]]
        check(
            all(row["duplicate"] for row in replays),
            "all_identical_events_are_idempotent",
            evidence,
        )
        check(
            repository.counts() == counts,
            "event_replay_does_not_add_any_record",
            evidence,
        )
        view = service.generation_view(gid)
        check(
            view["usage"]["output_tokens"] == 23,
            "cumulative_usage_not_added_repeatedly",
            evidence,
        )
        check(
            len(adapter.boundary.records) == 1 and counts["generations"] == 1,
            "double_submit_dispatches_one_actual_local_request",
            evidence,
        )
        evidence.update(
            fault_event={
                "event": "concurrent_same_submission_and_replayed_native_events"
            },
            **await collect_evidence(repository, adapter, gid),
        )


async def case_old_epoch(case_dir: Path, evidence: dict) -> None:
    repository = make_repository(case_dir)
    try:
        old = repository.publish_submission("chat_c1", "旧问题。", "sub_v2_09_old")
        first = repository.bind_execution("chat_c1", "storage-fixture-old")
        repository.claim_dispatch(
            old["generation_id"], first["binding_id"], first["epoch"]
        )
        repository.ingest_event(
            old["generation_id"],
            binding_id=first["binding_id"],
            epoch=first["epoch"],
            event_id="old-terminal",
            seq=1,
            kind="terminal",
            status="completed",
        )
        second = repository.bind_execution("chat_c1", "storage-fixture-new")
        new = repository.publish_submission("chat_c1", "新问题。", "sub_v2_09_new")
        repository.claim_dispatch(
            new["generation_id"], second["binding_id"], second["epoch"]
        )
        before = {
            "old": repository.get_generation(old["generation_id"]),
            "new": repository.get_generation(new["generation_id"]),
        }
        rejected = []
        for kind in ("body", "terminal", "usage"):
            rejected.append(
                repository.ingest_event(
                    old["generation_id"],
                    binding_id=first["binding_id"],
                    epoch=first["epoch"],
                    event_id=f"late-old-{kind}",
                    seq=900,
                    kind=kind,
                    text="迟到污染",
                    status="completed" if kind == "terminal" else None,
                    raw_usage={"output_tokens": 999} if kind == "usage" else None,
                )
            )
        after = {
            "old": repository.get_generation(old["generation_id"]),
            "new": repository.get_generation(new["generation_id"]),
        }
        check(
            all(item["stale"] and not item["accepted"] for item in rejected),
            "all_old_epoch_callbacks_rejected",
            evidence,
        )
        check(
            before == after,
            "old_epoch_cannot_mutate_old_or_new_generation_body_terminal_or_usage",
            evidence,
        )
        check(
            repository.counts()["rejected_events"] == 3,
            "rejected_callbacks_keep_diagnostic_identity",
            evidence,
        )
        evidence.update(
            fault_event={
                "event": "retired_epoch_body_terminal_usage_delivered",
                "old_epoch": first["epoch"],
                "current_epoch": second["epoch"],
            },
            business={
                "old": repository.evidence(old["generation_id"]),
                "new": repository.evidence(new["generation_id"]),
                "counts": repository.counts(),
            },
            actual_dispatches={
                "local_messages": 0,
                "real_model": 0,
                "measurement": "normalized_protocol_fixture_no_execution_component",
            },
            scope="actual storage epoch guard; synthetic normalized events, no Host dispatch claimed",
        )
    finally:
        repository.close()


async def case_course_switch(case_dir: Path, evidence: dict) -> None:
    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        body_committed = asyncio.Event()

        def hook(name, details):
            if name == "event_after_commit" and details.get("kind") == "body":
                body_committed.set()

        repository.fault_hook = hook
        adapter.boundary.set_scenario(
            {"text_chunks": ["课程 A 的活动回答。"], "gate_before_terminal": "continue"}
        )
        first = await service.submit(question("sub_v2_10_a"))
        await asyncio.wait_for(body_committed.wait(), 15)
        new_chat = repository.create_chat("course_b")
        old_chat_id = repository.get_generation(first["generation_id"])["chat_id"]
        check(
            old_chat_id == "chat_c1" and new_chat["chat_id"] != old_chat_id,
            "switching_course_creates_distinct_course_chat",
            evidence,
        )
        check(
            repository.get_chat(new_chat["chat_id"])["messages"] == [],
            "new_course_has_no_old_generation_content",
            evidence,
        )
        adapter.boundary.release_event("continue")
        await finish_task(service, first["generation_id"])
        check(
            repository.get_generation(first["generation_id"])["body"]
            == "课程 A 的活动回答。",
            "background_generation_finishes_in_original_chat",
            evidence,
        )
        before = repository.counts()["chats"]
        repository.get_chat("chat_c1")
        repository.get_chat("chat_c1")
        check(
            repository.counts()["chats"] == before,
            "reopening_existing_chat_does_not_create_chat",
            evidence,
        )
        evidence.update(
            fault_event={"event": "course_B_selected_after_C1_body_committed"},
            **await collect_evidence(repository, adapter, first["generation_id"]),
            scope="actual business course/chat ownership; fullscreen and player UI belong to MVP-A",
        )


async def case_completed_history(case_dir: Path, evidence: dict) -> None:
    from learning_app.storage import StorageError

    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        install_history(repository, case_dir)
        previous_binding = await adapter.prepare(
            "chat_c1", "seed-not-dispatched", "seed-not-dispatched", 1
        )
        old_db_binding = repository.bind_execution(
            "chat_c1", previous_binding.session_id
        )
        repository.merge_completed_history(
            "chat_c1", HISTORY["initial_completed_history"]
        )
        repository.merge_completed_history(
            "chat_c1", HISTORY["external_completed_history"]
        )
        repeated = repository.merge_completed_history(
            "chat_c1", HISTORY["external_completed_history"]
        )
        check(
            repeated == {"inserted": 0, "duplicates": 2},
            "completed_history_deduplicated_by_stable_identity",
            evidence,
        )
        counts = repository.counts()
        check(
            counts["messages"] == 4
            and counts["submissions"] == 0
            and counts["generations"] == 0
            and counts["usage_records"] == 0
            and counts["outbound_attempts"] == 0,
            "history_ingest_has_no_submission_attempt_event_or_usage",
            evidence,
        )
        check(
            len(adapter.boundary.records) == 0,
            "old_questions_actual_dispatch_count_zero",
            evidence,
        )
        changed = {
            **HISTORY["external_completed_history"][0],
            "text": "conflicting replacement",
        }
        conflict = None
        try:
            repository.merge_completed_history("chat_c1", [changed])
        except StorageError as exc:
            conflict = exc.code
        check(
            conflict == "HISTORY_MESSAGE_CONFLICT",
            "same_history_identity_different_content_rejected",
            evidence,
        )
        messages = repository.get_chat("chat_c1")["messages"]
        revisions = {m["revision_id"] for m in messages}
        check(
            {"rev_u1_v1", "rev_u2_v1"}.issubset(revisions),
            "stable_imported_revision_identity_preserved",
            evidence,
        )
        adapter.boundary.set_scenario(
            {"text_chunks": ["已基于 a=2 和允许的确认图继续。"]}
        )
        next_question = HISTORY["next_question"]
        submitted = await service.submit(
            question(
                next_question["submission_id"],
                text=next_question["text"],
                attachments=next_question["required_attachment_ids"],
            )
        )
        gid = submitted["generation_id"]
        await finish_task(service, gid)
        record = adapter.boundary.records[0]
        request = json.loads(
            (
                adapter.boundary.private / f"{record['request_id']}.request.json"
            ).read_text("utf-8")
        )
        serialized = json.dumps(request, ensure_ascii=False)
        check(
            "a=2" in serialized and "a=2 replaces a=1" in serialized,
            "actual_provider_request_keeps_latest_correction",
            evidence,
        )
        check(
            len(record["images"]) == 1
            and record["images"][0]["sha256"]
            == HISTORY["confirmed_attachment"]["sha256"],
            "actual_provider_request_contains_exactly_confirmed_history_image_hash",
            evidence,
        )
        generation = repository.get_generation(gid)
        check(
            {"rev_u1_v1", "rev_u2_v1"}.issubset(
                set(generation["context_snapshot"]["protected_revision_ids"])
            ),
            "actual_generation_snapshot_protects_original_and_correction_revisions",
            evidence,
        )
        check(
            repository.get_generation(gid)["chat_id"] == "chat_c1",
            "history_rebuild_preserves_UI_chat_identity",
            evidence,
        )
        new_binding = repository.current_binding("chat_c1")
        check(
            new_binding["epoch"] == old_db_binding["epoch"] + 1,
            "history_merge_rebuilds_stale_Host_binding",
            evidence,
        )
        check(len(adapter.boundary.records) == 1, "only_new_U3_is_dispatched", evidence)
        evidence.update(
            fault_event={"event": "completed_history_merge_twice_then_explicit_U3"},
            context_request_sha256=record["body_sha256"],
            **await collect_evidence(repository, adapter, gid),
        )


async def case_cancel_interleave(case_dir: Path, evidence: dict) -> None:
    from learning_app.storage import StorageError

    async with formal_host(case_dir, evidence) as (repository, adapter, service):
        active = asyncio.Event()

        def hook(name, details):
            if name == "event_after_commit" and details.get("kind") in {
                "body",
                "reasoning",
            }:
                active.set()

        repository.fault_hook = hook
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["取消前保留的正文。", "不应全部执行的后续。"],
                "pause_after_event": "text_delta",
                "gate": "continue",
            }
        )
        submitted = await service.submit(question("sub_v2_14_cancel"))
        gid = submitted["generation_id"]
        await asyncio.wait_for(active.wait(), 15)
        before_cancel = repository.get_generation(gid)
        check(
            bool(before_cancel["body"]) and before_cancel["state"] == "running",
            "cancel_happens_during_actual_body_generation",
            evidence,
        )
        await service.cancel(gid)
        await finish_task(service, gid)
        cancelled = repository.get_generation(gid)
        check(
            cancelled["state"] == "cancelled"
            and cancelled["body"] == before_cancel["body"],
            "single_session_cancel_preserves_partial_and_durable_terminal",
            evidence,
        )
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "request_finished", generation_id=gid
        )
        check(
            adapter.boundary.records[0].get("cancel_observed") is True,
            "provider_boundary_observes_cancelled_IO",
            evidence,
        )
        await adapter.peer.disconnect()
        await service.reconcile(gid)
        old_events = repository.evidence(gid)["events"]
        old_terminal = next(
            event for event in old_events if event["kind"] == "terminal"
        )
        adapter.boundary.set_scenario({"text_chunks": ["停止后同会话的新回答。"]})
        second = await service.submit(
            question("sub_v2_14_next", text="沿此前内容继续。")
        )
        await finish_task(service, second["generation_id"])
        second_before = repository.get_generation(second["generation_id"])
        replay_stored_event(repository, old_terminal)
        conflicting_late_completion = None
        try:
            # An adversarial normalized callback, explicitly not a fabricated
            # Host fact: the actual native terminal above remains cancelled.
            repository.ingest_event(
                gid,
                binding_id=old_terminal["binding_id"],
                epoch=old_terminal["epoch"],
                event_id="fixture-conflicting-late-completion",
                seq=9999,
                kind="terminal",
                status="completed",
            )
        except StorageError as exc:
            conflicting_late_completion = exc.code
        check(
            conflicting_late_completion == "TERMINAL_STATUS_CONFLICT"
            and repository.get_generation(gid)["state"] == "cancelled",
            "synthetic_late_completion_cannot_override_durable_cancelled_terminal",
            evidence,
            error_code=conflicting_late_completion,
            evidence_scope="normalized callback guard; not an actual native completion event",
        )
        check(
            repository.get_generation(second["generation_id"]) == second_before,
            "late_old_terminal_cannot_rewrite_followup",
            evidence,
        )
        check(
            second_before["state"] == "completed"
            and second_before["body"] == "停止后同会话的新回答。",
            "same_Host_same_chat_followup_works_after_cancel_and_disconnect",
            evidence,
        )
        check(
            repository.get_generation(gid)["adapter_metadata"]["session_id"]
            == second_before["adapter_metadata"]["session_id"],
            "followup_uses_same_native_session",
            evidence,
        )
        check(
            len(adapter.boundary.records) == 2,
            "exactly_cancelled_and_followup_local_dispatches",
            evidence,
        )
        evidence.update(
            fault_event={
                "event": "body_committed_then_single_session_cancel_then_peer_disconnect_then_late_terminal"
            },
            cancelled=await collect_evidence(repository, adapter, gid),
            followup=await collect_evidence(
                repository, adapter, second["generation_id"]
            ),
        )


async def case_source_version(case_dir: Path, evidence: dict) -> None:
    from learning_app.storage import StorageError

    repository = make_repository(case_dir)
    try:
        install_history(repository, case_dir)
        locator = {
            "asset_id": "source_math_1",
            "source_version": "SOURCE-v1",
            "page": 1,
        }
        submission = repository.publish_submission(
            "chat_c1",
            "只根据原 v1 与已确认裁剪回答。",
            "sub_v2_11",
            attachment_ids=["attach_crop_a1"],
            source_locators=[locator],
        )
        source_path = case_dir / HISTORY["source"]["display_name"]
        source_path.write_text(
            HISTORY["source"]["versions"][1]["fixture_content_utf8"], "utf-8"
        )
        repository.add_source_version(
            "source_math_1", source_path, source_version="SOURCE-v2"
        )
        mismatch = repository.resolve_source("source_math_1", "SOURCE-v1")
        check(
            mismatch["status"] == "version_mismatch",
            "overwritten_v1_is_explicit_mismatch",
            evidence,
        )
        check(
            repository.resolve_source("source_math_1", "SOURCE-v2")["status"]
            == "available",
            "v2_registered_as_separate_source_version",
            evidence,
        )
        source_path.unlink()
        check(
            repository.resolve_source("source_math_1", "SOURCE-v1")["status"]
            == "missing",
            "removed_v1_is_explicit_missing",
            evidence,
        )
        relocated = case_dir / "relocated-v1.txt"
        relocated.write_text(
            HISTORY["source"]["versions"][0]["fixture_content_utf8"], "utf-8"
        )
        wrong = case_dir / "wrong-version.txt"
        wrong.write_text("this is not v1", "utf-8")
        rejected = False
        try:
            repository.relocate_source("source_math_1", "SOURCE-v1", wrong)
        except StorageError:
            rejected = True
        check(rejected, "wrong_hash_relocation_rejected", evidence)
        repository.relocate_source("source_math_1", "SOURCE-v1", relocated)
        resolved = repository.resolve_source("source_math_1", "SOURCE-v1")
        check(
            resolved["status"] == "available"
            and resolved["asset_id"] == "source_math_1"
            and resolved["source_version"] == "SOURCE-v1",
            "legal_relocation_preserves_identity",
            evidence,
        )
        message = next(
            m
            for m in repository.get_chat("chat_c1")["messages"]
            if m["message_id"] == submission["user_message_id"]
        )
        check(
            message["source_locators"] == [locator],
            "old_message_locator_not_retargeted",
            evidence,
        )
        attachment = repository.resolve_attachment("attach_crop_a1")
        check(
            sha256(Path(attachment["path"]).read_bytes())
            == HISTORY["confirmed_attachment"]["sha256"],
            "confirmed_attachment_intact",
            evidence,
        )
        evidence.update(
            fault_event={
                "event": "source_overwritten_then_removed",
                "deterministic": True,
            },
            business={"submission": submission, "counts": repository.counts()},
            source_hashes={
                v["source_version"]: v["sha256"] for v in HISTORY["source"]["versions"]
            },
            actual_dispatches={
                "local_messages": 0,
                "real_model": 0,
                "measurement": "no_execution_component_created",
            },
            usage={"status": "unknown", "provider_fields_present": False},
            scope="actual_business_storage_and_context; no Host execution claimed",
        )
    finally:
        repository.close()


async def case_attachment_context(case_dir: Path, evidence: dict) -> None:
    from learning_app.storage import StorageError

    repository = make_repository(case_dir)
    try:
        install_history(repository, case_dir)
        submission = repository.publish_submission(
            "chat_c1",
            "已提交的旧裁剪。",
            "sub_v2_13_old",
            attachment_ids=["attach_crop_a1"],
        )
        # This case isolates attachment/context rules. The synthetic normalized
        # terminal is explicitly not a provider or Host event and records no usage.
        fixture_binding = repository.bind_execution("chat_c1", "storage-only-v2-13")
        repository.claim_dispatch(
            submission["generation_id"],
            fixture_binding["binding_id"],
            fixture_binding["epoch"],
            request_id="storage-only-request",
        )
        repository.ingest_event(
            submission["generation_id"],
            binding_id=fixture_binding["binding_id"],
            epoch=fixture_binding["epoch"],
            event_id="storage-only-terminal",
            seq=1,
            kind="terminal",
            status="completed",
        )
        original = repository.resolve_attachment("attach_crop_a1")
        first_bytes = Path(original["path"]).read_bytes()
        replacement = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
        )
        repository.stage_attachment(
            replacement, "image/png", confirmed=True, attachment_id="attach_crop_a2"
        )
        old_message = next(
            m
            for m in repository.get_chat("chat_c1")["messages"]
            if m["message_id"] == submission["user_message_id"]
        )
        check(
            old_message["attachment_ids"] == ["attach_crop_a1"],
            "new_crop_does_not_replace_old_reference",
            evidence,
        )
        old_snapshot = repository.get_generation(submission["generation_id"])[
            "context_snapshot"
        ]
        check(
            old_snapshot["required_attachment_ids"] == ["attach_crop_a1"],
            "new_crop_does_not_modify_published_generation_snapshot",
            evidence,
        )
        check(
            Path(original["path"]).read_bytes() == first_bytes,
            "new_crop_does_not_mutate_old_bytes",
            evidence,
        )
        Path(original["path"]).unlink()
        counts_before = repository.counts()
        missing_code = None
        try:
            repository.publish_submission(
                "chat_c1",
                "继续看这张图。",
                "sub_v2_13_missing",
                attachment_ids=["attach_crop_a1"],
            )
        except StorageError as exc:
            missing_code = exc.code
        check(
            missing_code == "ATTACHMENT_MISSING",
            "required_attachment_missing_blocks_publish",
            evidence,
            observed_code=missing_code,
        )
        check(
            repository.counts() == counts_before,
            "failed_submission_is_atomic",
            evidence,
        )
        independent = repository.publish_submission(
            "chat_other", "不依赖该图的新问题。", "sub_v2_13_other"
        )
        check(independent["created"], "independent_question_unaffected", evidence)
        evidence.update(
            fault_event={
                "event": "confirmed_attachment_removed",
                "attachment_id": "attach_crop_a1",
            },
            business={
                "counts_before": counts_before,
                "counts_after": repository.counts(),
                "old_submission": submission,
            },
            attachment_hashes={
                "old_confirmed": sha256(first_bytes),
                "new_draft_crop": sha256(replacement),
            },
            actual_dispatches={
                "local_messages": 0,
                "real_model": 0,
                "measurement": "no_execution_component_created",
            },
            usage={"status": "unknown", "provider_fields_present": False},
            scope="actual_business_storage_and_context; no image acquisition capability exists",
        )
    finally:
        repository.close()


CASES: dict[str, Callable] = {
    "V2-01": case_attachment_write,
    "V2-02": case_attachment_published,
    "V2-03": case_committed_pending,
    "V2-04": case_receipt_lost,
    "V2-05": case_stream_disconnect,
    "V2-06": case_terminal_crash,
    "V2-07": case_usage_missing,
    "V2-08": case_duplicates,
    "V2-09": case_old_epoch,
    "V2-10": case_course_switch,
    "V2-11": case_source_version,
    "V2-12": case_completed_history,
    "V2-13": case_attachment_context,
    "V2-14": case_cancel_interleave,
}


def source_hashes() -> dict:
    paths = [
        Path(__file__).resolve(),
        *sorted((ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((ROOT / "harness").rglob("*.mjs")),
        *sorted((ROOT / "harness").rglob("*.json")),
        ROOT / "backend/uv.lock",
        ROOT / "contracts/history-example.json",
        ROOT / "tests/fixtures/v2/cases.json",
        ROOT / "tests/contracts/v2-evidence-contract.json",
        ROOT / "scripts/test_browser.py",
        ROOT / "backend/tests/test_v2_http_cancel.py",
        ROOT / "pnpm-lock.yaml",
        *sorted((ROOT / "frontend/src").rglob("*.ts")),
        *sorted((ROOT / "frontend/src").rglob("*.tsx")),
        *sorted((ROOT / "frontend/src").rglob("*.css")),
    ]
    return {
        path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()) for path in paths
    }


def normalize_case_summary(row: dict) -> None:
    boundaries = []
    if row.get("boundary"):
        boundaries.append(row["boundary"])
    for name in ("cancelled", "followup"):
        if row.get(name, {}).get("boundary"):
            boundaries.append(row[name]["boundary"])
    records = {
        record["request_id"]: record
        for boundary in boundaries
        for record in boundary["records"]
    }
    if boundaries:
        row["actual_dispatches"] = {
            "local_messages": len(records),
            "real_model": max(
                boundary["actual_outbound_attempts"] for boundary in boundaries
            ),
            "measurement": "formal_MessagesBoundary_persistent_request_records_and_outbound_ledger",
        }
    row["input_fixture_hashes"] = {
        "confirmed_synthetic_image": HISTORY["confirmed_attachment"]["sha256"],
        "history_contract": sha256(
            (ROOT / "contracts/history-example.json").read_bytes()
        ),
    }


def ingest_security_evidence(path: Path, row: dict) -> None:
    value = json.loads(path.read_text("utf-8"))
    if value.get("id") != "V2-15" or value.get("status") != "passed":
        raise ValueError("V2_SECURITY_EVIDENCE_NOT_PASSED")
    if not value.get("assertions") or not all(
        check.get("passed") is True for check in value["assertions"]
    ):
        raise ValueError("V2_SECURITY_ASSERTIONS_INCOMPLETE")
    row.update(value)
    row["security_evidence_sha256"] = sha256(path.read_bytes())
    row["security_evidence_file"] = path.resolve().relative_to(ROOT).as_posix()


async def run(args) -> int:
    run_dir = (ROOT / ".runtime/validation" / args.run_id).resolve()
    if not run_dir.is_relative_to((ROOT / ".runtime/validation").resolve()):
        raise ValueError("RUN_DIRECTORY_OUTSIDE_VALIDATION_ROOT")
    run_dir.mkdir(parents=True, exist_ok=False)
    selected = (
        set(args.cases.split(","))
        if args.cases
        else {case["id"] for case in MATRIX["cases"]}
    )
    if not selected.issubset({case["id"] for case in MATRIX["cases"]}):
        raise ValueError("UNKNOWN_VALIDATION_CASE_ID")
    before_hashes = source_hashes()
    result = {
        "schema_version": "learning-v2-result/1",
        "run_id": args.run_id,
        "validation": "P0 / V2",
        "started_at_ns": time.time_ns(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "authorization": "user 2026-10-21 formal P0-01..04 and isolated V2-01..15",
        "real_model_calls": 0,
        "fixture_only": True,
        "cases": [],
        "source_hashes": before_hashes,
    }
    for definition in MATRIX["cases"]:
        case_id = definition["id"]
        row = {
            **definition,
            "status": "not_run",
            "assertions": [],
            "duration_seconds": None,
        }
        result["cases"].append(row)
        if case_id not in selected:
            row["reason"] = "not_selected"
            continue
        if case_id == "V2-15" and args.security_evidence:
            try:
                ingest_security_evidence(args.security_evidence.resolve(), row)
            except (ValueError, KeyError, OSError) as exc:
                row.update(status="failed", error=str(exc))
            normalize_case_summary(row)
            continue
        if case_id not in CASES:
            row["reason"] = "case_integration_not_implemented"
            continue
        case_dir = run_dir / "cases" / case_id
        case_dir.mkdir(parents=True)
        start = time.monotonic()
        try:
            await CASES[case_id](case_dir, row)
            row["status"] = "passed"
        except Exception as exc:
            row.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            (case_dir / "failure.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        row["duration_seconds"] = round(time.monotonic() - start, 4)
        normalize_case_summary(row)
        write_json(case_dir / "case-result.json", row)
        print(f"{case_id}: {row['status']}", flush=True)
    result["source_changed_during_run"] = before_hashes != source_hashes()
    result["finished_at_ns"] = time.time_ns()
    result["actual_dispatches"] = {
        "local_messages": sum(
            row.get("actual_dispatches", {}).get("local_messages", 0)
            for row in result["cases"]
        ),
        "real_model": sum(
            row.get("actual_dispatches", {}).get("real_model", 0)
            for row in result["cases"]
        ),
        "scope": "15-case matrix; independently executed adjacent regressions are listed separately",
    }
    result["real_model_calls"] = result["actual_dispatches"]["real_model"]
    if args.backlog_evidence:
        path = args.backlog_evidence.resolve()
        backlog = json.loads(path.read_text("utf-8"))
        if (
            backlog.get("status") != "passed"
            or backlog.get("source_changed_during_run")
            or backlog.get("service_sha256")
            != before_hashes["backend/src/learning_app/service.py"]
        ):
            raise ValueError("BACKLOG_REGRESSION_EVIDENCE_NOT_CURRENT_PASSED_SOURCE")
        result["adjacent_regressions"] = [
            {
                **{
                    key: backlog[key]
                    for key in (
                        "id",
                        "status",
                        "scope",
                        "service_sha256",
                        "reasoning_chunks",
                        "generation_id",
                        "HTTP_status",
                        "HTTP_cancel_seconds",
                        "cancel_handler",
                        "reasoning_characters",
                        "full_reasoning_characters",
                        "state",
                        "cleanup",
                        "HTTP_port_closed",
                        "actual_outbound_attempts",
                    )
                },
                "local_messages": len(backlog["boundary_records"]),
                "evidence_file": path.relative_to(ROOT).as_posix(),
                "evidence_sha256": sha256(path.read_bytes()),
            }
        ]
    result["status"] = (
        "passed"
        if all(r["status"] == "passed" for r in result["cases"])
        and not result["source_changed_during_run"]
        else "incomplete"
    )
    write_json(run_dir / "run-record.json", result)
    if args.export:
        write_json(ROOT / "research/results/V2-2026-10-21.json", result)
    return (
        0
        if all(r["status"] == "passed" for r in result["cases"] if r["id"] in selected)
        and not result["source_changed_during_run"]
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=f"v2-{time.time_ns()}")
    parser.add_argument(
        "--cases", help="Comma-separated V2 IDs; unselected remain not_run"
    )
    parser.add_argument(
        "--export", action="store_true", help="Write reviewed synthetic result location"
    )
    parser.add_argument(
        "--security-evidence",
        type=Path,
        help="Actual HTTP/WS/browser V2-15 result from owned service",
    )
    parser.add_argument(
        "--backlog-evidence",
        type=Path,
        help="Passed real HTTP reasoning-backlog adjacent regression result",
    )
    parser.add_argument("--worker")
    parser.add_argument("--case-dir", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.case_dir.resolve())
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
