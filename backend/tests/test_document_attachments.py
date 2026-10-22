"""Synthetic whole-file attachment coverage; no provider or user file reads."""

import asyncio
import base64
import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.document_processing import MAX_PDF_BYTES, MAX_TEXT_BYTES, prepare_document
from learning_app.documents import (
    DocumentAttachmentInput,
    attachment_metadata,
    document_content,
    expand_document_parts,
    install_document_routes,
    requires_images,
    upload_document,
    validate_document_scope,
)
from learning_app.drafts import DraftInput, save_draft
from learning_app.errors import AppError
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.service import GenerationService
from learning_app.storage import Repository, StorageError


def setup(tmp_path):
    repository = Repository(tmp_path / "data")
    course = repository.create_course("合成附件课程")
    chat = repository.create_chat(course["course_id"])
    return repository, course, chat


def pdf_bytes(pages=2):
    Image.init()
    pictures = [
        Image.new("RGB", (180, 100), "red" if index % 2 == 0 else "blue") for index in range(pages)
    ]
    stream = io.BytesIO()
    pictures[0].save(stream, "PDF", save_all=True, append_images=pictures[1:])
    for picture in pictures:
        picture.close()
    return stream.getvalue()


def value(chat_id, data, media_type="application/pdf", name="资料.pdf", upload_id=None):
    return DocumentAttachmentInput(
        chat_id=chat_id,
        filename=name,
        media_type=media_type,
        data_base64=base64.b64encode(data).decode(),
        confirmed=True,
        upload_id=upload_id,
    )


def test_whole_pdf_all_pages_prepared_and_original_not_used_as_image(tmp_path):
    repo, _course, chat = setup(tmp_path)
    try:
        original = pdf_bytes()
        result = upload_document(repo, value(chat["chat_id"], original, upload_id="pdf-two"))
        assert result["kind"] == "pdf" and result["page_count"] == 2
        assert result["sha256"] == hashlib.sha256(original).hexdigest()
        images, text = expand_document_parts(
            repo, chat["chat_id"], [repo.resolve_attachment(result["attachment_id"])]
        )
        assert len(images) == 2 and all(image["media_type"] == "image/jpeg" for image in images)
        assert images[0]["content_hash"] != images[1]["content_hash"]
        assert all(image["content_hash"] != result["sha256"] for image in images)
        assert "1至2" in text
        for index, item in enumerate(images):
            with Image.open(item["path"]) as page:
                pixel = page.convert("RGB").getpixel((20, 20))
                assert pixel[0] > 200 if index == 0 else pixel[2] > 200
        assert requires_images(repo, [result["attachment_id"]])
        assert len(repo.list_courses()) == 1
        assert repo.get_chat(chat["chat_id"])["messages"] == []
        assert not list((repo.root / "course-files").glob("*"))
    finally:
        repo.close()


@pytest.mark.parametrize(
    "media_type,name", [("text/plain", "资料.txt"), ("text/markdown", "资料.md")]
)
def test_text_and_markdown_are_complete_and_do_not_require_image_model(tmp_path, media_type, name):
    repo, _course, chat = setup(tmp_path)
    try:
        content = "开头中文\n" + "正文" * 300 + "\nEND-WHOLE-TEXT-SENTINEL"
        result = upload_document(repo, value(chat["chat_id"], content.encode(), media_type, name))
        assert result["character_count"] == len(content)
        images, text = expand_document_parts(
            repo, chat["chat_id"], [repo.resolve_attachment(result["attachment_id"])]
        )
        assert images == [] and content in text and "END-WHOLE-TEXT-SENTINEL" in text
        assert requires_images(repo, [result["attachment_id"]]) is False
        saved = save_draft(
            repo,
            chat["chat_id"],
            DraftInput(expected_revision=0, attachment_ids=[result["attachment_id"]]),
        )
        assert saved["attachment_ids"] == [result["attachment_id"]]
    finally:
        repo.close()


def test_document_upload_identity_idempotence_and_chat_scope(tmp_path):
    repo, _course, chat = setup(tmp_path)
    try:
        first = upload_document(
            repo, value(chat["chat_id"], b"first", "text/plain", "a.txt", "stable")
        )
        again = upload_document(
            repo, value(chat["chat_id"], b"first", "text/plain", "a.txt", "stable")
        )
        assert first == again
        assert len(repo.list_attachments()) == 1
        with pytest.raises(AppError, match="同一上传身份"):
            upload_document(
                repo, value(chat["chat_id"], b"changed", "text/plain", "a.txt", "stable")
            )
        other = repo.create_chat(chat["course_id"])
        with pytest.raises(AppError) as error:
            validate_document_scope(repo, other["chat_id"], [first["attachment_id"]])
        assert error.value.code == "DOCUMENT_NOT_IN_CHAT"
        with pytest.raises(AppError) as error:
            save_draft(
                repo,
                other["chat_id"],
                DraftInput(expected_revision=0, attachment_ids=[first["attachment_id"]]),
            )
        assert error.value.code == "DOCUMENT_NOT_IN_CHAT"
    finally:
        repo.close()


def test_partial_or_missing_pdf_page_blocks_all_input(tmp_path):
    repo, _course, chat = setup(tmp_path)
    try:
        result = upload_document(repo, value(chat["chat_id"], pdf_bytes(), upload_id="missing"))
        page = repo.resolve_attachment("document_missing_page_2")
        page["path"].unlink()  # This test owns this generated page.
        with pytest.raises(StorageError) as error:
            expand_document_parts(
                repo, chat["chat_id"], [repo.resolve_attachment(result["attachment_id"])]
            )
        assert error.value.code == "ATTACHMENT_MISSING"
    finally:
        repo.close()


