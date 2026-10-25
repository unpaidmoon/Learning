"""Synthetic selection-card and software-capture boundary checks.

No test acquires screen/video content or invokes a Harness/provider.
"""

import io

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import update

from learning_app.config import AppConfig
from learning_app.drafts import (
    AttachmentSourceInput,
    DraftClearInput,
    DraftInput,
    DraftReference,
    clear_draft,
    message_references,
    read_attachment_source,
    read_draft,
    reference_conditions,
    save_attachment_source,
    save_draft,
    submission_reference_conditions,
    submission_source_locators,
)
from learning_app.dto import ChatResponse, SubmissionInput
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business


def _setup(tmp_path):
    repository = Repository(tmp_path / "data")
    course = repository.create_course("合成课程")
    chat = repository.create_chat(course["course_id"])
    return repository, course["course_id"], chat["chat_id"]


def _answer(repository, chat_id, text):
    submission = repository.publish_submission(chat_id, "合成来源问题", "source_" + chat_id)
    repository.request_cancel(submission["generation_id"])
    with repository.engine.begin() as conn:
        conn.execute(
            update(business.messages)
            .where(business.messages.c.message_id == submission["assistant_message_id"])
            .values(text=text)
        )
    return submission["assistant_message_id"]


def _reference(message_id, source, selected="x+1", *, reference_id="selected_1"):
    index = source.index(selected)
    start = len(source[:index].encode("utf-16-le")) // 2
    end = start + len(selected.encode("utf-16-le")) // 2
    return DraftReference(
        reference_id=reference_id,
        message_id=message_id,
        text=selected,
        markdown=f"${selected}$",
        source_start=start,
        source_end=end,
    )


def _image(repository, size=(20, 15)):
    stream = io.BytesIO()
    Image.new("RGB", size, "blue").save(stream, "PNG")
    return repository.stage_attachment(stream.getvalue(), confirmed=True)["attachment_id"]


