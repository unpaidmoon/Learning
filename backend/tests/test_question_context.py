"""Submitted time metadata, scope ownership and no implicit image decoding."""

import pytest
from pydantic import ValidationError
from workbench_support import learning_course

from learning_app.dto import QuestionVideoContext, SubmissionInput
from learning_app.errors import AppError
from learning_app.storage import StorageError
from learning_app.workbench.question_context import freeze_subtitle_anchors, question_video_locator


def video_course(tmp_path):
    repo, course, root, selection = learning_course(tmp_path)
    path = root / "synthetic.wav"
    path.write_bytes(b"synthetic metadata fixture; no decoder is invoked")
    source = repo.register_source(course, path)
    repo.create_chat(course, "video_question")
    return repo, course, root, source, selection


def test_question_time_is_immutable_in_submission_and_survives_original_change(
    tmp_path, monkeypatch
):
    repo, course, root, source, _ = video_course(tmp_path)
    request = {
        "asset_id": source["asset_id"],
        "source_version": source["source_version"],
        "time_ms": 72_300,
    }

    # Metadata admission must not read/decode the current original or image.
    def no_source(*_args, **_kwargs):
        pytest.fail("question metadata must not resolve or decode source content")

    monkeypatch.setattr(repo, "resolve_source", no_source)
    locator = question_video_locator(repo, "video_question", request)
    value = SubmissionInput(
        chat_id="video_question",
        submission_id="question_time",
        text="解释当前这一段",
        question_video=request,
    )
    assert value.question_video.time_ms == 72_300
    first = repo.publish_submission(
        "video_question", value.text, value.submission_id, source_locators=[locator]
    )
    assert (
        repo.publish_submission(
            "video_question", value.text, value.submission_id, source_locators=[locator]
        )["created"]
        is False
    )
    with pytest.raises(StorageError) as changed:
        repo.publish_submission(
            "video_question",
            value.text,
            value.submission_id,
            source_locators=[{**locator, "time_ms": 90_000}],
        )
    assert changed.value.code == "SUBMISSION_CONFLICT"
    context = repo.build_context("video_question", first["assistant_message_id"])
    current = next(
        message
        for message in context["messages"]
        if message["message_id"] == first["user_message_id"]
    )
    (root / "synthetic.wav").write_bytes(b"changed original")
    with repo.engine.connect() as conn:
        anchors = freeze_subtitle_anchors(conn, course, context, current)
    assert len(anchors) == 1 and anchors[0]["time_ms"] == 72_300
    assert anchors[0]["source_digest"] == source["content_hash"]
    assert anchors[0]["source_version"] == source["source_version"]
    assert "image" not in anchors[0] and "path" not in anchors[0]
    repo.close()


def test_question_time_denies_other_course_and_document_sources(tmp_path):
    repo, _, _, source, selection = video_course(tmp_path)
    other = repo.create_course("other")["course_id"]
    repo.create_chat(other, "other_chat")
    with pytest.raises(AppError) as outside:
        question_video_locator(
            repo,
            "other_chat",
            {
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
                "time_ms": 0,
            },
        )
    assert outside.value.code == "SOURCE_COURSE_DENIED"
    with pytest.raises(AppError) as document:
        question_video_locator(
            repo,
            "video_question",
            {
                "asset_id": selection.reference.asset_id,
                "source_version": selection.reference.source_version,
                "time_ms": 0,
            },
        )
    assert document.value.code == "AUDIO_KIND_DENIED"
    repo.close()


@pytest.mark.parametrize("value", [True, 1.5, -1, float("nan"), float("inf"), 315_360_000_001])
def test_question_time_is_finite_strict_integer_metadata(value):
    with pytest.raises(ValidationError):
        QuestionVideoContext(asset_id="asset", source_version="version", time_ms=value)


def test_question_time_does_not_grant_paths_or_ranges():
    for extra in ({"path": "video.webm"}, {"start_ms": 0, "end_ms": 10**9}, {"frame": True}):
        with pytest.raises(ValidationError):
            QuestionVideoContext.model_validate(
                {"asset_id": "asset", "source_version": "version", "time_ms": 0, **extra}
            )
