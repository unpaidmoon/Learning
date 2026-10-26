"""Business ownership, input scope and read projection at the native UI seam."""

import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.desktop import DesktopGateway, project_transcript
from learning_app.drafts import DraftInput, read_draft, save_draft
from learning_app.dto import SubmissionInput
from learning_app.errors import AppError
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.storage import StorageError


class NativeFixture:
    def __init__(self, tmp_path):
        self.host = SimpleNamespace(
            workspace=tmp_path / "workspace", base="http://harness.invalid", _token="private-token",
        )
        self.peer = self
        self.starts = []
        self.commands = []

    async def rpc(self, method, request):
        self.commands.append((method, request))
        if method == "session/create":
            return {"sessionId": request["sessionId"]}
        if method == "session/selectModel":
            return {"selected": request}
        if method == "session/rename":
            return {"title": request["title"], "seq": 10}
        return {}

    async def validate_choice(self, model, thinking):
        return None

    async def prepare(self, chat, submission, generation, epoch):
        sid = "learning-" + hashlib.sha256(f"{chat}:{epoch}".encode()).hexdigest()[:32]
        return ExecutionBinding(chat, submission, generation, epoch, sid, submission)

    async def start(self, binding, prompt, attachments, **kwargs):
        self.starts.append((binding, prompt, attachments, kwargs))
        binding.accepted = True
        binding.status = "accepted"
        return binding

    async def observe(self, binding, after=0):
        yield NormalizedEvent("text", 1, binding.generation_id, binding.epoch,
                              "text_delta", text="合成回答")
        yield NormalizedEvent("terminal", 2, binding.generation_id, binding.epoch,
                              "terminal", status="completed")

    def attempts_for(self, generation_id):
        return []


def app_at(tmp_path, execution=None, **kwargs):
    return create_app(AppConfig(
        mode="test", desktop_enabled=True,
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "run", **kwargs,
    ), execution=execution or NativeFixture(tmp_path))


def png(color="blue"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(stream, "PNG")
    return stream.getvalue()


def native_envelope(method, request):
    return {"type": "client-request", "rpcId": "native-rpc", "method": method,
            "payload": {"args": {"request": request}}}


@pytest.mark.parametrize("method", [
    "messageFeedback/list", "messageFeedback/put", "messageFeedback/delete",
])
async def test_native_message_feedback_is_forwarded_only_for_project_sessions(tmp_path, method):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway = app.state.desktop_gateway
        opened = await gateway.create_chat(None, None)
        request = {"sessionId": opened["session_id"]}
        assert await gateway.rpc(method, native_envelope(method, request)) is None
        with pytest.raises(StorageError) as failure:
            foreign = {"sessionId": "unmapped-session"}
            await gateway.rpc(method, native_envelope(method, foreign))
        assert failure.value.code == "CHAT_NOT_FOUND"
        for invalid in ({}, {"sessionId": ""}, {"sessionId": 123}):
            with pytest.raises(AppError) as failure:
                await gateway.rpc(method, native_envelope(method, invalid))
            assert failure.value.code == "DESKTOP_SESSION_INVALID"


def test_native_message_feedback_http_preserves_host_results_and_csrf(tmp_path):
    from unittest.mock import patch

    app = app_at(tmp_path)
    forwarded = []
    host_value = {"ok": False, "error": {"code": "version-conflict", "current": None}}

    def upstream(request):
        if request.url.params.get("token"):
            return httpx.Response(303, headers={"Set-Cookie": "host-cookie=private; Path=/"})
        envelope = json.loads(request.content)
        forwarded.append(envelope)
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": envelope["rpcId"],
            "result": {"ok": True, "value": host_value},
        })

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        sid = client.post("/api/v1/desktop/default/open", headers=csrf).json()["session_id"]
        with patch("learning_app.desktop.httpx.AsyncClient",
                   side_effect=lambda *a, **kw: original_client(*a, **kw, transport=transport)):
            for operation in ("list", "put", "delete"):
                method = "messageFeedback/" + operation
                envelope = native_envelope(method, {
                    "sessionId": sid, "messageId": "assistant-1", "ifVersion": "v1",
                    "rating": "positive",
                })
                url = "/desktop/api/" + method
                assert client.post(url, json=envelope).status_code == 403
                response = client.post(url, headers=csrf, json=envelope)
                assert response.json()["result"] == {"ok": True, "value": host_value}
                assert forwarded[-1] == envelope
                assert "host-cookie" not in response.headers.get("set-cookie", "")
            assert len(forwarded) == 3


