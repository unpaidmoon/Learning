"""Actual business storage and frozen native authority; synthetic files only."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_desktop import app_at
from test_document_attachments import pdf_bytes
from test_native_business import call

from learning_app import chat_attachments as files
from learning_app.documents import attachment_metadata, expand_document_parts, requires_images
from learning_app.drafts import DraftInput
from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.execution.types import ExecutionBinding
from learning_app.storage import Repository
from learning_app.workbench.native_business import NativeBusiness


def stored(repo, directory, name, data, identity="upload", chat="chat"):
    source = directory / (identity + ".upload")
    source.write_bytes(data)
    return files.upload_file(repo, chat, name, source, identity)


def fixture(tmp_path):
    repo = Repository(tmp_path / "data")
    repo.create_course("合成课程", "course")
    repo.create_chat("course", "chat")
    return repo


def scope(repo, attachment_ids):
    result = repo.publish_submission("chat", "读取我发送的附件", "input",
                                     attachment_ids=attachment_ids)
    binding = ExecutionBinding("chat", "input", result["generation_id"], 1,
                               "native-chat", "input", native_floor=0, native_turn=1)
    active = repo.bind_execution("chat", binding.session_id, adapter_metadata=binding.to_dict())
    assert repo.claim_dispatch(binding.generation_id, active["binding_id"], 1,
                               request_id=binding.request_id, adapter_metadata=binding.to_dict())
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    return port, binding


def arguments(metadata, *, offset=0, page=None, mode="text"):
    return {"attachment_id": metadata["attachment_id"], "sha256": metadata["sha256"],
            "offset": offset, "page": page, "mode": mode}


def test_verbatim_files_survive_reopen_and_do_not_expand_into_model_input(tmp_path):
    repo = fixture(tmp_path)
    # Larger than the previous text intake limit; invalid as UTF-8 by design.
    data = b"\x00\xff" * (1024 * 1024) + b"PRIVATE-NOT-IN-PROMPT"
    metadata = stored(repo, tmp_path, "binary.unknown", data)
    pdf = stored(repo, tmp_path, "long.pdf", pdf_bytes(33), "pdf")
    originals = [repo.resolve_attachment(value["attachment_id"]) for value in (metadata, pdf)]
    images, prompt = expand_document_parts(repo, "chat", originals)
    assert images == [] and "PRIVATE-NOT-IN-PROMPT" not in prompt
    assert "read_chat_attachment" in prompt and metadata["sha256"] in prompt
    assert not requires_images(repo, [pdf["attachment_id"]])
    assert metadata["kind"] == "file" and metadata["page_count"] is None
    assert len(repo.list_attachments()) == 2  # No pages prepared on upload.
    repo.close()
    repo = Repository(tmp_path / "data")
    assert attachment_metadata(repo, metadata["attachment_id"]) == metadata
    assert repo.resolve_attachment(metadata["attachment_id"])["path"].read_bytes() == data
    assert stored(repo, tmp_path, "binary.unknown", data) == metadata
    with pytest.raises(AppError, match="内容已变化"):
        stored(repo, tmp_path, "changed.unknown", data)
    repo.create_chat("course", "other")
    with pytest.raises(AppError, match="内容已变化"):
        stored(repo, tmp_path, "binary.unknown", data, chat="other")
    repo.close()


def test_native_read_is_paginated_frozen_and_reconcilable(tmp_path):
    repo = fixture(tmp_path)
    text = "第一段\n" * 6000 + "END-OF-ATTACHMENT"
    metadata = stored(repo, tmp_path, "code.py", text.encode())
    unsubmitted = stored(repo, tmp_path, "not-sent.txt", b"NOT-SUBMITTED", "draft")
    port, binding = scope(repo, [metadata["attachment_id"]])
    args = arguments(metadata)
    native = call(binding, files.TOOL, args)
    first = port.invoke(native, args)
    assert first["text"] == text[:files.READ_CHARACTERS] and first["has_more"]
    assert port.invoke(native, args) == first == port.reconcile_effect(native)
    following = arguments(metadata, offset=first["next_offset"])
    second = port.invoke(replace(call(binding, files.TOOL, following), call_id="continued"), following)
    assert "END-OF-ATTACHMENT" in second["text"] and not second["has_more"]
    assert first["text"] + second["text"] == text
    denied = arguments(unsubmitted)
    with pytest.raises(ToolRejected, match="CHAT_ATTACHMENT_SCOPE_DENIED"):
        port.invoke(call(binding, files.TOOL, denied), denied)
    wrong = {**args, "sha256": "0" * 64}
    with pytest.raises(ToolRejected, match="CHAT_FILE_VERSION_MISMATCH"):
        port.invoke(call(binding, files.TOOL, wrong), wrong)
    assert NativeBusiness(repo).restore_scope(binding.generation_id, binding)["allowed_tools"] == port.restore_scope(binding.generation_id, binding)["allowed_tools"]
    repo.request_cancel(binding.generation_id)
    with pytest.raises(ToolRejected, match="NATIVE_RUN_CANCEL_REQUESTED"):
        port.invoke(native, args)
    repo.close()


def test_pdf_read_is_on_demand_and_uses_selected_page_only(tmp_path):
    repo = fixture(tmp_path)
    metadata = stored(repo, tmp_path, "pages.pdf", pdf_bytes(33))
    port, binding = scope(repo, [metadata["attachment_id"]])
    info_args = arguments(metadata)
    info = port.invoke(call(binding, files.TOOL, info_args), info_args)
    assert info["page_count"] == 33 and not info["content_read"]
    page_args = arguments(metadata, page=33)
    result = port.invoke(call(binding, files.TOOL, page_args), page_args)
    assert result["page_count"] == 33 and result["page"] == 33 and result["content_read"]
    requests = []
    def prepare(current_scope, grants):
        from learning_app.workbench.source_images import NATIVE_RUNTIME_SHA256
        requests.extend(grants)
        proof = {k: v for k, v in grants[0].items() if k != "data"}
        proof.update({k: current_scope[k] for k in ("session_id", "run_id", "request_id")})
        proof["epoch"] = current_scope["native_epoch"]
        proof.update(model=current_scope["settings"]["model"], runtime_sha256=NATIVE_RUNTIME_SHA256,
                     provider="deepseek-official", normalized_sha256=grants[0]["source_sha256"],
                     encoded_sha256=grants[0]["source_sha256"], encoded_media_type="image/png",
                     encoded_bytes=100, encoded_width=100, encoded_height=100,
                     normalized_ref={"attachmentId": "sha256:" + grants[0]["source_sha256"]})
        return [proof]
    port.document_images.prepare = prepare
    port.document_images.supports_images = lambda _: True
    image_args = arguments(metadata, page=33, mode="image")
    result = port.invoke(call(binding, files.TOOL, image_args), image_args)
    assert len(requests) == 1 and requests[0]["page_index"] == 32
    assert result["image_attachment"] and result["render_metadata"]["page"] == 33
    assert len(repo.list_attachments()) == 1
    repo.close()


def test_binary_and_audio_uploads_do_not_claim_reading_or_trigger_processing(tmp_path):
    repo = fixture(tmp_path)
    metadata = stored(repo, tmp_path, "recording.mp4", b"SYNTHETIC-NO-VIDEO")
    port, binding = scope(repo, [metadata["attachment_id"]])
    args = arguments(metadata)
    with pytest.raises(ToolRejected, match="CHAT_FILE_READER_UNAVAILABLE"):
        port.invoke(call(binding, files.TOOL, args), args)
    repo.close()


def test_office_reads_only_selected_text_part_and_never_extracts_paths(tmp_path):
    repo = fixture(tmp_path)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", '<root><p><t>SLIDE-ONE</t></p></root>')
        archive.writestr("ppt/slides/slide2.xml", '<root><p><t>SLIDE-TWO</t></p></root>')
        archive.writestr("../../escape.txt", "MUST-NOT-EXTRACT")
    metadata = stored(repo, tmp_path, "slides.pptx", stream.getvalue())
    port, binding = scope(repo, [metadata["attachment_id"]])
    args = arguments(metadata, page=2)
    result = port.invoke(call(binding, files.TOOL, args), args)
    assert result["text"] == "SLIDE-TWO" and result["part_count"] == 2
    assert not (tmp_path.parent / "escape.txt").exists()
    repo.close()


def test_raw_http_upload_csrf_download_and_more_than_eight_draft_files(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        opened = client.post("/api/v1/desktop/default/open", headers=csrf).json()
        url = "/desktop/api/session/uploadFileBinary"
        params = {"sessionId": opened["session_id"], "name": "user.html"}
        data = b"<script>untrusted()</script>"
        assert client.post(url, params=params, content=data).status_code == 403
        response = client.post(url, params=params, content=data,
                               headers={**csrf, "X-Learning-Upload-Id": "http-file"}).json()
        assert response["ok"]
        aid = response["value"]["receiptId"]
        download = client.get(f"/api/v1/attachments/{aid}")
        assert download.content == data and download.headers["content-security-policy"].startswith("sandbox")
        ids = [aid]
        for index in range(9):
            metadata = stored(app.state.repository, tmp_path, f"{index}.txt", b"text", f"file-{index}", opened["chat_id"])
            ids.append(metadata["attachment_id"])
        # No generic-file count limit in the app draft schema.
        value = DraftInput(expected_revision=0, text="draft", attachment_ids=ids)
        assert len(value.attachment_ids) == 10
        assert app.state.service.execution.starts == []
        metadata = attachment_metadata(app.state.repository, aid)
        assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
        assert json.dumps(response).find("script") == -1


def test_native_file_intake_exceeds_old_twenty_mib_limit_without_preparation(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        opened = client.post("/api/v1/desktop/default/open", headers=csrf).json()
        chunk = b"\x00SYNTHETIC" * 8192
        chunks = 300
        result = client.post("/desktop/api/session/uploadFileBinary",
                             params={"sessionId": opened["session_id"], "name": "large.bin"},
                             headers={**csrf, "X-Learning-Upload-Id": "large-synthetic"},
                             content=(chunk for _ in range(chunks))).json()
        assert len(chunk) * chunks > 20 * 1024 * 1024
        assert result["ok"] and result["value"]["file"]["bytes"] == len(chunk) * chunks
        assert len(app.state.repository.list_attachments()) == 1
        assert app.state.service.execution.starts == []


def test_internal_file_attachment_keeps_exact_bytes_and_never_opens_video(tmp_path):
    from learning_app.course_files import CourseFileStore

    app = app_at(tmp_path)
    folder = tmp_path / "course-files"
    folder.mkdir()
    data = b"SYNTHETIC-LOCAL-VIDEO-BYTES"
    (folder / "selected.mp4").write_bytes(data)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        store = CourseFileStore(app.state.repository)
        course = store.bind(None, str(folder))
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        url = f"/api/v1/courses/{course['course_id']}/files/attachment-source"
        assert client.post(url, json={"path": "selected.mp4"}).status_code == 403
        response = client.post(url, headers=csrf, json={"path": "selected.mp4"})
        assert response.status_code == 200, response.text
        source = response.json()
        assert source["video"] is None and source["content_url"].endswith("&download=1")
        copied = client.get(source["content_url"])
        assert copied.content == data and copied.headers["content-disposition"] == "attachment"
        assert copied.headers["content-type"] == "application/octet-stream"
        assert client.post(url, headers=csrf, json={"path": "../outside.txt"}).status_code >= 400
        (folder / "selected.mp4").write_bytes(data + b"changed")
        assert client.get(source["content_url"]).status_code >= 400


def test_native_gif_image_is_confirmed_and_validated(tmp_path):
    from PIL import Image

    app = app_at(tmp_path)
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(stream, "GIF")
    import base64

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post("/api/v1/attachments", headers=csrf, json={
            "data_base64": base64.b64encode(stream.getvalue()).decode(),
            "media_type": "image/gif", "confirmed": True,
        })
        assert response.status_code == 200, response.text
        assert response.json()["media_type"] == "image/gif"
