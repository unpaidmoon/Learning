"""Real SQLite/filesystem invariants; Host fault recovery is tested separately."""

from __future__ import annotations

import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from learning_app.storage import Repository, StorageError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


class InjectedFailure(RuntimeError):
    pass


def stop_at(point):
    def hook(name, _detail):
        if name == point:
            raise InjectedFailure(point)

    return hook


def fixture_repo(tmp_path: Path) -> Repository:
    repo = Repository(tmp_path / "business")
    repo.create_course("数学", "course_a")
    repo.create_chat("course_a", "chat_a")
    return repo


def prepared(repo: Repository, submission_id: str = "sub_1", session_id: str = "session_1"):
    submission = repo.publish_submission("chat_a", "已确认的问题", submission_id)
    binding = repo.bind_execution("chat_a", session_id)
    assert repo.claim_dispatch(submission["generation_id"], binding["binding_id"], binding["epoch"])
    return submission["generation_id"], binding


def ingest(repo, gid, binding, event_id, seq, kind, **kwargs):
    return repo.ingest_event(
        gid,
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id=event_id,
        seq=seq,
        kind=kind,
        **kwargs,
    )


def test_runtime_reports_real_sqlite_and_conservative_journal(tmp_path):
    repo = fixture_repo(tmp_path)
    info = repo.runtime_info()
    assert info["sqlite_version"] == sqlite3.sqlite_version
    assert info["journal_mode"] == "delete"
    assert info["synchronous"] == 2
    assert info["foreign_keys"] == 1
    assert info["schema_revision"] == "0001_business"


def test_failed_attachment_write_never_publishes_message_or_model_work(tmp_path):
    repo = fixture_repo(tmp_path)
    repo.fault_hook = stop_at("attachment_before_write")
    with pytest.raises(InjectedFailure):
        repo.stage_attachment(PNG)
    assert repo.counts()["attachments"] == 0
    assert repo.counts()["messages"] == 0
    assert repo.counts()["submissions"] == 0
    assert repo.counts()["outbound_attempts"] == 0


def test_published_file_before_metadata_is_retained_and_identified(tmp_path):
    repo = fixture_repo(tmp_path)
    repo.fault_hook = stop_at("attachment_after_publish")
    with pytest.raises(InjectedFailure):
        repo.stage_attachment(PNG)
    reopened = Repository(repo.root)
    assert reopened.counts()["attachments"] == 0
    assert reopened.counts()["messages"] == 0
    inventory = reopened.attachment_inventory()
    assert len(inventory["unregistered_object_hashes"]) == 1
    assert inventory["automatic_deletions"] == 0
    assert len(list(reopened.objects.glob("*/*"))) == 1


def test_submission_rollback_retains_confirmed_staged_attachment(tmp_path):
    repo = fixture_repo(tmp_path)
    attachment = repo.stage_attachment(PNG)
    repo.fault_hook = stop_at("submission_before_commit")
    with pytest.raises(InjectedFailure):
        repo.publish_submission(
            "chat_a", "题目", "sub_rollback", attachment_ids=[attachment["attachment_id"]]
        )
    reopened = Repository(repo.root)
    assert reopened.counts()["messages"] == 0
    assert reopened.counts()["submissions"] == 0
    assert reopened.counts()["generations"] == 0
    assert reopened.resolve_attachment(attachment["attachment_id"])["path"].read_bytes() == PNG
    assert reopened.attachment_inventory()["unreferenced_attachment_ids"] == [
        attachment["attachment_id"]
    ]


def test_committed_submission_survives_lost_return_and_reuses_identity(tmp_path):
    repo = fixture_repo(tmp_path)
    repo.fault_hook = stop_at("submission_after_commit")
    with pytest.raises(InjectedFailure):
        repo.publish_submission("chat_a", "题目", "sub_committed")
    reopened = Repository(repo.root)
    first = reopened.get_submission("sub_committed")
    repeated = reopened.publish_submission("chat_a", "题目", "sub_committed")
    assert first["generation_id"] == repeated["generation_id"]
    assert repeated["created"] is False
    assert reopened.counts()["messages"] == 2
    assert reopened.counts()["generations"] == 1
    with pytest.raises(StorageError, match="SUBMISSION_CONFLICT"):
        reopened.publish_submission("chat_a", "悄悄改变的问题", "sub_committed")


