"""Business-context and version invariants against the real temporary repository.

These are storage/contract checks. They do not claim provider dispatch, Host recovery,
Android synchronization, or a model's semantic understanding of the context.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from learning_app.storage import Repository, StorageError

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "history-example.json"
SECOND_CROP_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


@pytest.fixture
def history_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    repo = Repository(tmp_path / "business")
    repo.create_course("数学课程 A", course_id="course_a")
    repo.create_course("数学课程 B", course_id="course_b")
    repo.create_chat("course_a", chat_id="chat_c1")
    repo.create_chat("course_b", chat_id="chat_other")
    return repo


def install_source(repository: Repository, tmp_path: Path, contract: dict) -> Path:
    source = contract["source"]
    first = source["versions"][0]
    source_path = tmp_path / source["display_name"]
    source_path.write_text(first["fixture_content_utf8"], encoding="utf-8")
    repository.register_source(
        contract["course_id"],
        source_path,
        asset_id=source["asset_id"],
        display_name=source["display_name"],
        source_version=first["source_version"],
    )
    return source_path


def install_attachment(repository: Repository, contract: dict) -> str:
    attachment = contract["confirmed_attachment"]
    data = base64.b64decode(attachment["fixture_base64"], validate=True)
    assert len(data) == attachment["byte_length"]
    assert hashlib.sha256(data).hexdigest() == attachment["sha256"]
    repository.stage_attachment(
        data,
        attachment["media_type"],
        confirmed=True,
        attachment_id=attachment["attachment_id"],
    )
    return attachment["attachment_id"]


def message_by_id(repository: Repository, message_id: str) -> dict:
    return next(
        message
        for message in repository.get_chat("chat_c1")["messages"]
        if message["message_id"] == message_id
    )


def test_source_versions_do_not_silently_retarget_old_submissions(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    """V2-11: identical display paths do not identify immutable source contents."""
    source_path = install_source(repository, tmp_path, history_contract)
    attachment_id = install_attachment(repository, history_contract)
    old_locator = {"asset_id": "source_math_1", "source_version": "SOURCE-v1", "page": 1}
    submitted = repository.publish_submission(
        "chat_c1",
        "按 v1 和我确认的题图分析。",
        "sub_source_v1",
        attachment_ids=[attachment_id],
        source_locators=[old_locator],
    )

    source_path.write_text(
        history_contract["source"]["versions"][1]["fixture_content_utf8"], encoding="utf-8"
    )
    repository.add_source_version("source_math_1", source_path, source_version="SOURCE-v2")

    assert repository.resolve_source("source_math_1", "SOURCE-v1")["status"] == ("version_mismatch")
    assert repository.resolve_source("source_math_1", "SOURCE-v2")["status"] == "available"
    old_message = message_by_id(repository, submitted["user_message_id"])
    assert old_message["source_locators"] == [old_locator]
    assert old_message["attachment_ids"] == [attachment_id]
    assert Path(repository.resolve_attachment(attachment_id)["path"]).is_file()

    source_path.unlink()
    assert repository.resolve_source("source_math_1", "SOURCE-v1")["status"] == "missing"
    assert repository.resolve_source("source_math_1", "SOURCE-v2")["status"] == "missing"
    relocated_v1 = tmp_path / "relocated-v1.txt"
    relocated_v1.write_text(
        history_contract["source"]["versions"][0]["fixture_content_utf8"], encoding="utf-8"
    )
    repository.relocate_source("source_math_1", "SOURCE-v1", relocated_v1)

    resolved = repository.resolve_source("source_math_1", "SOURCE-v1")
    assert resolved["asset_id"] == "source_math_1"
    assert resolved["source_version"] == "SOURCE-v1"
    assert resolved["status"] == "available"
    assert message_by_id(repository, submitted["user_message_id"])["source_locators"] == [
        old_locator
    ]


def test_relocation_rejects_different_contents(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    install_source(repository, tmp_path, history_contract)
    wrong_version = tmp_path / "same-name-new-content.txt"
    wrong_version.write_text("different source bytes", encoding="utf-8")

    with pytest.raises(StorageError):
        repository.relocate_source("source_math_1", "SOURCE-v1", wrong_version)

    assert repository.resolve_source("source_math_1", "SOURCE-v1")["status"] == "available"


def test_source_association_is_checked_for_the_submitting_course(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    """The existence of a shared asset does not grant another course access."""
    install_source(repository, tmp_path, history_contract)
    before = repository.counts()

    with pytest.raises(StorageError):
        repository.publish_submission(
            "chat_other",
            "读取未关联到本课程的资料。",
            "sub_wrong_course",
            source_locators=[{"asset_id": "source_math_1", "source_version": "SOURCE-v1"}],
        )

    assert repository.counts() == before


def test_missing_confirmed_attachment_blocks_only_dependent_questions(
    repository: Repository, history_contract: dict
) -> None:
    """V2-13: metadata and source locators cannot substitute for image bytes."""
    attachment_id = install_attachment(repository, history_contract)
    repository.merge_completed_history(
        "chat_c1",
        [
            {
                "message_id": "msg_image_question",
                "role": "user",
                "text": "请保留我确认的题图。",
                "status": "completed",
                "attachment_ids": [attachment_id],
            }
        ],
    )
    Path(repository.resolve_attachment(attachment_id)["path"]).unlink()
    before = repository.counts()

    with pytest.raises(StorageError) as missing_context:
        repository.build_context("chat_c1")
    assert missing_context.value.code == "ATTACHMENT_MISSING"

    with pytest.raises(StorageError) as missing_submission:
        repository.publish_submission(
            "chat_c1", "接着分析这张题图。", "sub_missing_image", attachment_ids=[attachment_id]
        )
    assert missing_submission.value.code == "ATTACHMENT_MISSING"
    assert repository.counts() == before

    independent = repository.publish_submission("chat_other", "计算 1+1。", "sub_independent")
    assert independent["created"] is True
    assert independent["state"] == "pending"


def test_new_crop_keeps_the_previous_submission_and_attachment_immutable(
    repository: Repository, history_contract: dict
) -> None:
    """A new draft crop is a new asset, never an edit of already published bytes."""
    first_id = install_attachment(repository, history_contract)
    first_bytes = Path(repository.resolve_attachment(first_id)["path"]).read_bytes()
    submitted = repository.publish_submission(
        "chat_c1", "这是旧裁剪。", "sub_original_crop", attachment_ids=[first_id]
    )
    before = repository.counts()

    # A valid black-pixel PNG, distinct from the first fixture's white-pixel PNG.
    second_bytes = SECOND_CROP_PNG
    repository.stage_attachment(second_bytes, "image/png", attachment_id="attach_crop_a2")

    assert message_by_id(repository, submitted["user_message_id"])["attachment_ids"] == [first_id]
    assert Path(repository.resolve_attachment(first_id)["path"]).read_bytes() == first_bytes
    assert (
        Path(repository.resolve_attachment("attach_crop_a2")["path"]).read_bytes() == second_bytes
    )
    assert repository.counts()["submissions"] == before["submissions"]
    assert repository.counts()["generations"] == before["generations"]


def test_completed_history_merges_twice_without_queuing_old_questions(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    """V2-12's storage layer: merge facts and preserve the user's chat identity."""
    install_source(repository, tmp_path, history_contract)
    install_attachment(repository, history_contract)
    repository.merge_completed_history("chat_c1", history_contract["initial_completed_history"])
    first = repository.merge_completed_history(
        "chat_c1", history_contract["external_completed_history"]
    )
    repeated = repository.merge_completed_history(
        "chat_c1", history_contract["external_completed_history"]
    )

    assert first["inserted"] == 2
    assert repeated["inserted"] == 0
    assert repeated["duplicates"] == 2
    assert repository.counts()["messages"] == 4
    assert repository.counts()["submissions"] == 0
    assert repository.counts()["generations"] == 0
    assert repository.counts()["outbound_attempts"] == 0
    assert repository.counts()["usage_records"] == 0
    assert repository.counts()["execution_events"] == 0
    messages = repository.get_chat("chat_c1")["messages"]
    assert {message["message_id"] for message in messages} == {
        "msg_u1",
        "msg_a1",
        "msg_u2",
        "msg_a2",
    }
    assert all(message["status"] == "completed" for message in messages)

    next_question = history_contract["next_question"]
    submitted = repository.publish_submission(
        "chat_c1",
        next_question["text"],
        next_question["submission_id"],
        attachment_ids=next_question["required_attachment_ids"],
    )
    assert submitted["created"] is True
    assert repository.counts()["submissions"] == 1
    assert repository.counts()["generations"] == 1
    assert repository.get_generation(submitted["generation_id"])["chat_id"] == "chat_c1"


def test_context_preserves_original_picture_and_latest_correction_after_restart(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    """Full history remains traceable while the effective correction stays explicit."""
    install_source(repository, tmp_path, history_contract)
    attachment_id = install_attachment(repository, history_contract)
    repository.merge_completed_history("chat_c1", history_contract["initial_completed_history"])
    repository.merge_completed_history("chat_c1", history_contract["external_completed_history"])
    submitted = repository.publish_submission(
        "chat_c1", history_contract["next_question"]["text"], "sub_u3"
    )
    reopened = Repository(tmp_path / "business")
    snapshot = reopened.build_context("chat_c1", exclude_message_id=submitted["user_message_id"])

    projected_ids = {message["message_id"] for message in snapshot["messages"]}
    assert {"msg_u1", "msg_a1", "msg_u2", "msg_a2"}.issubset(projected_ids)
    assert submitted["user_message_id"] not in projected_ids
    assert attachment_id in snapshot["required_attachment_ids"]
    assert "a=2" in snapshot["conditions"]
    assert "a=2 replaces a=1" in snapshot["corrections"]
    assert message_by_id(reopened, "msg_u1")["conditions"] == ["a=1"]
    assert message_by_id(reopened, "msg_u2")["conditions"] == ["a=2"]
    assert reopened.counts()["messages"] == repository.counts()["messages"]
    assert reopened.counts()["generations"] == 1


@pytest.mark.parametrize("changed_field", ["text", "attachment_ids"])
def test_conflicting_history_identity_rolls_back_the_entire_merge(
    repository: Repository, tmp_path: Path, history_contract: dict, changed_field: str
) -> None:
    """An existing business ID cannot silently acquire another input or picture."""
    install_source(repository, tmp_path, history_contract)
    install_attachment(repository, history_contract)
    repository.stage_attachment(SECOND_CROP_PNG, attachment_id="attach_crop_a2")
    repository.merge_completed_history("chat_c1", history_contract["initial_completed_history"])
    repository.merge_completed_history("chat_c1", history_contract["external_completed_history"])
    before_counts = repository.counts()
    before_chat = repository.get_chat("chat_c1")
    conflicting = dict(history_contract["external_completed_history"][0])
    conflicting[changed_field] = (
        "不同的题干，不得覆盖旧记录。" if changed_field == "text" else ["attach_crop_a2"]
    )

    with pytest.raises(StorageError) as conflict:
        repository.merge_completed_history(
            "chat_c1",
            [
                {
                    "message_id": "msg_must_rollback",
                    "role": "user",
                    "text": "本条与之后的冲突在同一个事务中。",
                    "status": "completed",
                },
                conflicting,
            ],
        )

    assert conflict.value.code == "HISTORY_MESSAGE_CONFLICT"
    assert repository.counts() == before_counts
    assert repository.get_chat("chat_c1") == before_chat


def test_explicit_input_revision_keeps_the_original_generation_snapshot(
    repository: Repository, tmp_path: Path, history_contract: dict
) -> None:
    """The live business revision and a previously admitted execution stay distinct."""
    install_source(repository, tmp_path, history_contract)
    first_id = install_attachment(repository, history_contract)
    submitted = repository.publish_submission(
        "chat_c1",
        "原题条件是 a=1。",
        "sub_before_revision",
        attachment_ids=[first_id],
        conditions=["a=1"],
        source_locators=[{"asset_id": "source_math_1", "source_version": "SOURCE-v1"}],
    )
    old_snapshot = repository.get_generation(submitted["generation_id"])["context_snapshot"]
    old_message = message_by_id(repository, submitted["user_message_id"])
    repository.stage_attachment(SECOND_CROP_PNG, attachment_id="attach_crop_a2")
    before = repository.counts()

    revised = repository.revise_input(
        submitted["user_message_id"],
        "更正：条件为 a=2，使用新确认的裁剪。",
        attachment_ids=["attach_crop_a2"],
        conditions=["a=2"],
        corrections=["a=2 replaces a=1"],
        source_locators=[{"asset_id": "source_math_1", "source_version": "SOURCE-v1"}],
    )
    reopened = Repository(tmp_path / "business")
    latest = reopened.build_context("chat_c1")
    frozen = reopened.get_generation(submitted["generation_id"])["context_snapshot"]

    assert revised["supersedes"] == old_message["revision_id"]
    assert revised["revision_id"] != old_message["revision_id"]
    assert frozen == old_snapshot
    assert frozen["required_attachment_ids"] == [first_id]
    assert frozen["conditions"] == ["a=1"]
    assert latest["required_attachment_ids"] == ["attach_crop_a2"]
    assert latest["conditions"] == ["a=2"]
    assert latest["corrections"] == ["a=2 replaces a=1"]
    assert latest["context_hash"] != frozen["context_hash"]
    assert (
        message_by_id(reopened, submitted["user_message_id"])["revision_id"]
        == (revised["revision_id"])
    )
    assert reopened.counts()["input_revisions"] == before["input_revisions"] + 1
    assert reopened.counts()["submissions"] == before["submissions"]
    assert reopened.counts()["generations"] == before["generations"]
    assert Path(reopened.resolve_attachment(first_id)["path"]).is_file()
