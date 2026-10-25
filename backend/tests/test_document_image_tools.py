"""Document image authority and receipt fault windows; synthetic local codec."""

import base64
import hashlib
import io
import json

import pytest
from PIL import Image
from sqlalchemy import func, insert, select, update
from test_course_corpus import index_all
from test_document_regions import document_parent
from test_native_business import call, prepare_chat
from workbench_support import learning_course

from learning_app.execution.boundary import _image_summary
from learning_app.execution.native_tools import ToolRejected
from learning_app.storage import schema as business
from learning_app.workbench import document_image_schema as d
from learning_app.workbench import schema as s
from learning_app.workbench.common import now
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.native_document_images import (
    TOOL,
    NativeDocumentImages,
    rendered_metadata,
)
from learning_app.workbench.source_images import NATIVE_RUNTIME_SHA256


class Codec:
    def __init__(self):
        self.calls = 0
        self.bad_owner = False

    def __call__(self, scope, requests):
        self.calls += 1
        result = []
        for request in requests:
            raw = base64.b64decode(request["data"])
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
            digest = hashlib.sha256(raw).hexdigest()
            result.append(
                {
                    **{key: value for key, value in request.items() if key != "data"},
                    "session_id": scope["session_id"],
                    "run_id": "wrong_run" if self.bad_owner else scope["run_id"],
                    "request_id": scope["request_id"],
                    "epoch": scope["native_epoch"],
                    "model": scope["settings"]["model"],
                    "runtime_sha256": NATIVE_RUNTIME_SHA256,
                    "provider": "deepseek-official",
                    "normalized_sha256": digest,
                    "encoded_sha256": digest,
                    "normalized_ref": {
                        "attachmentId": "sha256:" + digest,
                        "mediaType": "image/png",
                    },
                    "depth": "uchar",
                    "space": "srgb",
                    "encoded_media_type": "image/png",
                    "encoded_bytes": len(raw),
                    "encoded_width": width,
                    "encoded_height": height,
                }
            )
        return result


def setup(tmp_path):
    repo, course, root, source = learning_course(tmp_path)
    pdf, _ = document_parent(repo, course, root)
    index_all(CourseCorpus(repo), course)
    _, binding = prepare_chat(repo, course, source, "image_tools")
    port, codec = NativeBusiness(repo), Codec()
    port.document_images.prepare = codec
    port.document_images.supports_images = lambda _model: True
    port.ensure_chat_scope(binding.generation_id, binding)
    return repo, course, source, pdf, binding, port, codec


def locate(port, binding):
    query = {"concept": "green diagram"}
    hit = port.invoke(call(binding, "search_course_concept", query), query)["results"][0]
    args = {**{key: hit[key] for key in ("handle_id", "source_version")}, "region_id": None}
    location = port.invoke(call(binding, TOOL, args, seq=4), args)
    assert location["status"] == "located" and "image_attachment" not in location
    args["region_id"] = min(location["regions"], key=lambda item: item["region"]["y0"])["region_id"]
    return call(binding, TOOL, args, seq=5), args


def test_manual_text_or_listed_handle_without_actual_text_receipt_cannot_grant_pixels(tmp_path):
    repo, course, source, _, binding, port, codec = setup(tmp_path)
    args = {
        "handle_id": source.snapshot_id,
        "source_version": source.content_hash,
        "region_id": None,
    }
    with pytest.raises(ToolRejected) as error:
        port.invoke(call(binding, TOOL, args), args)
    assert error.value.code == "DOCUMENT_IMAGE_DOCUMENT_GRANT_REQUIRED"
    hit = CourseCorpus(repo).search(course, "green diagram")[0]
    with repo.engine.begin() as conn:
        conn.execute(
            insert(s.native_source_handles).values(
                run_id=binding.generation_id,
                handle_id="listed_only",
                kind="snapshot",
                source_version=hit["snapshot"].content_hash,
                locator={
                    "snapshot_id": hit["snapshot"].snapshot_id,
                    "corpus_document_id": hit["document_id"],
                    "retrieval": True,
                },
                created_at=now(),
            )
        )
    args = {
        "handle_id": "listed_only",
        "source_version": hit["snapshot"].content_hash,
        "region_id": None,
    }
    with pytest.raises(ToolRejected) as error:
        port.invoke(call(binding, TOOL, args, seq=4), args)
    assert error.value.code == "DOCUMENT_IMAGE_TEXT_NOT_READ" and codec.calls == 0
    repo.close()


