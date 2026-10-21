"""Atomic stream bursts preserve event identity and the original fault boundaries."""

import pytest

from learning_app.storage import Repository, StorageError


def active_store(tmp_path, fault_hook=None):
    repository = Repository(tmp_path / "data", fault_hook=fault_hook)
    repository.create_course("数学", "course")
    repository.create_chat("course", "chat")
    submission = repository.publish_submission("chat", "问题", "submission")
    binding = repository.bind_execution("chat", "session")
    gid = submission["generation_id"]
    repository.claim_dispatch(
        gid, binding["binding_id"], binding["epoch"], adapter_metadata={}
    )

    def row(identity, kind="body", **kwargs):
        return {
            "generation_id": gid, "binding_id": binding["binding_id"],
            "epoch": binding["epoch"], "event_id": f"event-{identity}",
            "seq": identity, "kind": kind, **kwargs,
        }

    return repository, gid, row


def test_batch_keeps_exact_events_duplicates_terminal_and_usage(tmp_path):
    hooks = []
    repository, gid, row = active_store(
        tmp_path, lambda name, details: hooks.append((name, details))
    )
    batch = [
        row(1, text="甲"), row(2, text="乙"), row(1, text="甲"),
        row(3, "terminal", status="completed"),
        row(4, "usage", raw_usage={"input_tokens": 7, "output_tokens": 2}),
    ]
    result = repository.ingest_events(batch)
    assert [item["duplicate"] for item in result] == [False, False, True, False, False]
    assert all(item["duplicate"] for item in repository.ingest_events(batch))
    generation = repository.get_generation(gid)
    assert generation["body"] == "甲乙" and generation["state"] == "completed"
    assert generation["event_watermark"] == 4
    assert len(generation["usage"]) == 1
    after = [details for name, details in hooks if name == "event_after_commit"]
    assert [item["event_id"] for item in after] == ["event-1", "event-2", "event-3", "event-4"]
    repository.close()


def test_failure_before_commit_rolls_back_whole_burst(tmp_path):
    hooks = []

    def fail(name, details):
        hooks.append(name)
        if name == "event_before_commit" and details["event_id"] == "event-2":
            raise RuntimeError("controlled batch crash")

    repository, gid, row = active_store(tmp_path, fail)
    with pytest.raises(RuntimeError, match="controlled batch crash"):
        repository.ingest_events([row(1, text="甲"), row(2, text="乙")])
    generation = repository.get_generation(gid)
    assert generation["body"] == "" and generation["event_watermark"] == 0
    assert "event_after_commit" not in hooks
    repository.close()


def test_conflicting_terminal_rolls_back_prior_batch_events(tmp_path):
    repository, gid, row = active_store(tmp_path)
    with pytest.raises(StorageError, match="TERMINAL_STATUS_CONFLICT"):
        repository.ingest_events([
            row(1, text="甲"), row(2, "terminal", status="completed"),
            row(3, "terminal", status="cancelled"),
        ])
    generation = repository.get_generation(gid)
    assert generation["body"] == "" and generation["event_watermark"] == 0
    assert generation["state"] == "dispatching"
    repository.close()