def test_reference_card_survives_reopen_and_late_callbacks(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    source = "请观察 $x+1$ 的符号。"
    reference = _reference(_answer(repository, chat_id, source), source)
    saved = save_draft(
        repository,
        chat_id,
        DraftInput(expected_revision=0, text="为什么？", references=[reference]),
    )
    assert saved["references"] == [reference.model_dump()]
    repository.close()
    repository = Repository(tmp_path / "data")
    assert read_draft(repository, chat_id) == saved
    with pytest.raises(AppError) as failure:
        save_draft(repository, chat_id, DraftInput(expected_revision=0, references=[]))
    assert failure.value.code == "DRAFT_REVISION_CONFLICT"
    assert read_draft(repository, chat_id)["references"] == [reference.model_dump()]
    repository.close()


def test_utf16_ranges_and_partial_math_selection_are_exact(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    source = "😀 先看 $x+1$，然后看 $x+2$。"
    reference = _reference(_answer(repository, chat_id, source), source)
    conditions = submission_reference_conditions(repository, chat_id, [reference])
    assert message_references(conditions) == [reference.model_dump()]
    assert "x+2" not in conditions[0]
    broken = reference.model_copy(update={"source_start": 1, "source_end": 2})
    with pytest.raises(AppError) as failure:
        submission_reference_conditions(repository, chat_id, [broken])
    assert failure.value.code == "REFERENCE_RANGE_INVALID"
    repository.close()


@pytest.mark.parametrize("change", [{"markdown": "$x+2$"}, {"source_end": 9000}])
def test_reference_rejects_changed_content_and_outside_range(tmp_path, change):
    repository, _, chat_id = _setup(tmp_path)
    source = "公式 $x+1$。"
    reference = _reference(_answer(repository, chat_id, source), source).model_copy(update=change)
    with pytest.raises(AppError):
        save_draft(repository, chat_id, DraftInput(expected_revision=0, references=[reference]))
    assert read_draft(repository, chat_id)["revision"] == 0
    repository.close()


def test_reference_source_must_be_answer_in_current_chat(tmp_path):
    repository, course_id, chat_id = _setup(tmp_path)
    other = repository.create_chat(course_id)["chat_id"]
    source = "公式 $x+1$。"
    reference = _reference(_answer(repository, other, source), source)
    with pytest.raises(AppError) as failure:
        save_draft(repository, chat_id, DraftInput(expected_revision=0, references=[reference]))
    assert failure.value.code == "REFERENCE_SOURCE_NOT_IN_CHAT"
    repository.close()


def test_empty_math_wrapper_cannot_match_every_answer(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    message_id = _answer(repository, chat_id, "纯文字回答")
    reference = DraftReference(
        reference_id="empty_math", message_id=message_id, text="伪造选区", markdown="$ $"
    )
    with pytest.raises(AppError) as failure:
        submission_reference_conditions(repository, chat_id, [reference])
    assert failure.value.code == "REFERENCE_SELECTION_MISMATCH"
    repository.close()


def test_clear_requires_frozen_references_and_preserves_new_cards(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    source = "公式 $x+1$。"
    reference = _reference(_answer(repository, chat_id, source), source)
    draft = DraftInput(
        expected_revision=0,
        text="解释选区\n保留原条件",
        references=[reference],
    )
    save_draft(repository, chat_id, draft)
    repository.publish_submission(
        chat_id,
        draft.text,
        "reference_submission",
        conditions=draft.conditions + reference_conditions(draft.references),
    )
    cleared = clear_draft(
        repository,
        chat_id,
        DraftClearInput(expected_revision=1, submission_id="reference_submission"),
    )
    assert cleared["references"] == []
    newer = save_draft(
        repository,
        chat_id,
        DraftInput(expected_revision=cleared["revision"], references=[reference], text="新的输入"),
    )
    repeated = clear_draft(
        repository,
        chat_id,
        DraftClearInput(expected_revision=1, submission_id="reference_submission"),
    )
    assert repeated == newer
    repository.close()


def test_missing_reference_in_submission_never_clears_card(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    source = "公式 $x+1$。"
    reference = _reference(_answer(repository, chat_id, source), source)
    save_draft(
        repository,
        chat_id,
        DraftInput(expected_revision=0, text="解释", references=[reference]),
    )
    repository.publish_submission(chat_id, "解释", "missing_reference")
    with pytest.raises(AppError) as failure:
        clear_draft(
            repository,
            chat_id,
            DraftClearInput(expected_revision=1, submission_id="missing_reference"),
        )
    assert failure.value.code == "DRAFT_SUBMISSION_MISMATCH"
    assert read_draft(repository, chat_id)["references"]
    repository.close()


def test_workspace_crop_metadata_persists_without_original_or_video_claim(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    attachment_id = _image(repository)
    source = AttachmentSourceInput(
        chat_id=chat_id,
        origin="workspace",
        source_width=100,
        source_height=80,
        crop={"x": 30, "y": 25, "width": 20, "height": 15},
        edit_root_id="workspace_original_local_only",
    )
    saved = save_attachment_source(repository, attachment_id, source)
    assert saved["origin"] == "workspace"
    assert saved["asset_id"] is None
    assert read_attachment_source(repository, attachment_id) == saved
    assert submission_source_locators(repository, chat_id, [attachment_id]) == []
    assert set(saved) == {"attachment_id", *AttachmentSourceInput.model_fields}
    with pytest.raises(ValidationError):
        AttachmentSourceInput.model_validate({**source.model_dump(), "original_data_base64": "bad"})
    with pytest.raises(ValidationError):
        AttachmentSourceInput.model_validate({**source.model_dump(), "asset_id": "incomplete"})
    repository.close()


def test_reference_range_and_identity_models_reject_ambiguous_input():
    common = {"reference_id": "ref", "message_id": "message", "text": "x", "markdown": "$x$"}
    for extra in ({"source_start": 0}, {"source_start": 2, "source_end": 2}):
        with pytest.raises(ValidationError):
            DraftReference.model_validate({**common, **extra})
    reference = DraftReference.model_validate(common)
    with pytest.raises(ValidationError):
        DraftInput(expected_revision=0, references=[reference, reference])


async def test_service_freezes_reference_and_restores_history_without_dispatch(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    source = "结果为 $x+1$。"
    reference = _reference(_answer(repository, chat_id, source), source)
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    service = GenerationService(repository, config)
    service.execution_ready = True
    value = SubmissionInput(
        chat_id=chat_id,
        submission_id="reference_publish_once",
        text="说明这一项\n其他条件",
        references=[reference],
    ).model_dump()
    result = await service.submit(value)
    generation = repository.get_generation(result["generation_id"])
    assert generation["context_snapshot"]["conditions"] == [
        *reference_conditions([reference]),
    ]
    view = ChatResponse.model_validate(service.chat_view(chat_id))
    published = next(
        message for message in view.messages if message.message_id == result["user_message_id"]
    )
    assert published.references == [reference]
    # Cancel the not-yet-dispatched coroutine before yielding; no execution
    # provider is attached, and this test intentionally verifies publication.
    await service.close()
    assert repository.get_generation(result["generation_id"])["state"] == "pending"
    repository.close()