async def test_course_initialization_and_repeated_new_keep_unsent_draft(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo = app.state.repository
        repo.create_course("数学", "course")
        gateway = app.state.desktop_gateway
        first, second = await asyncio.gather(
            gateway.open_course("course"), gateway.open_course("course"),
        )
        assert first == second
        assert len(repo.list_chats("course")) == 1
        assert not repo.get_chat(first["chat_id"])["messages"]
        assert gateway.workspaces()["items"][0]["sessionIds"] == [first["session_id"]]
        # Native named blank restoration is idempotent.
        replay = await gateway.create_chat("course", first["session_id"])
        assert replay == first
        assert len(repo.list_chats("course")) == 1
        image = repo.stage_attachment(png(), "image/png", confirmed=True)
        draft = save_draft(repo, first["chat_id"], DraftInput(
            expected_revision=0, text="尚未发送的文字", attachment_ids=[image["attachment_id"]],
        ))
        repeated = await gateway.create_chat("course", None, first["session_id"])
        assert repeated == first
        assert read_draft(repo, first["chat_id"]) == draft
        assert await gateway.create_chat("course", None) == first
        # Once the current chat has a real user message, explicit New Session
        # creates one blank; concurrent repeats from that same origin converge.
        repo.publish_submission(first["chat_id"], "已发送的问题", "sent-first")
        new, repeated = await asyncio.gather(
            gateway.create_chat("course", None, first["session_id"]),
            gateway.create_chat("course", None, first["session_id"]),
        )
        assert repeated == new
        assert new["chat_id"] != first["chat_id"]
        assert len(repo.list_chats("course")) == 2
        assert app.state.service.execution.starts == []


async def test_named_creation_receipt_and_selected_model_survive_gateway_restart(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo = app.state.repository
        repo.create_course("数学", "course")
        gateway = app.state.desktop_gateway
        first = await gateway.create_chat("course", "native-named-create")
        await gateway.rpc("session/selectModel", native_envelope("session/selectModel", {
            "sessionId": first["session_id"], "provider": "deepseek-official",
            "model": "deepseek-v4-pro", "reasoningEffort": "off",
        }))
        restarted = DesktopGateway(app)
        assert await restarted.create_chat("course", "native-named-create") == first
        assert restarted.selected(first["session_id"]) == ("deepseek-v4-pro", "none")
        assert len(repo.list_chats("course")) == 1


async def test_explicit_course_creation_seed_keeps_old_draft_and_does_not_inherit_scope(tmp_path):
    from learning_app.workspaces import set_workspaces

    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo = app.state.repository
        repo.create_course("数学", "math")
        repo.create_course("英语", "english")
        gateway = app.state.desktop_gateway
        old = await gateway.open_course("math")
        set_workspaces(repo, old["chat_id"], ["math", "english"])
        draft = save_draft(repo, old["chat_id"], DraftInput(expected_revision=0, text="旧对话草稿"))
        request = native_envelope("session/create", {"sessionId": "learning-create-explicit", "workspaceId": "math"})
        created = await gateway.rpc("session/create", request)
        replay = await gateway.rpc("session/create", request)
        assert created == replay
        mapped = gateway.read_mapping(created["sessionId"])
        assert mapped["chat_id"] != old["chat_id"]
        assert mapped["course_id"] == "math" and mapped["workspace_ids"] == ["math"]
        assert gateway.read_mapping(old["session_id"])["workspace_ids"] == ["math", "english"]
        assert read_draft(repo, old["chat_id"]) == draft
        assert len(repo.list_chats("math")) == 2
        assert app.state.service.execution.starts == []


async def test_native_prompt_uses_business_submission_id_and_keeps_model_selection(tmp_path):
    execution = NativeFixture(tmp_path)
    app = app_at(tmp_path, execution)
    async with app.router.lifespan_context(app):
        repo = app.state.repository
        repo.create_course("数学", "course")
        gateway = app.state.desktop_gateway
        opened = await gateway.open_course("course")
        await gateway.rpc("session/selectModel", native_envelope("session/selectModel", {
            "sessionId": opened["session_id"], "provider": "deepseek-official",
            "model": "deepseek-v4-pro", "reasoningEffort": "off",
        }))
        request = {"sessionId": opened["session_id"], "requestId": "native-submission",
                   "mode": "queue", "content": [{"type": "text", "text": "只解当前问题"}]}
        assert await gateway.prompt(request) == {"accepted": True}
        receipt = repo.get_submission("native-submission")
        await app.state.service.tasks[receipt["generation_id"]]
        assert await gateway.prompt(request) == {"accepted": True}
        assert len(execution.starts) == 1
        binding, prompt, _, choices = execution.starts[0]
        assert binding.request_id == "native-submission"
        assert prompt == "只解当前问题"
        assert choices["model"] == "deepseek-v4-pro" and choices["thinking"] == "none"
        assert choices["history_context"] is not None
        # Dispatch metadata updates must not erase the user's saved choice.
        assert DesktopGateway(app).selected(opened["session_id"]) == ("deepseek-v4-pro", "none")


async def test_capture_reuse_requires_matching_confirmed_bytes_and_retry_is_stable(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        data = png()
        original = repo.stage_attachment(data, "image/png", confirmed=True)
        part = {"type": "image", "data": base64.b64encode(data).decode(),
                "mediaType": "image/png", "name": f"learning-capture_{original['attachment_id']}.png"}
        assert gateway.image(part, "request", 0) == original["attachment_id"]
        forged = {**part, "data": base64.b64encode(png("red")).decode()}
        with pytest.raises(AppError) as changed:
            gateway.image(forged, "request", 0)
        assert changed.value.code == "CONFIRMED_ATTACHMENT_CHANGED"
        ordinary = {**part, "name": "user.png"}
        first = gateway.image(ordinary, "native-request", 0)
        assert gateway.image(ordinary, "native-request", 0) == first
        with pytest.raises(AppError) as conflict:
            gateway.image({**forged, "name": "user.png"}, "native-request", 0)
        assert conflict.value.code == "SUBMISSION_CONFLICT"
        assert len(repo.list_attachments()) == 2


async def test_read_projection_removes_verified_routing_but_preserves_raw_and_user_text(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("数学", "course")
        opened = await gateway.open_course("course")
        result = repo.publish_submission(opened["chat_id"], "用户自己的问题", "native-request")
        gid = result["generation_id"]
        context = repo.execution_context(gid)
        marker = {"type": "text", "text": f"[[learning-generation:{gid}]]\n"}
        history = {"type": "text", "text": "已完成业务历史，仅作上下文，不重答旧问题：\n"
                   + json.dumps(context["history_context"], ensure_ascii=False)}
        prompt = {"type": "text", "text": "用户自己的问题"}
        event = {"type": "user/message", "data": {"source": {"kind": "user", "rpcId": "native-request"},
                                                   "content": [marker, history, prompt]}}
        projected = project_transcript(event, repo)
        assert projected["data"]["content"] == [prompt]
        assert event["data"]["content"] == [marker, history, prompt]
        forged = {**event, "data": {**event["data"], "source": {"kind": "user", "rpcId": "different"}}}
        assert project_transcript(forged, repo) == forged
        # A user's actual stored prompt starting with our history notice keeps
        # that text; the projected event cannot invent a different user input.
        another = repo.create_chat("course")
        own_text = "已完成业务历史，仅作上下文，不重答旧问题：\n用户输入"
        own = repo.publish_submission(another["chat_id"], own_text, "own-history-notice")
        without_history = {"type": "user/message", "data": {
            "source": {"kind": "user", "rpcId": "own-history-notice"}, "content": [
                {"type": "text", "text": f"[[learning-generation:{own['generation_id']}]]\n"},
                {"type": "text", "text": own_text},
            ],
        }}
        assert project_transcript(without_history, repo)["data"]["content"][0]["text"].endswith("用户输入")
        snapshot = {"header": {"id": opened["session_id"], "cwd": "native-only"},
                    "projections": {"values": {"title": "[[learning-generation:generation_123",
                                                "workingDirectory": "native-only"}}}
        view = gateway.project_view(snapshot)
        assert view["projections"]["values"]["title"] == "用户自己的问题"
        assert view["header"]["cwd"] == gateway.workspaces()["items"][0]["path"]
        assert snapshot["header"]["cwd"] == "native-only"
        update = {"type": "projection", "sessionId": opened["session_id"], "key": "title",
                  "value": "[[learning-generation:generation_123", "seq": 11}
        assert gateway.project_view(update)["value"] == "用户自己的问题"
        control = {"type": "baseline", "value": {"projections": {
            opened["session_id"]: {"asOfSeq": 11, "values": {
                "title": "[[learning-generation:generation_123",
            }},
        }}}
        assert gateway.project_view(control)["value"]["projections"][opened["session_id"]]["values"]["title"] == "用户自己的问题"


def test_native_rpc_capability_csrf_and_host_cookie_do_not_leak(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        gateway = app.state.desktop_gateway

        def upstream(request):
            if request.url.params.get("token"):
                return httpx.Response(303, headers={"Set-Cookie": "host-cookie=private-host-cookie; Path=/"})
            return httpx.Response(200, text='<html><head><script type="module" src="./assets/a.js"></script></head></html>',
                                  headers={"Content-Type": "text/html", "Set-Cookie": "do-not-return=secret"})

        transport = httpx.MockTransport(upstream)
        original_client = httpx.AsyncClient

        class Factory:
            def __call__(self, *args, **kwargs):
                return original_client(*args, **kwargs, transport=transport)

        # Inject only the Host browser carrier, while exercising real HTTP
        # sessions, CSRF middleware and response header filtering.
        from unittest.mock import patch

        with patch("learning_app.desktop.httpx.AsyncClient", Factory()):
            page = client.get("/desktop/")
            assert page.status_code == 200
            assert "host-cookie" not in page.headers.get("set-cookie", "")
            assert "private-token" not in page.text and "private-host-cookie" not in page.text
            assert 'src="learning-bridge.js"' in page.text
            onboarding = 'globalThis.__DSH_MODELS_ONBOARDING__={"credentialOnboarding":false}'
            assert onboarding in page.text
            assert page.text.index(onboarding) < page.text.index('src="library-components.js"')
            assert client.post("/desktop/api/session/prompt", json={}).status_code == 403
            session = client.get("/api/v1/session")
            assert "Path=/;" in session.headers["set-cookie"]
            csrf = {"X-Learning-CSRF": session.json()["csrf_token"]}
            denied = client.post("/desktop/api/credentials/set", headers=csrf,
                                 json=native_envelope("credentials/set", {"key": "x"})).json()
            assert denied["result"]["error"]["code"] == "DESKTOP_METHOD_DENIED"
            mismatch = client.post("/desktop/api/session/prompt", headers=csrf,
                                  json=native_envelope("session/cancel", {}))
            assert mismatch.status_code == 422
            assert gateway.client.cookies.get("host-cookie") == "private-host-cookie"


async def test_native_documents_are_whole_scoped_and_restore_without_preparation(tmp_path):
    from unittest.mock import patch

    from test_document_attachments import pdf_bytes

    from learning_app.documents import expand_document_parts

    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("数学", "course")
        opened = await gateway.open_course("course")
        pdf = pdf_bytes(2)
        uploaded = await gateway.upload_file(opened["session_id"], "整份资料.pdf", pdf, upload_id="whole-pdf")
        aid = uploaded["receiptId"]
        assert uploaded["file"] == {"attachmentId": aid, "name": "整份资料.pdf", "bytes": len(pdf)}
        assert app.state.service.execution.starts == []
        assert gateway.document(aid, opened["chat_id"]) == aid
        images, instruction = expand_document_parts(repo, opened["chat_id"], [repo.resolve_attachment(aid)])
        assert len(images) == 2 and "1至2" in instruction
        with patch("learning_app.documents.prepare_document", side_effect=AssertionError("reprepared")):
            restored = await gateway.upload_file(
                opened["session_id"], "整份资料.pdf", pdf, existing_document=aid,
            )
        assert restored == uploaded
        with pytest.raises(AppError, match="不一致"):
            await gateway.upload_file(opened["session_id"], "整份资料.pdf", pdf + b"x", existing_document=aid)
        other = repo.create_chat("course")
        with pytest.raises(AppError) as foreign:
            gateway.document(aid, other["chat_id"])
        assert foreign.value.code == "DOCUMENT_NOT_IN_CHAT"
        text = "完整的文本\nEND-WHOLE-TEXT"
        uploaded_text = await gateway.upload_file(opened["session_id"], "题目.md", text.encode(), upload_id="whole-md")
        images, instruction = expand_document_parts(repo, opened["chat_id"], [repo.resolve_attachment(uploaded_text["receiptId"])])
        assert not images and text in instruction
        fallback = await gateway.rpc("fileUploads/upload", {
            "payload": {"args": {"agentId": opened["session_id"], "request": {
                "name": "原生回退.txt", "data": base64.b64encode(b"FULL-FALLBACK").decode(),
            }}},
        })
        assert fallback["file"]["name"] == "原生回退.txt"
        assert fallback["file"]["bytes"] == 13


async def test_native_submission_projects_own_attachments_and_hides_context_replay(tmp_path):
    from learning_app.documents import DocumentAttachmentInput, upload_document

    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("数学", "course")
        opened = await gateway.open_course("course")
        image = repo.stage_attachment(png(), "image/png", confirmed=True)
        first = repo.publish_submission(opened["chat_id"], "看图", "image-question", attachment_ids=[image["attachment_id"]])
        assert repo.fail_pending(first["generation_id"], "FIXTURE_FINISHED")
        doc = upload_document(repo, DocumentAttachmentInput(
            chat_id=opened["chat_id"], filename="完整资料.txt", media_type="text/plain",
            data_base64=base64.b64encode(b"FULL-TEXT").decode(), confirmed=True, upload_id="project-text",
        ))
        # Both the old picture and document text may appear in the Host's
        # protected context, but a follow-up row represents this submission.
        following = repo.publish_submission(opened["chat_id"], "继续解释", "follow-question", attachment_ids=[doc["attachment_id"]])
        event = {"type": "user/message", "data": {
            "source": {"kind": "user", "rpcId": "follow-question"}, "content": [
                {"type": "text", "text": f"[[learning-generation:{following['generation_id']}]]\n"},
                {"type": "text", "text": "programmatic protected context"},
                {"type": "image", "attachment": {"attachmentId": "native-context-image"}},
            ],
        }}
        projected = project_transcript(event, repo)["data"]["content"]
        assert projected == [{"type": "text", "text": "继续解释"}, {"type": "file", "attachment": {
            "attachmentId": doc["attachment_id"], "name": "完整资料.txt", "bytes": 9,
        }}]
        assert event["data"]["content"][-1]["type"] == "image"
        first_event = {"type": "user/message", "data": {
            "source": {"kind": "user", "rpcId": "image-question"},
            "content": [{"type": "text", "text": f"[[learning-generation:{first['generation_id']}]]\n"}],
        }}
        native_image = project_transcript(first_event, repo)["data"]["content"][1]
        assert native_image["attachment"]["attachmentId"] == image["attachment_id"]
        readable = gateway.read_image(opened["session_id"], image["attachment_id"])
        assert base64.b64decode(readable["data"]) == png()
        orphan = repo.stage_attachment(png("red"), "image/png", confirmed=True)
        with pytest.raises(AppError) as denied:
            gateway.read_image(opened["session_id"], orphan["attachment_id"])
        assert denied.value.code == "ATTACHMENT_NOT_IN_CHAT"


def test_native_raw_document_upload_requires_csrf_and_never_dispatches(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        repo = app.state.repository
        repo.create_course("数学", "course")
        csrf = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        opened = client.post("/api/v1/desktop/courses/course/open", headers=csrf, json={}).json()
        url = "/desktop/api/session/uploadFileBinary"
        params = {"sessionId": opened["session_id"], "name": "资料.txt"}
        assert client.post(url, params=params, content=b"FULL-TEXT").status_code == 403
        headers = {**csrf, "X-Learning-Upload-Id": "native-raw-document"}
        uploaded = client.post(url, params=params, headers=headers, content=b"FULL-TEXT").json()
        assert uploaded["ok"] is True
        assert uploaded["value"]["receiptId"] == "document_native-raw-document"
        assert client.post(url, params=params, headers=headers, content=b"FULL-TEXT").json() == uploaded
        changed = client.post(url, params=params, headers=headers, content=b"CHANGED").json()
        assert changed["error"]["code"] == "DOCUMENT_UPLOAD_CONFLICT"
        unsupported = client.post(url, params={**params, "name": "video.mp4"}, headers=csrf, content=b"video").json()
        assert unsupported["error"]["code"] == "DESKTOP_ATTACHMENT_UNSUPPORTED"
        assert app.state.service.execution.starts == []


def test_separate_context_is_rejected_for_new_edits_but_legacy_data_is_readable(tmp_path):
    from sqlalchemy import insert

    from learning_app.drafts import drafts
    from learning_app.storage import Repository

    with pytest.raises(ValidationError):
        SubmissionInput(chat_id="chat", submission_id="submission", text="问题", conditions=["x>0"])
    with pytest.raises(ValidationError):
        DraftInput(expected_revision=0, corrections=["改为x=3"])
    repo = Repository(tmp_path / "legacy")
    try:
        repo.create_course("数学", "course")
        chat = repo.create_chat("course")
        with repo.engine.begin() as conn:
            conn.execute(insert(drafts).values(chat_id=chat["chat_id"], revision=1,
                payload={"text": "旧草稿", "attachment_ids": [], "conditions": ["x>0"],
                         "corrections": ["改为x=3"], "references": [], "model": None, "thinking": None},
                updated_at="2026-10-22T00:00:00Z"))
        legacy = read_draft(repo, chat["chat_id"])
        assert legacy["conditions"] == ["x>0"] and legacy["corrections"] == ["改为x=3"]
        original = repo.publish_submission(chat["chat_id"], "旧问题", "legacy-submission",
                                          conditions=["x>0"], corrections=["改为x=3"])
        context = repo.execution_context(original["generation_id"])
        assert "x>0" in context["prompt"] and "改为x=3" in context["prompt"]
        assert "用户已确认的题目条件" not in context["prompt"]
        assert "优先于较早表述" not in context["prompt"]
        assert repo.get_chat(chat["chat_id"])["messages"][0]["conditions"] == ["x>0"]
    finally:
        repo.close()