@pytest.mark.parametrize("change", ["region", "version", "bbox"])
def test_opaque_region_and_version_do_not_accept_model_coordinates_or_another_handle(
    tmp_path, change
):
    repo, _, _, _, binding, port, codec = setup(tmp_path)
    _, args = locate(port, binding)
    bad = {**args}
    if change == "region":
        bad["region_id"] = "other_run_region"
    elif change == "version":
        bad["source_version"] = "0" * 64
    else:
        bad["bbox"] = [0, 0, 1, 1]
    with pytest.raises(ToolRejected):
        port.invoke(call(binding, TOOL, bad, seq=6), bad)
    assert codec.calls == 0
    repo.close()


def test_changed_original_cannot_be_replaced_by_newer_pixels(tmp_path):
    repo, _, _, pdf, binding, port, codec = setup(tmp_path)
    native, args = locate(port, binding)
    pdf.write_bytes(pdf.read_bytes() + b"\n% changed owned fixture\n")
    with pytest.raises(ToolRejected) as error:
        port.invoke(native, args)
    assert error.value.code == "SOURCE_VERSION_UNAVAILABLE" and codec.calls == 0
    repo.close()


def test_encoding_owner_mismatch_is_rejected_and_never_becomes_an_allowance(tmp_path):
    repo, _, _, _, binding, port, codec = setup(tmp_path)
    native, args = locate(port, binding)
    codec.bad_owner = True
    with pytest.raises(ToolRejected) as error:
        port.invoke(native, args)
    assert error.value.code == "DOCUMENT_IMAGE_PREPARATION_SCOPE"
    result = port.reconcile_effect(native)
    assert result["error_code"] == "DOCUMENT_IMAGE_PREPARATION_SCOPE"
    assert port.invoke(native, args) == result and codec.calls == 1
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(d.image_allowances)).scalar_one() == 0
    repo.close()


def test_committed_unknown_reply_recovers_exact_receipt_without_encoding_or_source_replay(tmp_path):
    repo, _, _, pdf, binding, port, codec = setup(tmp_path)
    native, args = locate(port, binding)

    def failure(name, detail):
        if name == "document_image_after_commit":
            raise RuntimeError("owned lost reply")

    repo.fault_hook = failure
    with pytest.raises(RuntimeError, match="owned lost reply"):
        port.invoke(native, args)
    repo.fault_hook = None
    pdf.unlink()
    recovered = port.reconcile_effect(native)
    assert recovered["source"]["reference"]["kind"] == "document_region_image"
    assert port.reconcile_effect(native) == recovered and codec.calls == 1
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(d.image_reads)).scalar_one() == 1
    repo.close()


def test_local_encoding_interruption_has_no_image_result_and_no_automatic_retry(tmp_path):
    repo, _, _, _, binding, port, codec = setup(tmp_path)
    native, args = locate(port, binding)

    def failure(name, detail):
        if name == "document_image_after_encoding":
            raise RuntimeError("owned encoding interruption")

    repo.fault_hook = failure
    with pytest.raises(RuntimeError, match="owned encoding interruption"):
        port.invoke(native, args)
    repo.fault_hook = None
    recovered = port.reconcile_effect(native)
    assert recovered["status"] == "not_ready" and "image_attachment" not in recovered
    assert port.invoke(native, args) == recovered and codec.calls == 1
    repo.close()