def test_invalid_oversize_and_page_limit_reject_without_original_registration(tmp_path):
    repo, _course, chat = setup(tmp_path)
    try:
        for data, media_type, name, expected in [
            (b"not pdf", "application/pdf", "a.pdf", "DOCUMENT_INVALID"),
            (b"%PDF-1.4\ninvalid", "application/pdf", "a.pdf", "DOCUMENT_PDF_UNREADABLE"),
            (pdf_bytes(33), "application/pdf", "a.pdf", "DOCUMENT_PAGE_LIMIT"),
            (b"a" * (MAX_TEXT_BYTES + 1), "text/plain", "a.txt", "DOCUMENT_SIZE_LIMIT"),
            (b"\xff\xfe", "text/plain", "a.txt", "DOCUMENT_ENCODING_UNSUPPORTED"),
            (b"a" * 200001, "text/plain", "a.txt", "DOCUMENT_TEXT_LIMIT"),
        ]:
            with pytest.raises(AppError) as error:
                upload_document(repo, value(chat["chat_id"], data, media_type, name))
            assert error.value.code == expected
        assert repo.list_attachments() == []
        with pytest.raises(AppError) as error:
            prepare_document(
                b"%PDF-" + b"x" * MAX_PDF_BYTES, "application/pdf", "x.pdf", tmp_path / "never"
            )
        assert error.value.code == "DOCUMENT_SIZE_LIMIT"
    finally:
        repo.close()


def test_download_and_metadata_have_safe_types(tmp_path):
    repo, _course, chat = setup(tmp_path)
    try:
        result = upload_document(
            repo, value(chat["chat_id"], b"<script>alert(1)</script>", "text/plain", "code.txt")
        )
        response = document_content(repo, result["attachment_id"])
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"].startswith("sandbox")
        assert attachment_metadata(repo, result["attachment_id"])["filename"] == "code.txt"
    finally:
        repo.close()


def test_http_document_upload_has_session_csrf_and_original_bytes(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    app = create_app(config)
    if not any(
        getattr(route, "path", "") == "/api/v1/document-attachments" for route in app.routes
    ):
        install_document_routes(app)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/v1/document-attachments", json={}).status_code == 403
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        headers = {"X-Learning-CSRF": csrf}
        course = client.post("/api/v1/courses", json={"title": "合成课程"}, headers=headers).json()
        chat = client.post(f"/api/v1/courses/{course['course_id']}/chats", headers=headers).json()
        payload = value(
            chat["chat_id"], b"whole file SENTINEL", "text/plain", "notes.txt", "http"
        ).model_dump()
        assert client.post("/api/v1/document-attachments", json=payload).status_code == 403
        result = client.post("/api/v1/document-attachments", json=payload, headers=headers)
        assert result.status_code == 200, result.text
        detail = result.json()
        assert client.get(detail["download_url"]).content == b"whole file SENTINEL"
        assert (
            client.get(f"/api/v1/attachments/{detail['attachment_id']}/metadata").json() == detail
        )


class CapturingExecution:
    def __init__(self, *, images=True):
        self.images = images
        self.calls = []

    def supports_model_images(self, _model):
        return self.images

    async def validate_choice(self, _model, _thinking):
        return None

    async def prepare(self, chat, submission, generation, epoch):
        return ExecutionBinding(
            chat, submission, generation, epoch, "synthetic-session", submission
        )

    async def start(self, binding, prompt, attachments, **kwargs):
        self.calls.append((prompt, attachments, kwargs))
        binding.accepted = True
        binding.status = "accepted"
        return binding

    def attempts_for(self, _generation):
        return []

    async def observe(self, binding, after=0):
        yield NormalizedEvent(
            "synthetic-body", 1, binding.generation_id, binding.epoch, "text_delta", text="合成完成"
        )
        yield NormalizedEvent(
            "synthetic-stop",
            2,
            binding.generation_id,
            binding.epoch,
            "terminal",
            status="completed",
        )


@pytest.mark.parametrize("kind", ["pdf", "text"])
async def test_formal_service_dispatches_whole_document_parts_and_preserves_receipt(tmp_path, kind):
    repo, _course, chat = setup(tmp_path)
    media_type = "application/pdf" if kind == "pdf" else "text/plain"
    content = pdf_bytes() if kind == "pdf" else b"FULL-TEXT\nEND-DOCUMENT-SENTINEL"
    uploaded = upload_document(
        repo, value(chat["chat_id"], content, media_type, "doc.pdf" if kind == "pdf" else "doc.txt")
    )
    execution = CapturingExecution(images=kind == "pdf")
    service = GenerationService(
        repo, AppConfig(mode="test", data_dir=repo.root), execution=execution
    )
    await service.open()
    payload = {
        "chat_id": chat["chat_id"],
        "submission_id": "document-service",
        "text": "读取整份附件",
        "attachment_ids": [uploaded["attachment_id"]],
        "model": "deepseek-flash" if kind == "pdf" else "deepseek-v4-pro",
        "thinking": "high",
    }
    try:
        receipt = await service.submit(payload)
        await asyncio.wait_for(service.tasks[receipt["generation_id"]], timeout=10)
        assert service.generation_view(receipt["generation_id"])["state"] == "completed"
        prompt, attachments, _kwargs = execution.calls[0]
        if kind == "pdf":
            assert len(attachments) == 2
            assert all(item.media_type == "image/jpeg" for item in attachments)
            assert "页码1至2" in prompt
        else:
            assert attachments == [] and "END-DOCUMENT-SENTINEL" in prompt
        repo.resolve_attachment(uploaded["attachment_id"])["path"].unlink()
        again = await service.submit(payload)
        assert again["generation_id"] == receipt["generation_id"]
        assert again["created"] is False and len(execution.calls) == 1
    finally:
        await service.close()
        repo.close()
