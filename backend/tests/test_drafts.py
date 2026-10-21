"""Persistence and input-boundary checks using generated images and fake source bytes."""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.drafts import (
    AttachmentSourceInput,
    DraftClearInput,
    DraftInput,
    clear_draft,
    install_draft_routes,
    read_attachment_source,
    read_draft,
    save_attachment_source,
    save_draft,
    submission_source_locators,
)
from learning_app.errors import AppError
from learning_app.storage import Repository, StorageError


def _setup(tmp_path):
    repository = Repository(tmp_path / "data")
    course = repository.create_course("合成测试课程")
    chat = repository.create_chat(course["course_id"])
    return repository, course["course_id"], chat["chat_id"]


def _image(repository, size=(20, 15), *, confirmed=True):
    data = io.BytesIO()
    Image.new("RGB", size, "blue").save(data, "PNG")
    return repository.stage_attachment(data.getvalue(), confirmed=confirmed)["attachment_id"]


def _video_source(repository, course_id, chat_id, tmp_path):
    # Hash-only source identity fixture: no frame is read or acquired.
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic-source-version-one")
    registered = repository.register_source(course_id, source, source_version="fixture-v1")
    return AttachmentSourceInput(
        chat_id=chat_id,
        asset_id=registered["asset_id"],
        source_version=registered["source_version"],
        time_ms=12_345,
        source_width=100,
        source_height=80,
        crop={"x": 30, "y": 25, "width": 20, "height": 15},
        edit_root_id="capture_original_1",
    )