def test_claim_is_durable_before_io_and_cannot_be_reclaimed_after_unknown(tmp_path):
    repo = fixture_repo(tmp_path)
    submission = repo.publish_submission("chat_a", "题目", "sub_claim")
    gid = submission["generation_id"]
    binding = repo.bind_execution("chat_a", "session_claim")
    repo.fault_hook = stop_at("dispatch_claimed")
    with pytest.raises(InjectedFailure):
        repo.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    reopened = Repository(repo.root)
    assert reopened.get_generation(gid)["state"] == "dispatching"
    assert not reopened.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    reopened.mark_unknown(gid, "RECEIPT_LOST")
    assert not reopened.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    assert reopened.get_generation(gid)["outbound_attempt_count"] == 0
    assert reopened.recoverable_generations()[0]["state"] == "unknown"
    with pytest.raises(StorageError, match="CHAT_EXECUTION_ACTIVE"):
        reopened.publish_submission("chat_a", "另一个问题", "sub_no_bypass")


def test_pending_cancel_fences_a_prepared_dispatch_without_an_attempt(tmp_path):
    repo = fixture_repo(tmp_path)
    gid = repo.publish_submission("chat_a", "尚未出站", "sub_cancel_pending")["generation_id"]
    binding = repo.bind_execution("chat_a", "session_cancel_pending")
    repo.request_cancel(gid)
    assert not repo.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    generation = repo.get_generation(gid)
    assert generation["state"] == "cancelled"
    assert generation["outbound_attempt_count"] == 0
    assert repo.get_chat("chat_a")["messages"][-1]["status"] == "cancelled"
    assert repo.get_submission("sub_cancel_pending")["state"] == "cancelled"
    assert repo.recoverable_generations() == []


def test_known_pre_dispatch_failure_is_failed_and_claimed_failure_stays_unknown(tmp_path):
    repo = fixture_repo(tmp_path)
    gid = repo.publish_submission("chat_a", "尚未出站", "sub_known_failed")["generation_id"]
    assert repo.fail_pending(gid, "ATTACHMENT_MISSING")
    assert repo.get_generation(gid)["state"] == "failed"
    assert repo.get_generation(gid)["unknown_reason"] == "ATTACHMENT_MISSING"
    second, _binding = prepared(repo, "sub_uncertain")
    assert not repo.fail_pending(second, "DO_NOT_RESET_AFTER_CLAIM")
    repo.mark_unknown(second, "DISPATCH_UNCERTAIN")
    assert repo.get_generation(second)["state"] == "unknown"