@pytest.mark.parametrize("change", ["none", "pixels", "call", "metadata", "duplicate"])
def test_actual_tool_image_requires_exact_call_metadata_and_encoding(tmp_path, change):
    from learning_app.workbench.sources import SourceStore

    repo, _, _, _, binding, port, _ = setup(tmp_path)
    native, args = locate(port, binding)
    result = port.invoke(native, args)
    raw = SourceStore(repo).image_path(result["encoding_proof"]["source_sha256"]).read_bytes()
    picture = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }
    metadata = rendered_metadata(result)
    if change == "pixels":
        picture["source"]["data"] = base64.b64encode(b"different pixels").decode("ascii")
    if change == "metadata":
        metadata["source"] = {**metadata["source"], "snapshot_id": "different_source"}
    value = {
        "type": "tool_result",
        "tool_use_id": "different_call" if change == "call" else native.call_id,
        "content": [{"type": "text", "text": json.dumps(metadata)}, picture]
        + ([picture] if change == "duplicate" else []),
    }
    request = {"messages": [{"role": "user", "content": [value]}]}
    if change == "none":
        port.validate_model_images(binding.generation_id, request, _image_summary)
    else:
        with pytest.raises(ToolRejected) as error:
            port.validate_model_images(binding.generation_id, request, _image_summary)
        assert error.value.code == "DOCUMENT_IMAGE_WIRE_SCOPE_DENIED"
    repo.close()


@pytest.mark.parametrize(
    "context_kind",
    ["valid", "missing", "user_role", "wrong_message", "foreign_chat", "current_generation"],
)
def test_history_freezes_exact_context_message_generation_and_chat_only(tmp_path, context_kind):
    repo, course, _, _, binding, port, _ = setup(tmp_path)
    native, args = locate(port, binding)
    port.invoke(native, args)
    current = repo.get_generation(binding.generation_id)
    with repo.engine.begin() as conn:
        conn.execute(
            update(business.generations)
            .where(
                business.generations.c.generation_id == binding.generation_id,
            )
            .values(state="completed")
        )
    message = {
        "role": "assistant",
        "generation_id": binding.generation_id,
        "message_id": current["assistant_message_id"],
    }
    messages = [message]
    if context_kind == "missing":
        messages = []
    elif context_kind == "user_role":
        message["role"] = "user"
    elif context_kind == "wrong_message":
        message["message_id"] = "not_the_assistant_message"
    chat = {
        "chat_id": "foreign_chat" if context_kind == "foreign_chat" else "native_chat",
        "course_id": course,
    }
    run = {
        "generation_id": binding.generation_id
        if context_kind == "current_generation"
        else "next_generation"
    }
    with repo.engine.connect() as conn:
        frozen = NativeDocumentImages.freeze_history(conn, run, chat, {"messages": messages})
    assert len(frozen) == (1 if context_kind == "valid" else 0)
    if frozen:
        assert frozen[0]["effect_id"] and frozen[0]["result_hash"] and frozen[0]["source_sha256"]
    repo.close()


def test_cached_picture_remains_viewable_after_original_disappears_without_new_read_permission(
    tmp_path,
):
    from fastapi.testclient import TestClient

    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.workbench.sources import SourceStore

    repo, course, _, pdf, binding, port, _ = setup(tmp_path)
    native, args = locate(port, binding)
    result = port.invoke(native, args)
    snapshot_id = result["source"]["snapshot_id"]
    raw = SourceStore(repo).image_path(result["encoding_proof"]["source_sha256"]).read_bytes()
    pdf.unlink()
    with TestClient(
        create_app(
            AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run"),
            repository=repo,
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        client.get("/api/v1/session")
        response = client.get(f"/api/v1/courses/{course}/learning/selections/{snapshot_id}/image")
        assert response.status_code == 200 and response.content == raw
    # A different read is still checked against the original version; viewing
    # a stored picture does not turn it into permission for new retrieval.
    with pytest.raises(ToolRejected) as error:
        port.invoke(call(binding, TOOL, args, seq=9), args)
    assert error.value.code == "SOURCE_VERSION_UNAVAILABLE"
    repo.close()