def test_draft_survives_repository_reopen_and_rejects_late_save(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    assert read_draft(repository, chat_id)["revision"] == 0
    attachment_id = _image(repository)
    value = DraftInput(
        expected_revision=0,
        text="题目条件保持原样",
        attachment_ids=[attachment_id],
        conditions=["x>0"],
        corrections=["更正为x=2"],
        model="deepseek-flash",
        thinking="max",
    )
    saved = save_draft(repository, chat_id, value)
    assert saved["revision"] == 1
    repository.close()
    repository = Repository(tmp_path / "data")
    assert read_draft(repository, chat_id) == saved
    with pytest.raises(AppError) as error:
        save_draft(repository, chat_id, value.model_copy(update={"text": "旧回调"}))
    assert error.value.code == "DRAFT_REVISION_CONFLICT"
    assert read_draft(repository, chat_id)["text"] == "题目条件保持原样"
    assert repository.get_chat(chat_id)["messages"] == []
    repository.close()


def test_clear_requires_matching_accepted_submission_and_keeps_new_draft(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    saved = save_draft(
        repository,
        chat_id,
        DraftInput(expected_revision=0, text="原问题", model="selected-model", thinking="high"),
    )
    clear = DraftClearInput(expected_revision=saved["revision"], submission_id="submitted-once")
    with pytest.raises(AppError, match="尚未确认"):
        clear_draft(repository, chat_id, clear)
    repository.publish_submission(chat_id, "原问题", "submitted-once")
    cleared = clear_draft(repository, chat_id, clear)
    assert cleared["text"] == ""
    assert cleared["model"] == "selected-model"
    assert cleared["thinking"] == "high"
    assert cleared["revision"] == 2
    assert clear_draft(repository, chat_id, clear) == cleared
    newer = save_draft(repository, chat_id, DraftInput(expected_revision=2, text="新草稿"))
    assert clear_draft(repository, chat_id, clear) == newer
    assert read_draft(repository, chat_id)["text"] == "新草稿"
    assert len(repository.get_chat(chat_id)["messages"]) == 2
    repository.close()


def test_clear_never_erases_different_or_newer_input(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    save_draft(repository, chat_id, DraftInput(expected_revision=0, text="未发送的新问题"))
    repository.publish_submission(chat_id, "另一个提交", "accepted")
    with pytest.raises(AppError) as error:
        clear_draft(
            repository, chat_id, DraftClearInput(expected_revision=1, submission_id="accepted")
        )
    assert error.value.code == "DRAFT_SUBMISSION_MISMATCH"
    with pytest.raises(AppError) as error:
        clear_draft(
            repository, chat_id, DraftClearInput(expected_revision=0, submission_id="accepted")
        )
    assert error.value.code == "DRAFT_REVISION_CONFLICT"
    assert read_draft(repository, chat_id)["text"] == "未发送的新问题"
    repository.close()


def test_draft_accepts_only_intact_confirmed_attachments(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    unconfirmed = _image(repository, confirmed=False)
    with pytest.raises(AppError) as error:
        save_draft(
            repository, chat_id, DraftInput(expected_revision=0, attachment_ids=[unconfirmed])
        )
    assert error.value.code == "ATTACHMENT_NOT_CONFIRMED"
    attachment_id = _image(repository, size=(21, 15))
    repository.resolve_attachment(attachment_id)["path"].unlink()
    with pytest.raises(StorageError) as error:
        save_draft(
            repository, chat_id, DraftInput(expected_revision=0, attachment_ids=[attachment_id])
        )
    assert error.value.code == "ATTACHMENT_MISSING"
    assert read_draft(repository, chat_id)["revision"] == 0
    repository.close()


def test_confirmed_source_is_immutable_and_reedit_keeps_original_identity(tmp_path):
    repository, course_id, chat_id = _setup(tmp_path)
    value = _video_source(repository, course_id, chat_id, tmp_path)
    attachment_id = _image(repository)
    saved = save_attachment_source(repository, attachment_id, value)
    assert save_attachment_source(repository, attachment_id, value) == saved
    with pytest.raises(AppError) as error:
        save_attachment_source(repository, attachment_id, value.model_copy(update={"time_ms": 1}))
    assert error.value.code == "ATTACHMENT_SOURCE_CONFLICT"
    child_id = _image(repository, size=(10, 10))
    child = value.model_copy(
        update={
            "parent_attachment_id": attachment_id,
            "crop": value.crop.model_copy(update={"width": 10, "height": 10}),
        }
    )
    child_saved = save_attachment_source(repository, child_id, child)
    assert child_saved["edit_root_id"] == saved["edit_root_id"]
    assert child_saved["parent_attachment_id"] == attachment_id
    bad_child_id = _image(repository, size=(10, 10))
    with pytest.raises(AppError) as error:
        save_attachment_source(
            repository, bad_child_id, child.model_copy(update={"edit_root_id": "different_frame"})
        )
    assert error.value.code == "EDIT_SOURCE_MISMATCH"
    # Original source disappearance does not erase an already confirmed crop or
    # remap its provenance to a different source version.
    (tmp_path / "synthetic.mp4").unlink()
    assert read_attachment_source(repository, attachment_id) == saved
    repository.close()


def test_source_bounds_image_dimensions_and_course_membership_are_enforced(tmp_path):
    repository, course_id, chat_id = _setup(tmp_path)
    value = _video_source(repository, course_id, chat_id, tmp_path)
    attachment_id = _image(repository)
    with pytest.raises(ValidationError):
        AttachmentSourceInput.model_validate({**value.model_dump(), "source_width": 49})
    with pytest.raises(ValidationError):
        AttachmentSourceInput.model_validate({**value.model_dump(), "original_data_base64": "bad"})
    with pytest.raises(AppError) as error:
        save_attachment_source(repository, _image(repository, size=(21, 15)), value)
    assert error.value.code == "CROP_IMAGE_MISMATCH"
    other_course = repository.create_course("另一课程")["course_id"]
    other_chat = repository.create_chat(other_course)["chat_id"]
    with pytest.raises(AppError) as error:
        save_attachment_source(
            repository, attachment_id, value.model_copy(update={"chat_id": other_chat})
        )
    assert error.value.code == "SOURCE_NOT_IN_COURSE"
    save_attachment_source(repository, attachment_id, value)
    with pytest.raises(AppError) as error:
        save_draft(
            repository, other_chat, DraftInput(expected_revision=0, attachment_ids=[attachment_id])
        )
    assert error.value.code == "SOURCE_NOT_IN_COURSE"
    repository.close()


def test_external_image_provenance_cannot_claim_video_or_fetch_original(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    attachment_id = _image(repository)
    source = AttachmentSourceInput(
        chat_id=chat_id,
        origin="image",
        source_width=50,
        source_height=50,
        crop={"x": 10, "y": 10, "width": 20, "height": 15},
        edit_root_id="pasted_image_1",
    )
    with pytest.raises(ValidationError):
        AttachmentSourceInput.model_validate({**source.model_dump(), "asset_id": "video"})
    save_attachment_source(repository, attachment_id, source)
    save_draft(repository, chat_id, DraftInput(expected_revision=0, attachment_ids=[attachment_id]))
    assert repository.get_chat(chat_id)["messages"] == []
    repository.publish_submission(
        chat_id, "只看确认选区", "image_submission", attachment_ids=[attachment_id]
    )
    context = repository.build_context(chat_id)
    assert context["required_attachment_ids"] == [attachment_id]
    assert "pasted_image_1" not in str(context)
    assert "original" not in str(context)
    repository.close()


def test_draft_http_uses_existing_session_and_csrf_boundary(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    app = create_app(config)
    if not any(route.path == "/api/v1/chats/{chat_id}/draft" for route in app.routes):
        install_draft_routes(app)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        repository = app.state.repository
        course_id = repository.create_course("HTTP测试")["course_id"]
        chat_id = repository.create_chat(course_id)["chat_id"]
        path = f"/api/v1/chats/{chat_id}/draft"
        assert client.get(path).status_code == 403
        session = client.get("/api/v1/session").json()
        assert client.get(path).json()["revision"] == 0
        assert (
            client.post(path, json={"expected_revision": 0, "text": "blocked"}).status_code == 403
        )
        headers = {"X-Learning-CSRF": session["csrf_token"]}
        response = client.post(
            path, headers=headers, json={"expected_revision": 0, "text": "已保存"}
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 1
        assert (
            client.post(
                path, headers=headers, json={"expected_revision": 0, "text": "迟到"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                path, headers=headers, json={"expected_revision": 1, "original_path": "no"}
            ).status_code
            == 422
        )
        assert client.get(path).json()["text"] == "已保存"


def test_submission_locators_freeze_only_confirmed_crop_metadata_without_original_access(tmp_path):
    repository, course_id, chat_id = _setup(tmp_path)
    source = _video_source(repository, course_id, chat_id, tmp_path)
    attachment_id = _image(repository)
    save_attachment_source(repository, attachment_id, source)
    (tmp_path / "synthetic.mp4").unlink()
    locators = submission_source_locators(repository, chat_id, [attachment_id, attachment_id])
    assert len(locators) == 1
    assert locators[0]["time_ms"] == 12_345
    assert locators[0]["crop"] == {"x": 30, "y": 25, "width": 20, "height": 15}
    assert locators[0]["attachment_id"] == attachment_id
    assert "local_path" not in locators[0]
    receipt = repository.publish_submission(
        chat_id,
        "确认选区仍可提问",
        "source_metadata_submission",
        attachment_ids=[attachment_id],
        source_locators=locators,
    )
    published = repository.get_chat(chat_id)["messages"][0]
    assert published["source_locators"] == locators
    assert receipt["created"] is True
    repository.close()


def test_submission_locators_keep_legacy_and_pasted_image_compatibility(tmp_path):
    repository, _, chat_id = _setup(tmp_path)
    legacy_id = _image(repository)
    pasted_id = _image(repository)
    save_attachment_source(
        repository,
        pasted_id,
        AttachmentSourceInput(
            chat_id=chat_id,
            origin="image",
            source_width=20,
            source_height=15,
            crop={"x": 0, "y": 0, "width": 20, "height": 15},
            edit_root_id="paste_origin",
        ),
    )
    assert submission_source_locators(repository, chat_id, [legacy_id, pasted_id]) == []
    repository.close()


def test_direct_http_submission_cannot_bypass_draft_course_boundary(tmp_path):
    repository, course_id, chat_id = _setup(tmp_path)
    source = _video_source(repository, course_id, chat_id, tmp_path)
    attachment_id = _image(repository)
    save_attachment_source(repository, attachment_id, source)
    other_course_id = repository.create_course("不相关课程")["course_id"]
    other_chat_id = repository.create_chat(other_course_id)["chat_id"]
    assert read_draft(repository, other_chat_id)["revision"] == 0
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    # The supplied executor is never touched: course validation must reject at
    # the publication boundary before scheduling or dispatching any generation.
    app = create_app(config, repository=repository, execution=object())
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        session = client.get("/api/v1/session").json()
        response = client.post(
            "/api/v1/submissions",
            headers={
                "X-Learning-CSRF": session["csrf_token"],
            },
            json={
                "chat_id": other_chat_id,
                "submission_id": "direct_cross_course",
                "text": "直接提交，未经过草稿",
                "attachment_ids": [attachment_id],
                "model": "deepseek-flash",
                "thinking": "high",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "SOURCE_NOT_IN_COURSE"
        assert repository.get_chat(other_chat_id)["messages"] == []
        assert app.state.service.tasks == {}