def test_fixture_attempts_are_distinguished_from_actual_outbound(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, _binding = prepared(repo)
    assert repo.record_outbound_attempt(gid, "fixture_attempt", actual_outbound=False)
    assert repo.record_outbound_attempt(gid, "real_attempt", actual_outbound=True)
    assert not repo.record_outbound_attempt(gid, "real_attempt", actual_outbound=True)
    generation = repo.get_generation(gid)
    assert generation["outbound_attempt_count"] == 2
    assert generation["real_outbound_attempt_count"] == 1


def test_received_attempt_upgrades_at_real_outbound_edge_without_recount(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, _binding = prepared(repo)
    repo.record_outbound_attempt(gid, "edge_race", actual_outbound=False)
    assert repo.get_generation(gid)["real_outbound_attempt_count"] == 0
    assert not repo.record_outbound_attempt(gid, "edge_race", actual_outbound=True)
    assert not repo.record_outbound_attempt(gid, "edge_race", actual_outbound=False)
    generation = repo.get_generation(gid)
    assert generation["outbound_attempt_count"] == 1
    assert generation["real_outbound_attempt_count"] == 1


def test_two_repositories_atomically_claim_only_once(tmp_path):
    repo = fixture_repo(tmp_path)
    gid = repo.publish_submission("chat_a", "题目", "sub_race")["generation_id"]
    binding = repo.bind_execution("chat_a", "session_race")
    other = Repository(repo.root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(item.claim_dispatch, gid, binding["binding_id"], binding["epoch"])
            for item in (repo, other)
        ]
        results = [future.result() for future in futures]
    assert sorted(results) == [False, True]


def test_partial_stream_is_saved_and_duplicate_event_does_not_append(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    ingest(repo, gid, binding, "body_1", 1, "body", text="已保存部分", data={"native_seq": 10})
    reopened = Repository(repo.root)
    repeated = ingest(
        reopened, gid, binding, "body_1", 99, "body", text="已保存部分", data={"native_seq": 10}
    )
    assert repeated["duplicate"] is True
    assert reopened.get_generation(gid)["body"] == "已保存部分"
    assert reopened.get_binding(binding["binding_id"])["last_event_watermark"] == 10
    ingest(
        reopened,
        gid,
        binding,
        "snapshot_1",
        2,
        "snapshot",
        text="已保存部分以及恢复后半段",
        data={"native_seq": 12, "reasoning": "原思考"},
    )
    assert reopened.get_generation(gid)["body"] == "已保存部分以及恢复后半段"
    assert reopened.get_generation(gid)["reasoning"] == "原思考"


def test_terminal_transaction_failure_replays_once_without_usage_duplication(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    ingest(repo, gid, binding, "body_before_terminal", 1, "body", text="完整答案")
    repo.fault_hook = stop_at("terminal_before_commit")
    with pytest.raises(InjectedFailure):
        ingest(repo, gid, binding, "terminal_1", 2, "terminal", status="completed")
    reopened = Repository(repo.root)
    assert reopened.get_generation(gid)["state"] == "running"
    ingest(reopened, gid, binding, "terminal_1", 2, "terminal", status="completed")
    ingest(reopened, gid, binding, "terminal_1", 2, "terminal", status="completed")
    generation = reopened.get_generation(gid)
    assert generation["state"] == "completed"
    assert generation["body"] == "完整答案"
    assert generation["usage"] == []
    assert reopened.counts()["execution_events"] == 2
    assert reopened.counts()["messages"] == 2


def test_authoritative_snapshot_can_complete_saved_terminal_text_without_reexecution(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    ingest(repo, gid, binding, "before_dropped_final_chunk", 1, "body", text="前半段")
    ingest(
        repo,
        gid,
        binding,
        "known_terminal",
        2,
        "terminal",
        status="completed",
        data={"native_seq": 20},
    )
    ingest(
        repo,
        gid,
        binding,
        "durable_full_snapshot",
        3,
        "snapshot",
        text="前半段和后半段",
        data={"native_seq": 21, "reasoning": "保存的思考"},
    )
    generation = repo.get_generation(gid)
    assert generation["state"] == "completed"
    assert generation["body"] == "前半段和后半段"
    assert generation["reasoning"] == "保存的思考"
    assert generation["outbound_attempt_count"] == 0
    assert repo.counts()["messages"] == 2


def test_terminal_with_binding_and_missing_usage_is_reconciled_without_new_dispatch(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    ingest(repo, gid, binding, "known_terminal_no_usage", 1, "terminal", status="completed")
    reopened = Repository(repo.root)
    pending_reconciliation = reopened.recoverable_generations()
    assert [item["generation_id"] for item in pending_reconciliation] == [gid]
    assert pending_reconciliation[0]["state"] == "completed"
    assert not reopened.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
    ingest(
        reopened,
        gid,
        binding,
        "usage_remains_unknown",
        2,
        "usage",
        raw_usage=None,
        usage_fields={"input_tokens": False, "output_tokens": False},
    )
    assert reopened.recoverable_generations() == []
    assert reopened.get_generation(gid)["state"] == "completed"
    assert reopened.get_generation(gid)["usage"][0]["normalized"]["output_tokens"] is None


def test_same_session_continues_with_independent_generation_metadata_and_seq(tmp_path):
    repo = fixture_repo(tmp_path)
    first, binding = prepared(repo)
    first_metadata = {
        "generation_id": first,
        "session_id": "session_1",
        "epoch": 1,
        "request_id": "request_1",
    }
    repo.save_adapter_metadata(first, first_metadata)
    ingest(repo, first, binding, "first_terminal", 1, "terminal", status="completed")
    second = repo.publish_submission("chat_a", "继续", "sub_2")["generation_id"]
    reused = repo.bind_execution("chat_a", "session_1")
    assert reused["epoch"] == binding["epoch"]
    second_metadata = {
        "generation_id": second,
        "session_id": "session_1",
        "epoch": 1,
        "request_id": "request_2",
    }
    assert repo.claim_dispatch(
        second, reused["binding_id"], reused["epoch"], adapter_metadata=second_metadata
    )
    ingest(repo, second, reused, "second_body", 1, "body", text="新回答")
    assert repo.get_generation(first)["adapter_metadata"] == first_metadata
    assert repo.get_generation(second)["adapter_metadata"] == second_metadata
    assert repo.get_generation(second)["body"] == "新回答"
    second_user = repo.get_generation(second)["user_message_id"]
    first_assistant = repo.get_generation(first)["assistant_message_id"]
    assert next(
        message
        for message in repo.get_chat("chat_a")["messages"]
        if message["message_id"] == second_user
    )["parent_ids"] == [first_assistant]
    stale = ingest(repo, first, binding, "first_late_body", 2, "body", text="迟到污染")
    assert stale["stale"] is True
    assert repo.get_generation(second)["body"] == "新回答"


def test_old_epoch_cannot_change_new_binding_or_old_message(tmp_path):
    repo = fixture_repo(tmp_path)
    first, binding = prepared(repo)
    ingest(repo, first, binding, "old_terminal", 1, "terminal", status="completed")
    new_binding = repo.bind_execution("chat_a", "new_session")
    assert new_binding["epoch"] == 2
    old_message = repo.get_generation(first)["body"]
    rejected = ingest(repo, first, binding, "late_old", 2, "body", text="不应出现")
    assert rejected["stale"] is True
    assert repo.get_generation(first)["body"] == old_message
    assert repo.get_binding(new_binding["binding_id"])["last_event_watermark"] == 0
    assert repo.counts()["rejected_events"] == 1


def test_cancel_is_intent_and_late_known_completion_decides_the_terminal(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    ingest(repo, gid, binding, "reasoning_1", 1, "reasoning", text="正在思考")
    repo.request_cancel(gid)
    assert repo.get_generation(gid)["state"] == "running"
    assert repo.get_generation(gid)["cancel_requested"] is True
    ingest(repo, gid, binding, "completed_race", 2, "terminal", status="completed")
    assert repo.get_generation(gid)["state"] == "completed"
    assert repo.get_generation(gid)["reasoning"] == "正在思考"
    next_generation = repo.publish_submission("chat_a", "后续追问", "sub_next")
    assert next_generation["state"] == "pending"


def test_usage_preserves_absence_and_settles_without_stream_double_count(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    raw = {
        "source": "provider_sse_before_harness_projection",
        "raw": [],
        "settled": False,
        "normalized": {"input_tokens": 0, "output_tokens": 0},
        "field_presence": {"input_tokens": False, "output_tokens": False},
    }
    ingest(
        repo,
        gid,
        binding,
        "unsettled_usage",
        1,
        "usage",
        raw_usage=raw,
        usage_source="provider_boundary",
        usage_fields=raw["field_presence"],
    )
    unknown = repo.get_generation(gid)["usage"][0]
    assert unknown["raw_usage"] == raw
    assert unknown["normalized"]["input_tokens"] is None
    assert unknown["normalized"]["output_tokens"] is None
    final = {
        "raw": [{"event": "message_delta", "usage": {"output_tokens": 4}}],
        "settled": True,
        "normalized": {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 3},
        "field_presence": {"input_tokens": True, "output_tokens": True},
    }
    for index in (2, 3):
        ingest(
            repo,
            gid,
            binding,
            f"settled_usage_{index}",
            index,
            "usage",
            raw_usage=final,
            usage_source="provider_boundary",
            usage_fields=final["field_presence"],
            settlement_id="attempt_1_final",
        )
    records = repo.get_generation(gid)["usage"]
    assert len(records) == 2
    settled = next(record for record in records if record["settlement_id"] == "attempt_1_final")
    assert settled["normalized"]["input_tokens"] == 12
    assert settled["normalized"]["output_tokens"] == 4
    assert settled["normalized"]["cached_input_tokens"] == 3
    assert settled["normalized"]["reasoning_tokens"] is None


def test_usage_observations_upgrade_once_and_never_downgrade_final_settlement(tmp_path):
    repo = fixture_repo(tmp_path)
    gid, binding = prepared(repo)
    unsettled = {
        "raw": [{"event": "message_start", "usage": {"input_tokens": 11, "output_tokens": 0}}],
        "field_presence": {"input_tokens": True, "output_tokens": True},
        "normalized": {"input_tokens": None, "output_tokens": None},
        "observed_cumulative": {"input_tokens": 11, "output_tokens": 0},
        "settled": False,
        "settlement_identity": "provider_request_1",
    }
    final = {
        **unsettled,
        "raw": [*unsettled["raw"], {"event": "message_delta", "usage": {"output_tokens": 7}}],
        "normalized": {"input_tokens": 11, "output_tokens": 7},
        "observed_cumulative": {"input_tokens": 11, "output_tokens": 7},
        "settled": True,
    }
    for seq, raw in enumerate((unsettled, final, final, unsettled), 1):
        ingest(
            repo,
            gid,
            binding,
            f"provider_usage_observation_{seq}",
            seq,
            "usage",
            raw_usage=raw,
            usage_fields=raw["field_presence"],
            usage_source="provider_boundary",
            settlement_id="provider_request_1",
        )
        records = repo.get_generation(gid)["usage"]
        assert len(records) == 1
        if seq == 1:
            assert records[0]["normalized"]["output_tokens"] is None
        else:
            assert records[0]["raw_usage"] == final
            assert records[0]["normalized"]["output_tokens"] == 7
    conflict = {**final, "normalized": {"input_tokens": 11, "output_tokens": 8}}
    with pytest.raises(StorageError, match="USAGE_SETTLEMENT_CONFLICT"):
        ingest(
            repo,
            gid,
            binding,
            "provider_usage_conflicting_final",
            5,
            "usage",
            raw_usage=conflict,
            usage_fields=conflict["field_presence"],
            usage_source="provider_boundary",
            settlement_id="provider_request_1",
        )
    assert repo.counts()["usage_records"] == 1
    assert repo.counts()["execution_events"] == 4
    assert repo.get_generation(gid)["usage"][0]["normalized"]["output_tokens"] == 7


def test_migration_recovery_snapshot_preserves_existing_assets_on_failure(tmp_path):
    root = tmp_path / "migration"
    root.mkdir()
    with sqlite3.connect(root / "business.sqlite3") as conn:
        conn.execute("CREATE TABLE previous_asset(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO previous_asset VALUES ('asset_1', '用户已有数据')")
    with pytest.raises(InjectedFailure):
        Repository(root, fault_hook=stop_at("migration_before_commit"))
    backups = list((root / "migration-recovery").glob("*.sqlite3"))
    assert len(backups) == 1
    for path in (root / "business.sqlite3", backups[0]):
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT value FROM previous_asset").fetchone()[0] == "用户已有数据"
            assert (
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='generations'").fetchone()
                is None
            )
    recovered = Repository(root)
    assert recovered.runtime_info()["schema_revision"] == "0001_business"
    with sqlite3.connect(root / "business.sqlite3") as conn:
        assert conn.execute("SELECT value FROM previous_asset").fetchone()[0] == "用户已有数据"
