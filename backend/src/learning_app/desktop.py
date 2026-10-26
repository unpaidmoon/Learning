"""Pinned native client carrier; project submissions remain the sole write path.

The renderer receives application cookies only. Harness launch tokens and
browser/operator cookies live in this module and the execution implementation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from learning_app.chat_presentation import ChatPresentation
from learning_app.desktop_avatar import (
    picture_reference,
    project_account_picture,
    read_account_picture,
)
from learning_app.desktop_theme import DesktopThemePreferences
from learning_app.document_processing import MAX_PDF_BYTES
from learning_app.documents import (
    DocumentAttachmentInput,
    attachment_metadata,
    upload_document,
    validate_document_scope,
)
from learning_app.dto import SubmissionInput
from learning_app.errors import AppError
from learning_app.execution.types import ExecutionError
from learning_app.imported_history import ImportedHistory
from learning_app.security import COOKIE_NAME
from learning_app.service import TERMINAL
from learning_app.storage import StorageError
from learning_app.workbench.models import (
    DesktopLearningTaskInput,
    ModelSettings,
    TaskCreate,
    TaskUnitInput,
    TaskView,
)
from learning_app.workbench.requested_learning import NativeLearningTaskLink, admitted_task_links
from learning_app.workbench.task_scope_models import TaskAdmission, TaskIntent
from learning_app.workbench.tasks import TaskStore
from learning_app.workbench.translation_reuse import TranslationCacheEntry, TranslationPolicyView
from learning_app.workspaces import (
    ensure_unclassified,
    read_workspaces,
    set_workspaces,
)

_READ_METHODS = frozenset(
    {
        "session/list",
        "session/search",
        "session/page",
        "session/projections",
        "session/attachment",
        "session/modelCatalog",
        "session/initializeDefaultModel",
        "skills/list",
        "settings/describe",
        "account/getState",
        "account/getProfile",
        "account/getBalance",
        "account/hasRunningAccountTasks",
        "account/getUnnotifiedBonuses",
        "remoteAccess/explain",
        "commands/list",
        "agentPresets/list",
        "agentPresets/read",
        "llm/listProviders",
        "llm/listConfigurableProviders",
        "credentials/describe",
        "permissionPresets/catalog",
        "pluginInventory/list",
    }
)
_STREAM_METHODS = frozenset(
    {
        "$events",
        "session/follow",
        "session/control",
        "workspace/follow",
        "remoteAccess/follow",
        "account/watch",
        "account/watchExpiry",
    }
)
_MESSAGE_FEEDBACK_METHODS = frozenset(
    {"messageFeedback/list", "messageFeedback/put", "messageFeedback/delete"}
)
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}
_CAPTURE_NAME = re.compile(r"^learning-capture_(attachment_[A-Za-z0-9_-]+)\.png$")
_STATIC_ROOTS = ("assets/", "plugins/", "fonts/")
_STATIC_NAMES = frozenset(
    {"", "index.html", "favicon.svg", "favicon-dark.svg", "manifest.webmanifest"}
)
_CLIENT_NAMES = frozenset(
    {
        "learning-bridge.js",
        "learning.css",
        "learning-player.js",
        "learning-player.css",
        "native-workspace.js",
        "course-library.js",
        "course-library.css",
        "draft-bridge.js",
        "answer-quotes.js",
    }
)
_BUILT_CLIENT_NAMES = frozenset(
    {
        "library-components.js",
        "library-components.css",
        "video-components.js",
        "video-components.css",
    }
)
_NO_VALUE = object()
_GENERATION_MARKER = re.compile(r"^\[\[learning-generation:([A-Za-z0-9_-]+)\]\]\n$")
logger = logging.getLogger(__name__)


def project_transcript(value, repository):
    """Remove only verified project routing blocks from the native read view.

    Raw Host events, model inputs, and business ingestion retain their bytes.
    A user typing a marker-like string does not acquire a matching submission
    identity, so it is left intact.
    """
    if isinstance(value, list):
        return [project_transcript(item, repository) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: project_transcript(item, repository) for key, item in value.items()}
    if result.get("type") != "user/message":
        return result
    data = result.get("data", {})
    content = data.get("content", [])
    source = data.get("source", {})
    if not content or not isinstance(content[0], dict) or source.get("kind") != "user":
        return result
    first = content[0]
    match = (
        _GENERATION_MARKER.fullmatch(first.get("text", "")) if first.get("type") == "text" else None
    )
    if not match:
        return result
    try:
        generation = repository.get_generation(match[1])
        if generation["submission_id"] != source.get("rpcId"):
            return result
    except StorageError:
        return result
    current = next(
        (
            message
            for message in generation["context_snapshot"]["messages"]
            if message["message_id"] == generation["user_message_id"]
        ),
        None,
    )
    if current is None:
        return result
    # The model receives the complete protected context; the user-facing row
    # reflects this actual submission, not programmatic history/image replay
    # or the PDF expansion performed at the dispatch seam.
    rendered = [{"type": "text", "text": current["text"]}] if current["text"] else []
    for aid in current.get("attachment_ids", ()):
        item = attachment_metadata(repository, aid)
        if item["kind"] == "image":
            rendered.append({"type": "image", "attachment": _image_ref(repository, aid)})
        else:
            rendered.append({"type": "file", "attachment": _file_ref(item)})
    result["data"] = {**data, "content": rendered}
    return result


def _file_ref(metadata: dict) -> dict:
    return {
        "attachmentId": metadata["attachment_id"],
        "name": metadata["filename"],
        "bytes": metadata["size_bytes"],
    }


def _image_ref(repository, attachment_id: str) -> dict:
    from PIL import Image

    image = repository.resolve_attachment(attachment_id)
    if image["media_type"] not in _IMAGE_TYPES or not image["confirmed"]:
        raise AppError("ATTACHMENT_NOT_CONFIRMED", "图片尚未确认", 409)
    with Image.open(image["path"]) as source:
        width, height = source.size
    return {
        "attachmentId": attachment_id,
        "mediaType": image["media_type"],
        "bytes": image["byte_length"],
        "width": width,
        "height": height,
        "name": "截图",
    }


class DesktopSessionResponse(BaseModel):
    chat_id: str
    course_id: str
    session_id: str
    epoch: int
    read_only: bool = False
    history_available: bool = False
    has_unresolved_history: bool = False
    workspace_ids: list[str] = []


class DesktopWorkspaceInput(BaseModel):
    course_ids: list[str]


def _method(value: str) -> str:
    # The pinned artifact uses slashes; the source projection can use dots.
    if not isinstance(value, str) or len(value) > 200:
        raise AppError("DESKTOP_RPC_INVALID", "原生界面请求方法无效", 422)
    return value.replace(".", "/", 1) if "/" not in value else value


def _arguments(envelope: dict) -> dict:
    payload = envelope.get("payload", {})
    if not isinstance(payload, dict):
        raise AppError("DESKTOP_RPC_INVALID", "原生界面请求无效", 422)
    args = payload.get("args", payload)
    if not isinstance(args, dict):
        raise AppError("DESKTOP_RPC_INVALID", "原生界面请求参数无效", 422)
    request = args.get("request", args)
    if not isinstance(request, dict):
        raise AppError("DESKTOP_RPC_INVALID", "原生界面请求参数无效", 422)
    return request


def _scoped_session(envelope: dict, request: dict):
    scope = envelope.get("payload", {}).get("scope", {})
    if not isinstance(scope, dict):
        raise AppError("DESKTOP_RPC_INVALID", "原生对话范围无效", 422)
    return request.get("sessionId") or request.get("agentId") or scope.get("agentId") or scope.get("sessionId")


class DesktopGateway:
    def __init__(self, app: FastAPI):
        self.app = app
        self.client: httpx.AsyncClient | None = None
        self._host = None
        self._lock = asyncio.Lock()
        self.course_locks = defaultdict(asyncio.Lock)
        self._history = None
        self.theme = DesktopThemePreferences(app.state.config.data_dir)
        self._pending_renames = set()

    @property
    def service(self):
        if not self.app.state.config.desktop_enabled:
            raise AppError("DESKTOP_DISABLED", "原生桌面入口未启用", 404)
        if not self.app.state.ready or not self.app.state.service.execution_ready:
            raise AppError("EXECUTION_UNAVAILABLE", "Harness 尚未连接", 503)
        return self.app.state.service

    async def browser(self) -> httpx.AsyncClient:
        host = self.service.execution.host
        async with self._lock:
            if self.client is None or self._host is not host:
                if self.client is not None:
                    await self.client.aclose()
                client = httpx.AsyncClient(base_url=host.base, trust_env=False, timeout=30)
                # Browser cookies and machine /connect operator cookies have
                # distinct signed versions. Never return either to the page.
                response = await client.get("/", params={"token": host._token})
                if response.status_code != 303:
                    await client.aclose()
                    raise AppError("DESKTOP_HOST_AUTH_FAILED", "原生界面连接失败", 503)
                self.client, self._host = client, host
            return self.client

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def native_read(self, method, envelope):
        # The machine Peer deliberately has interactions=none and cannot list
        # browser Sessions. Reuse the existing private browser capability.
        client = await self.browser()
        response = await client.post("/api/" + method, json=envelope)
        value = response.json().get("result", {})
        if response.status_code != 200 or not value.get("ok"):
            raise AppError("DESKTOP_NATIVE_READ_UNAVAILABLE", "原生对话列表暂不可读，请重新连接", 503)
        return value.get("value", _NO_VALUE)

    @property
    def presentation(self):
        return ChatPresentation(self.service.repository)

    async def prepare_title(self, generation, binding):
        boundary = getattr(self.service.execution, "boundary", None)
        configure = getattr(self.service.execution.host, "configure_title", None)
        if boundary is None or configure is None:
            return
        chat = self.service.repository.get_chat(binding.conversation_id)
        if len([m for m in chat["messages"] if m["role"] == "user"]) != 1:
            return
        identity = self.presentation.reserve_title(binding.conversation_id)
        if not identity:
            return
        try:
            scope = boundary.register_title(identity, binding.session_id,
                generation.get("text") or "图片提问", generation["model"], generation["thinking"])
            await configure(scope)
            self.presentation.set(binding.conversation_id, title_receipt={"state": "authorized", "request_id": identity})
        except (ValueError, ExecutionError, httpx.HTTPError):
            self.presentation.set(binding.conversation_id, title_receipt={"state": "unavailable", "request_id": identity})
            logger.warning("Native title preparation unavailable; the main submission continues")

    @property
    def history(self):
        if self._history is None:
            self._history = ImportedHistory(self.service, self.history_content)
        return self._history

    def history_content(self, message):
        content = [{"type": "text", "text": message["text"]}] if message["text"] else []
        for aid in message["attachment_ids"]:
            metadata = attachment_metadata(self.service.repository, aid)
            content.append({"type": "image", "attachment": _image_ref(self.service.repository, aid)} if metadata["kind"] == "image" else {"type": "file", "attachment": _file_ref(metadata)})
        return content

    def read_mapping(self, session_id):
        mapped = self.history.mapping(session_id)
        if mapped is None:
            return self.mapping(session_id)
        chat = self.service.repository.get_chat(mapped["chat_id"])
        with self.service.repository.engine.connect() as conn:
            return {**mapped, "workspace_ids": read_workspaces(conn, chat)}

    def mapping(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id):
            raise AppError("DESKTOP_SESSION_INVALID", "原生对话身份无效", 422)
        if session_id.startswith("learning-history-"):
            raise AppError("IMPORTED_EXECUTION_READ_ONLY", "此处是已保存的导入历史，请使用继续提问或新建对话")
        repository = self.service.repository
        self.service.imported.require_fresh("session", session_id)
        chat_id = repository.chat_for_session(session_id)
        chat = repository.get_chat(chat_id)
        binding = repository.current_binding(chat_id)
        if not binding or binding["session_id"] != session_id:
            raise AppError("DESKTOP_SESSION_STALE", "此对话执行身份已更新，请重新打开", 409)
        with repository.engine.connect() as conn:
            workspace_ids = read_workspaces(conn, chat)
        return {
            "chat_id": chat_id,
            "course_id": chat["course_id"],
            "session_id": session_id,
            "epoch": binding["epoch"],
            "history_available": chat_id in self.history.chats,
            "workspace_ids": workspace_ids,
        }

    async def open_chat(self, chat_id: str, *, history_view=True) -> dict:
        service = self.service
        async with service.locks[chat_id]:
            chat = service.repository.get_chat(chat_id)
            current = service.repository.current_binding(chat_id)
            if history_view and chat_id in self.history.chats and (not current or current["state"] in {"stale", "retired"} or service.imported.contains("session", current["session_id"])):
                return self.read_mapping(self.history.chats[chat_id])
            if (
                current
                and current["state"] not in {"stale", "retired"}
                and not service.imported.contains("session", current["session_id"])
            ):
                return self.mapping(current["session_id"])
            if (
                current
                and service.imported.contains("session", current["session_id"])
                and current["active_generation_id"]
            ):
                raise AppError(
                    "IMPORTED_EXECUTION_READ_ONLY",
                    "旧执行结果在本机无法核对，历史仍可查看；请新建对话继续学习",
                )
            epoch = current["epoch"] + 1 if current else 1
            sid = "learning-" + hashlib.sha256(f"{chat_id}:{epoch}".encode()).hexdigest()[:32]
            # Empty Session creation is local. It never calls the model.
            value = await service.execution.peer.rpc(
                "session/create", {"cwd": str(service.execution.host.workspace), "sessionId": sid}
            )
            actual = value["sessionId"]
            service.repository.bind_execution(
                chat_id,
                actual,
                adapter_metadata={
                    "history_revision": chat.get("history_revision", 0),
                    "desktop_pending_history": True,
                },
            )
            return self.mapping(actual)

    async def open_course(self, course_id: str) -> dict:
        async with self.course_locks[course_id]:
            repository = self.service.repository
            chats = repository.list_chats(course_id)
            preferences = self.presentation.all()
            chats = [chat for chat in chats if not preferences.get(chat["chat_id"], {}).get("archived")]
            chat = repository.get_chat(chats[-1]["chat_id"]) if chats else None
            if chat is None:
                chat = repository.create_chat(course_id)
            return await self.open_chat(chat["chat_id"])

    async def create_chat(
        self,
        course_id: str | None,
        requested_session: str | None,
        current_session: str | None = None,
    ) -> dict:
        course_id = course_id or ensure_unclassified(self.service.repository)
        async with self.course_locks[course_id]:
            repository = self.service.repository
            preferences = self.presentation.all()
            if not current_session:
                existing = repository.list_chats(course_id)
                existing = [chat for chat in existing if not preferences.get(chat["chat_id"], {}).get("archived")]
                if existing and existing[-1]["chat_id"] in self.history.chats:
                    # Native first-use/blank initialization must not silently
                    # replace a restored history with a new business chat.
                    return await self.open_chat(existing[-1]["chat_id"])
            if current_session:
                mapped = self.read_mapping(current_session)
                if mapped["course_id"] != course_id or preferences.get(mapped["chat_id"], {}).get("archived"):
                    current_session = None
            if current_session:
                mapped = self.read_mapping(current_session)
                current = repository.get_chat(mapped["chat_id"])
                if not current["messages"] and not current["generations"]:
                    # Draft state is owned by this same business/native identity.
                    # A repeated click neither clears it nor switches the input.
                    return mapped
                for candidate in reversed(repository.list_chats(course_id)):
                    if preferences.get(candidate["chat_id"], {}).get("archived"):
                        continue
                    candidate = repository.get_chat(candidate["chat_id"])
                    binding = repository.current_binding(candidate["chat_id"])
                    if (
                        not candidate["messages"]
                        and not candidate["generations"]
                        and binding
                        and binding.get("adapter_metadata", {}).get("desktop_new_from")
                        == current_session
                    ):
                        return self.mapping(binding["session_id"])
            if requested_session:
                receipt = repository.desktop_creation_for_seed(requested_session)
                if receipt:
                    mapped = self.mapping(receipt["session_id"])
                    if mapped["course_id"] != course_id:
                        raise AppError(
                            "DESKTOP_CREATE_CONFLICT", "同一新建身份对应的课程发生变化", 409
                        )
                    return mapped
                try:
                    mapped = self.mapping(requested_session)
                except StorageError as exc:
                    if exc.code != "CHAT_NOT_FOUND":
                        raise
                else:
                    chat = repository.get_chat(mapped["chat_id"])
                    if not chat["messages"] and not chat["generations"]:
                        return mapped
            # Carriers predating currentSessionId still converge concurrent
            # empty-course initialization. Explicit nonempty navigation above
            # retains the current Session as the source of the new blank.
            if not current_session and not requested_session:
                chats = repository.list_chats(course_id)
                chats = [chat for chat in chats if not preferences.get(chat["chat_id"], {}).get("archived")]
                if chats:
                    latest = repository.get_chat(chats[-1]["chat_id"])
                    if not latest["messages"] and not latest["generations"]:
                        return await self.open_chat(latest["chat_id"])
            chat = repository.create_chat(course_id)
            opened = await self.open_chat(chat["chat_id"])
            if requested_session or current_session:
                binding = repository.current_binding(opened["chat_id"])
                repository.update_binding_metadata(
                    binding["binding_id"],
                    {
                        **binding["adapter_metadata"],
                        **({"desktop_create_seed": requested_session} if requested_session else {}),
                        **({"desktop_new_from": current_session} if current_session else {}),
                    },
                )
            return opened

    def workspaces(self) -> dict:
        """Project a native Workspace feed from business records, without another store."""
        repository = self.service.repository
        items = []
        preferences = self.presentation.all()
        archived = []
        for course in repository.list_courses():
            session_ids = []
            for sid, view in self.history.views.items():
                if view["mapping"]["course_id"] == course["course_id"]:
                    session_ids.append(sid)
            for chat in repository.list_chats(course["course_id"]):
                binding = repository.current_binding(chat["chat_id"])
                if binding and binding["state"] not in {"stale", "retired"}:
                    session_ids.append(binding["session_id"])
                    if preferences.get(chat["chat_id"], {}).get("archived"):
                        archived.append(binding["session_id"])
            for sid in session_ids:
                mapped = self.history.views.get(sid, {}).get("mapping")
                if mapped and preferences.get(mapped["chat_id"], {}).get("archived"):
                    archived.append(sid)
            items.append(
                {
                    "workspaceId": course["course_id"],
                    "title": course["title"],
                    "path": str(self.service.execution.host.workspace / course["course_id"]),
                    "sessionIds": session_ids,
                    "createdAt": course["created_at"],
                    "updatedAt": course["created_at"],
                }
            )
        return {"items": items, "archivedSessionIds": list(dict.fromkeys(archived)), "pinnedSessionIds": []}

    def selected(self, session_id: str) -> tuple[str, str]:
        chat = self.service.repository.get_chat(self.mapping(session_id)["chat_id"])
        binding = self.service.repository.current_binding(chat["chat_id"])
        selection = binding.get("adapter_metadata", {}).get("desktop_selection", {})
        if selection:
            return selection["model"], selection["thinking"]
        generations = chat.get("generations", [])
        if generations:
            return generations[-1]["model"], generations[-1]["thinking"]
        return self.app.state.config.model, self.app.state.config.thinking

    def project_view(self, value, session_id=None):
        value = project_transcript(value, self.service.repository)

        def visit(item, inherited_sid=None):
            if isinstance(item, list):
                return [visit(child, inherited_sid) for child in item]
            if not isinstance(item, dict):
                return item
            sid = item.get("sessionId", item.get("id", inherited_sid))
            if not isinstance(sid, str) or not sid.startswith("learning-"):
                sid = inherited_sid
            header = item.get("header", {})
            if isinstance(header, dict) and isinstance(header.get("id"), str):
                sid = header["id"]
            result = {
                key: visit(child, key if key.startswith("learning-") else sid)
                for key, child in item.items()
            }
            if "cwd" in result and isinstance(sid, str):
                try:
                    mapped = self.read_mapping(sid)
                except (AppError, StorageError):
                    pass
                else:
                    result["cwd"] = str(self.service.execution.host.workspace / mapped["course_id"])
            header = result.get("header", {})
            if isinstance(header, dict) and "cwd" in header:
                projections = result.get("projections", {}).get("values", {})
                if "workingDirectory" in projections:
                    projections["workingDirectory"] = header["cwd"]
            title_field = (
                "value"
                if result.get("type") == "projection" and result.get("key") == "title"
                else "title"
            )
            title = result.get(title_field)
            if isinstance(sid, str) and sid.startswith("learning-") and (title is not None or "sessionId" in result):
                try:
                    mapped = self.read_mapping(sid)
                    chat = self.service.repository.get_chat(mapped["chat_id"])
                    source = result.get("source", {})
                    if source.get("kind") == "user" and isinstance(title, str):
                        self.presentation.set(mapped["chat_id"], title=title, manual=True)
                    saved = self.presentation.all().get(mapped["chat_id"], {})
                    if (saved.get("title_request_id") and not saved.get("manual")
                        and sid not in self._pending_renames and isinstance(title, str)
                        and title.strip() and title not in {"workspace", mapped["course_id"]}
                        and not title.startswith(("[[learning-generation:", "learning-"))):
                        if saved.get("title") != title:
                            self.presentation.set(mapped["chat_id"], title=title, title_receipt={"state": "completed"})
                    result[title_field] = title if sid in self._pending_renames and isinstance(title, str) else self.presentation.title(chat, title)
                except (AppError, StorageError):
                    pass
            return result

        return visit(value, session_id)

    def image(self, part: dict, request_id: str, index: int) -> str:
        try:
            data = base64.b64decode(part["data"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise AppError("IMAGE_INVALID", "图片编码无效", 422) from exc
        media_type = part.get("mediaType")
        if media_type not in _IMAGE_TYPES or not data or len(data) > 10_000_000:
            raise AppError("IMAGE_SIZE_INVALID", "图片格式无效或超过 10MB", 422)
        repository = self.service.repository
        match = _CAPTURE_NAME.fullmatch(part.get("name", ""))
        if match:
            item = repository.resolve_attachment(match[1])
            if (
                not item["confirmed"]
                or item["media_type"] != media_type
                or item["content_hash"] != hashlib.sha256(data).hexdigest()
            ):
                raise AppError("CONFIRMED_ATTACHMENT_CHANGED", "确认的截图内容已变化", 409)
            return item["attachment_id"]
        stable_id = (
            "attachment_"
            + hashlib.sha256(f"desktop:{request_id}:{index}".encode()).hexdigest()[:32]
        )
        try:
            existing = repository.resolve_attachment(stable_id)
        except StorageError as exc:
            if exc.code != "ATTACHMENT_NOT_FOUND":
                raise
        else:
            if (
                existing["content_hash"] != hashlib.sha256(data).hexdigest()
                or existing["media_type"] != media_type
                or not existing["confirmed"]
            ):
                raise AppError("SUBMISSION_CONFLICT", "同一提交身份的图片内容发生变化", 409)
            return stable_id
        # A native attachment is submitted by the user's Send action. The
        # repository decodes it, checks MIME/dimensions, and publishes bytes.
        return repository.stage_attachment(
            data,
            media_type,
            confirmed=True,
            attachment_id=stable_id,
        )["attachment_id"]

    async def upload_file(
        self,
        session_id: str,
        name: str,
        data: bytes,
        *,
        upload_id: str | None = None,
        existing_document: str | None = None,
    ) -> dict:
        mapped = self.mapping(session_id)
        repository = self.service.repository
        if not isinstance(name, str) or not name:
            raise AppError(
                "DOCUMENT_FILENAME_REQUIRED", "请选择带文件名的 PDF、TXT 或 Markdown 文件", 422
            )
        restored = re.fullmatch(r"learning-document_(document_[A-Za-z0-9_-]+)__(.+)", name)
        if restored:
            existing_document, name = restored.groups()
        media_type = _DOCUMENT_TYPES.get(Path(name).suffix.lower())
        if media_type is None:
            raise AppError(
                "DESKTOP_ATTACHMENT_UNSUPPORTED", "附件支持图片、PDF、TXT 和 Markdown", 422
            )
        if not data or len(data) > MAX_PDF_BYTES:
            raise AppError("DOCUMENT_SIZE_INVALID", "文件为空或超过整份附件上限", 422)
        if existing_document:
            validate_document_scope(repository, mapped["chat_id"], [existing_document])
            metadata = attachment_metadata(repository, existing_document)
            if (
                metadata["filename"] != name
                or metadata["media_type"] != media_type
                or metadata["sha256"] != hashlib.sha256(data).hexdigest()
                or metadata["size_bytes"] != len(data)
                or not metadata["confirmed"]
            ):
                raise AppError("DOCUMENT_UPLOAD_CONFLICT", "恢复的附件内容与已确认原件不一致", 409)
        else:
            if upload_id is None:
                upload_id = hashlib.sha256(f"{session_id}:{name}:".encode() + data).hexdigest()
            try:
                value = DocumentAttachmentInput(
                    chat_id=mapped["chat_id"],
                    filename=name,
                    media_type=media_type,
                    confirmed=True,
                    upload_id=upload_id,
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            except ValueError as exc:
                raise AppError("DOCUMENT_INVALID", "附件名称或上传身份无效", 422) from exc
            metadata = await asyncio.to_thread(upload_document, repository, value)
        return {"receiptId": metadata["attachment_id"], "file": _file_ref(metadata)}

    def document(self, receipt_id: str, chat_id: str) -> str:
        if not isinstance(receipt_id, str) or not receipt_id.startswith("document_"):
            raise AppError("DOCUMENT_RECEIPT_INVALID", "文件上传回执无效，请重新核对草稿附件", 422)
        validate_document_scope(self.service.repository, chat_id, [receipt_id])
        metadata = attachment_metadata(self.service.repository, receipt_id)
        if metadata["kind"] not in {"pdf", "text", "markdown"} or not metadata["confirmed"]:
            raise AppError("DOCUMENT_NOT_READY", "文件尚未完整准备好", 409)
        return receipt_id

    def read_image(self, session_id: str, attachment_id: str) -> dict | None:
        # Native historical image seats may reference business-owned images
        # projected above. Only attachments actually submitted to this chat
        # can be read here; arbitrary object ids and unsubmitted drafts cannot.
        if not isinstance(attachment_id, str) or not attachment_id.startswith("attachment_"):
            return None
        mapped = self.read_mapping(session_id)
        chat = self.service.repository.get_chat(mapped["chat_id"])
        if mapped.get("read_only") and attachment_id not in self.history.views[session_id]["attachment_ids"]:
            raise AppError("ATTACHMENT_NOT_IN_CHAT", "图片不属于此历史视图", 403)
        if not any(attachment_id in message["attachment_ids"] for message in chat["messages"]):
            raise AppError("ATTACHMENT_NOT_IN_CHAT", "此图片不属于当前对话记录", 403)
        image = self.service.repository.resolve_attachment(attachment_id)
        return {
            "attachment": _image_ref(self.service.repository, attachment_id),
            "data": base64.b64encode(image["path"].read_bytes()).decode("ascii"),
        }

    async def prompt(self, request: dict) -> dict:
        session_id = request.get("sessionId", "")
        chat_id = self.mapping(session_id)["chat_id"]
        request_id = request.get("requestId", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise AppError("DESKTOP_CONTENT_INVALID", "提交身份无效", 422)
        if request.get("mode", "queue") != "queue":
            raise AppError("DESKTOP_STEER_UNAVAILABLE", "请停止当前回答后再追问", 409)
        parts = request.get("content")
        if not isinstance(parts, list) or len(parts) > 40:
            raise AppError("DESKTOP_CONTENT_INVALID", "问题内容无效", 422)
        texts, attachment_ids = [], []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise AppError("DESKTOP_CONTENT_INVALID", "问题内容无效", 422)
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "image":
                attachment_ids.append(self.image(part, request_id, index))
            elif part.get("type") == "file":
                attachment_ids.append(self.document(part.get("receiptId"), chat_id))
            else:
                raise AppError(
                    "DESKTOP_ATTACHMENT_UNSUPPORTED", "附件支持图片、PDF、TXT 和 Markdown", 422
                )
        model, thinking = self.selected(session_id)
        chat = self.service.repository.get_chat(chat_id)
        with self.service.repository.engine.connect() as conn:
            workspace_ids = read_workspaces(conn, chat)
        selections = list(request.get("source_snapshot_ids", []))
        if request.get("current_document"):
            from learning_app.workbench.on_demand_sources import current_selection

            snapshot = current_selection(self.service.repository, request["current_document"], "\n".join(texts))
            if snapshot:
                selections.append(snapshot.snapshot_id)
        try:
            value = SubmissionInput(
                chat_id=chat_id,
                submission_id=request.get("requestId", ""),
                text="\n".join(texts),
                attachment_ids=attachment_ids,
                references=request.get("references", []),
                source_snapshot_ids=list(dict.fromkeys(selections)),
                read_course_ids=workspace_ids,
                question_video=request.get("question_video"),
                model=model,
                thinking=thinking,
            )
        except ValueError as exc:
            raise AppError("DESKTOP_CONTENT_INVALID", "提交身份或问题内容无效", 422) from exc
        async with self.app.state.submission_gate:
            if self.app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "应用正在安全退出", 409)
            submitted_draft = {**value.model_dump(),
                               "source_snapshot_ids": request.get("source_snapshot_ids", []),
                               "read_course_ids": request.get("read_course_ids", [])}
            await self.service.submit(value.model_dump(), submitted_draft=submitted_draft)
        # The same requestId is used by ExecutionAdapter's native prompt; the
        # durable user event retires the official composer's pending echo.
        return {"accepted": True}

    async def cancel(self, request: dict) -> dict:
        chat_id = self.mapping(request.get("sessionId", ""))["chat_id"]
        chat = self.service.repository.get_chat(chat_id)
        active = [g for g in chat["generations"] if g["state"] not in TERMINAL]
        if active:
            await self.service.cancel(active[-1]["generation_id"])
        return {"accepted": True}

    async def rpc(self, method: str, envelope: dict) -> dict | None:
        request = _arguments(envelope)
        sid = request.get("sessionId") or request.get("address", {}).get("sessionId")
        if method in {"workspace/archiveSession", "workspace/unarchiveSession"}:
            mapped = self.read_mapping(sid or "")
            chat = self.service.repository.get_chat(mapped["chat_id"])
            active = [g for g in chat["generations"] if g["state"] not in TERMINAL]
            if method == "workspace/archiveSession":
                if active and request.get("stopActivity") is not True:
                    raise AppError("workspace/session-active", "此对话仍有回答运行或待核对，请确认停止后归档", 409)
                if active:
                    for generation in active:
                        await self.service.cancel(generation["generation_id"])
                    chat = self.service.repository.get_chat(mapped["chat_id"])
                    if any(g["state"] not in TERMINAL for g in chat["generations"]):
                        raise AppError("workspace/session-active", "停止结果仍待核对，暂未归档", 409)
            # This pinned SEA has no Workspace server controller. Its original
            # client contract is backed by the same business projection as follow.
            self.presentation.set(mapped["chat_id"], archived=method == "workspace/archiveSession")
            return {"archivedSessionIds": self.workspaces()["archivedSessionIds"]}
        if isinstance(sid, str) and sid.startswith("learning-history-"):
            if not self.history.mapping(sid):
                raise AppError("DESKTOP_SESSION_UNMAPPED", "历史视图身份无效", 404)
            if method == "session/rename":
                title = request.get("title")
                if not isinstance(title, str) or not title.strip() or len(title.encode()) > 512:
                    raise AppError("title-invalid", "请输入有效的对话名称", 422)
                self.presentation.set(self.history.mapping(sid)["chat_id"], title=title.strip(), manual=True)
                return {"title": title.strip(), "seq": 0}
            if method == "session/create":
                if request.get("workspaceId", self.history.mapping(sid)["course_id"]) != self.history.mapping(sid)["course_id"]:
                    raise AppError("DESKTOP_CREATE_CONFLICT", "历史不属于此课程")
                return {"sessionId": sid}
            if method == "session/page":
                return self.history.page(sid, request)
            if method == "session/projections":
                return {"kind": "sequenced", **self.history.projections(sid)}
            if method == "session/attachment":
                value = self.read_image(sid, request.get("attachmentId", ""))
                if value is None:
                    raise AppError("ATTACHMENT_NOT_IN_CHAT", "图片不属于此历史视图", 403)
                return value
            if method == "session/modelCatalog":
                return await self.native_read(method, envelope)
            raise AppError("IMPORTED_EXECUTION_READ_ONLY", "导入历史只读，未提交新的执行请求")
        if method in _MESSAGE_FEEDBACK_METHODS:
            # Native feedback belongs to one persisted Host session. Keep its
            # versioned writes upstream, but admit only project-owned sessions.
            session_id = request.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AppError("DESKTOP_SESSION_INVALID", "请选择当前对话", 422)
            self.mapping(session_id)
            return None
        if method in {"session/list", "session/search"}:
            value = await self.native_read(method, envelope)
            aliases = self.history.views
            allowed = {sid for workspace in self.workspaces()["items"] for sid in workspace["sessionIds"]}
            original = [item for item in value["items"] if item["sessionId"] in allowed and item["sessionId"] not in aliases]
            if method == "session/list":
                return self.project_view({**value, "items": [*original, *self.history.summaries()]})
            query = request.get("query", "").casefold().strip()
            if not query or len(query) > 500:
                return self.project_view({**value, "items": original})
            additions = []
            for identity, view in aliases.items():
                texts = [part.get("text", "") for row in view["records"] for part in (row["event"]["data"].get("content") or row["event"]["data"].get("message", {}).get("content", []))]
                found = next((text for text in texts if query in text.casefold()), None)
                if found is not None:
                    additions.append({"sessionId": identity, "snippet": found[:240]})
            items = [*original, *additions]
            return self.project_view({"items": items[:20], "hasMore": value.get("hasMore", False) or len(items) > 20})
        if method == "settings/describe":
            native = await self.native_read(method, envelope)
            local = self.theme.handle(method, request)
            return {**native, "hasDocument": False, "namespaces": [
                *[row for row in native["namespaces"] if row["ns"] != "ui-theme"],
                *local["namespaces"],
            ]}
        if method in {"settings/query", "settings/mutate", "settings/update", "settings/replace"}:
            namespace = request.get("ns", request.get("namespace", ""))
            if namespace == "ui-theme":
                return self.theme.handle(method, request)
            if method != "settings/query" and not (
                isinstance(namespace, str)
                and (namespace.startswith("ui-") or namespace in {
                    "locale", "agent-preset-registry", "agent-default-model",
                })
            ):
                raise AppError("DESKTOP_SETTINGS_MANAGED", "此执行设置由学习应用管理，不能绕过资料与模型权限边界", 403)
            return None
        if method.startswith("settings/"):
            raise AppError("DESKTOP_METHOD_DENIED", "此设置操作未开放", 403)
        if method == "permissionPresets/catalog":
            # This app's tool guard owns authority; no shell permission preset
            # is installed. Keep the native row unavailable without a failed
            # request or advertising a permission the app cannot grant.
            return {"options": [], "defaultOptions": [], "defaultPreset": "custom"}
        if method == "agentPresets/select":
            sid = _scoped_session(envelope, request)
            self.mapping(sid or "")
            return None
        if method == "commands/execute":
            sid = _scoped_session(envelope, request)
            self.mapping(sid or "")
            # The native export command only admits a log download. Other
            # human commands cannot bypass the business submission path.
            line = request.get("line")
            if not isinstance(line, str) or line.strip() != "/export":
                raise AppError("DESKTOP_METHOD_DENIED", "此指令请通过学习对话提交", 403)
            return None
        if method == "session/prompt":
            return await self.prompt(request)
        if method == "session/cancel":
            return await self.cancel(request)
        if method == "session/attachment":
            value = self.read_image(request.get("sessionId", ""), request.get("attachmentId", ""))
            if value is not None:
                return value
        if method == "fileUploads/upload":
            payload = envelope.get("payload", {})
            scope = payload.get("scope", {})
            args = payload.get("args", {})
            if not isinstance(scope, dict) or not isinstance(args, dict):
                raise AppError("DESKTOP_RPC_INVALID", "附件请求参数无效", 422)
            session_id = (
                request.get("sessionId")
                or payload.get("sessionId")
                or payload.get("agentId")
                or args.get("agentId")
                or scope.get("sessionId")
                or scope.get("agentId")
            )
            try:
                data = base64.b64decode(request.get("data", ""), validate=True)
            except (TypeError, ValueError, binascii.Error) as exc:
                raise AppError("DOCUMENT_INVALID", "附件编码无效", 422) from exc
            return await self.upload_file(session_id or "", request.get("name", ""), data)
        if method == "session/selectModel":
            sid = request.get("sessionId", "")
            self.mapping(sid)
            model = request.get("model", "")
            effort = request.get("reasoningEffort", self.app.state.config.thinking)
            thinking = "none" if effort == "off" else effort
            execution = self.service.execution
            expected_provider = (
                "deepseek-account"
                if getattr(execution, "mode", "fixture") == "official"
                and getattr(execution, "credential_route", "api_key") == "account"
                else "deepseek-official"
            )
            if request.get("provider") != expected_provider:
                raise AppError(
                    "CREDENTIAL_ROUTE_MISMATCH", "所选模型提供方与当前登录方式不一致", 409
                )
            await self.service.execution.validate_choice(model, thinking)
            selected = await self.service.execution.peer.rpc("session/selectModel", request)
            mapped = self.mapping(sid)
            binding = self.service.repository.current_binding(mapped["chat_id"])
            self.service.repository.update_binding_metadata(
                binding["binding_id"],
                {
                    **binding.get("adapter_metadata", {}),
                    "desktop_selection": {"model": model, "thinking": thinking},
                },
            )
            return selected
        if method == "session/rename":
            sid = request.get("sessionId", "")
            mapped = self.mapping(sid)
            self._pending_renames.add(sid)
            try:
                selected = await self.service.execution.peer.rpc(method, request)
                self.presentation.set(mapped["chat_id"], title=selected["title"], manual=True)
            finally:
                self._pending_renames.discard(sid)
            binding = self.service.repository.current_binding(mapped["chat_id"])
            self.service.repository.update_binding_metadata(
                binding["binding_id"],
                {
                    **binding.get("adapter_metadata", {}),
                    "desktop_title_override": selected["title"],
                },
            )
            return selected
        if method == "session/create":
            # Native New Session remains a project chat. Caller-provided paths
            # and arbitrary workspace ids never become filesystem authority.
            previous = request.get("sessionId")
            course_id = request.get("workspaceId")
            if previous:
                try:
                    mapped = self.mapping(previous)
                except StorageError as exc:
                    if exc.code != "CHAT_NOT_FOUND" or not course_id:
                        raise
                else:
                    course_id = mapped["course_id"]
            course_id = course_id or ensure_unclassified(self.service.repository)
            if not any(
                c["course_id"] == course_id for c in self.service.repository.list_courses()
            ):
                raise AppError("DESKTOP_COURSE_REQUIRED", "指定工作区不存在", 409)
            opened = await self.create_chat(course_id, previous, request.get("currentSessionId"))
            return {"sessionId": opened["session_id"]}
        if method == "workspace/initializeDefault":
            items = self.workspaces()["items"]
            return {"workspace": items[0]} if items else _NO_VALUE
        if method in {"account/startSignIn", "account/cancelSignIn", "account/signOut"}:
            from learning_app.accounts import (
                AccountSignInInput,
                _client,
                _require_idle,
                _require_official,
            )

            execution = _require_official(self.app)
            if method == "account/startSignIn":
                native_client = request.get("client", {})
                client = _client(
                    AccountSignInInput(
                        locale=native_client.get("locale", "zh-CN"),
                        timezone_offset_seconds=native_client.get("timezoneOffsetSeconds", 28800),
                    )
                )
                # Harness owns PKCE and its callback. Renderer-supplied
                # callback origins cannot redirect local account credentials.
                return await execution.peer.rpc_args(
                    method,
                    {
                        "client": client,
                        "callbackOrigin": execution.host.base,
                        "loginSource": "web",
                    },
                )
            if method == "account/cancelSignIn":
                attempt_id = request.get("attemptId")
                if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 200:
                    raise AppError("ACCOUNT_ATTEMPT_INVALID", "登录尝试身份无效", 422)
                return await execution.peer.rpc_args(method, {"attemptId": attempt_id})
            async with self.app.state.submission_gate:
                _require_idle(self.app)
                value = await execution.peer.rpc_args(method, {"client": _client()})
                self.app.state.account_details = {}
                execution._account_models = []
                return value
        if method not in _READ_METHODS:
            raise AppError("DESKTOP_METHOD_DENIED", "此操作不在学习试用范围内", 403)
        sid = _scoped_session(envelope, request)
        if method == "commands/list" and not sid:
            raise AppError("DESKTOP_SESSION_INVALID", "请选择当前对话", 422)
        address = request.get("address")
        if isinstance(address, dict):
            if address.get("kind") != "session":
                raise AppError("DESKTOP_METHOD_DENIED", "此试用不支持子代理会话", 403)
            sid = address.get("sessionId")
        if sid:
            self.mapping(sid)
        return None


def install_desktop_routes(app: FastAPI, sessions) -> None:
    gateway = DesktopGateway(app)
    app.state.desktop_gateway = gateway

    @app.get("/api/v1/desktop/account-avatar/{version}", include_in_schema=False)
    async def account_avatar(version: str):
        from learning_app.accounts import _client

        profile = await gateway.service.execution.peer.rpc_args("account/getProfile", _client())
        reference = picture_reference(profile)
        if reference is None or reference[1] != version:
            raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像已改变，请刷新账号", 404)
        try:
            content, media = await asyncio.to_thread(read_account_picture, reference[0])
        except (OSError, ValueError, httpx.HTTPError) as error:
            raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像暂时无法加载", 502) from error
        return Response(content, media_type=media)

    @app.post("/api/v1/desktop/chats/{chat_id}/open", response_model=DesktopSessionResponse)
    async def open_chat(chat_id: str):
        return await gateway.open_chat(chat_id)

    @app.post("/api/v1/desktop/courses/{course_id}/open", response_model=DesktopSessionResponse)
    async def open_course(course_id: str):
        return await gateway.open_course(course_id)

    @app.get("/api/v1/desktop/sessions/{session_id}", response_model=DesktopSessionResponse)
    async def mapping(session_id: str):
        return gateway.read_mapping(session_id)

    @app.post("/api/v1/desktop/default/open", response_model=DesktopSessionResponse)
    async def open_default():
        return await gateway.create_chat(None, None)

    @app.get("/api/v1/desktop/sessions/{session_id}/answer-sources")
    def answer_sources(session_id: str, native_turn: int = Query(ge=1)):
        from sqlalchemy import select

        from learning_app.workbench import schema as ws
        from learning_app.workbench.evidence import chat_evidence

        mapped = gateway.read_mapping(session_id)
        repository = gateway.service.repository
        with repository.engine.connect() as conn:
            runs = set(conn.execute(select(ws.native_scopes.c.run_id).where(
                ws.native_scopes.c.session_id == session_id,
                ws.native_scopes.c.business_binding["native_turn"].as_integer() == native_turn,
            )).scalars())
        return [item for item in chat_evidence(repository, mapped["course_id"], mapped["chat_id"]).items
                if item.generation_id in runs]

    @app.get("/api/v1/desktop/sessions/{session_id}/answer-subtitles")
    def answer_subtitles(session_id: str, native_turn: int = Query(ge=1)):
        from sqlalchemy import select

        from learning_app.media import MediaStore
        from learning_app.workbench import corpus_schema
        from learning_app.workbench import schema as ws
        from learning_app.workbench.preparations import PreparationStore

        gateway.read_mapping(session_id)
        repository = gateway.service.repository
        with repository.engine.connect() as conn:
            rows = list(conn.execute(select(corpus_schema.query_receipts.c.result).join(
                ws.native_scopes, ws.native_scopes.c.run_id == corpus_schema.query_receipts.c.run_id
            ).where(ws.native_scopes.c.session_id == session_id,
                    ws.native_scopes.c.business_binding["native_turn"].as_integer() == native_turn)
            ).scalars())
        results = []
        for result in rows:
            if not result.get("preparation_id"):
                continue
            job = PreparationStore(repository).summary(result["course_id"], result["preparation_id"])
            results.append({"job": job, "video": MediaStore(repository).open(job.asset_id, job.source_version)})
        return results

    @app.get("/api/v1/desktop/sessions/{session_id}/documents/{task_id}")
    def generated_document(session_id: str, task_id: str):
        from sqlalchemy import select

        from learning_app.workbench import schema as ws
        from learning_app.workbench.artifact_files import published_file

        mapped = gateway.read_mapping(session_id)
        task = TaskStore(gateway.service.repository).get(mapped["course_id"], task_id)
        if task.chat_id != mapped["chat_id"]:
            raise AppError("TASK_CHAT_DENIED", "该文档不属于当前对话", 403)
        outline = None
        if task.kind == "book" and task.state == "awaiting_outline":
            from learning_app.workbench.books import BookStore

            outline = BookStore(gateway.service.repository).outline(task.course_id, task_id)
        with gateway.service.repository.engine.connect() as conn:
            spec = conn.execute(select(ws.tasks.c.scope_spec).where(ws.tasks.c.task_id == task_id)).scalar_one()
        return {"task": task, "file": published_file(gateway.service.repository, task), "outline": outline,
                "open_requested": bool((spec or {}).get("origin", {}).get("open_result"))}

    @app.post("/api/v1/desktop/sessions/{session_id}/workspaces", response_model=DesktopSessionResponse)
    async def update_workspaces(session_id: str, value: DesktopWorkspaceInput):
        mapped = gateway.mapping(session_id)
        set_workspaces(gateway.service.repository, mapped["chat_id"], value.course_ids)
        return gateway.mapping(session_id)

    @app.post("/api/v1/desktop/chats/{chat_id}/history", response_model=DesktopSessionResponse)
    async def history(chat_id: str):
        sid = gateway.history.chats.get(chat_id)
        if not sid:
            raise AppError("IMPORT_HISTORY_NOT_FOUND", "此对话没有导入历史", 404)
        return gateway.read_mapping(sid)

    @app.post("/api/v1/desktop/chats/{chat_id}/continue", response_model=DesktopSessionResponse)
    async def continue_chat(chat_id: str):
        return await gateway.open_chat(chat_id, history_view=False)

    @app.get(
        "/api/v1/desktop/sessions/{session_id}/learning-task-links",
        response_model=list[NativeLearningTaskLink],
    )
    def learning_task_links(session_id: str, native_turn: int = Query(ge=1)):
        binding = gateway.read_mapping(session_id)
        if binding.get("read_only"):
            return []
        return admitted_task_links(
            app.state.repository, binding["course_id"], binding["chat_id"], session_id, native_turn
        )

    @app.post("/api/v1/desktop/sessions/{session_id}/learning-tasks", response_model=TaskView)
    async def learning_task(session_id: str, value: DesktopLearningTaskInput):
        binding = gateway.mapping(session_id)
        model, thinking = gateway.selected(session_id)
        config = app.state.config
        payload = value.model_dump(
            exclude={"source_snapshot_ids", "pdf_selection", "cached_task_id"}
        )
        identities = value.source_snapshot_ids
        if value.pdf_selection:
            from learning_app.workbench.reading import DocumentReader

            if identities or value.coverage_mode != "selection":
                raise AppError("LEARNING_TASK_INPUT_INVALID", "局部选区与完整任务范围不能混用", 422)
            snapshot = await asyncio.to_thread(
                DocumentReader(app.state.repository).capture,
                binding["course_id"],
                value.pdf_selection,
            )
            identities = [snapshot.snapshot_id]
        try:
            request = TaskCreate(
                **payload,
                chat_id=binding["chat_id"],
                settings=ModelSettings(
                    model=model,
                    thinking=thinking,
                    max_output_tokens=config.max_output_tokens,
                    timeout_seconds=config.model_timeout_seconds,
                ),
                units=[
                    TaskUnitInput(
                        unit_id=f"selection_{index}",
                        label=f"选区 {index + 1}",
                        source_snapshot_id=identity,
                    )
                    for index, identity in enumerate(identities)
                ],
            )
        except ValueError as exc:
            raise AppError("LEARNING_TASK_INPUT_INVALID", "任务范围与当前选择不符", 422) from exc
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未接收新的学习生成", 409)
            from learning_app.workbench.source_grants import declare_snapshot

            with app.state.repository.engine.begin() as conn:
                for identity in identities:
                    declare_snapshot(
                        conn,
                        binding["course_id"],
                        identity,
                        authority={"kind": "user_translation", "session_id": session_id},
                    )
            task = await asyncio.to_thread(
                app.state.service.translation_reuse.reuse,
                binding["course_id"],
                request,
                value.cached_task_id,
            )
            if task is None:
                task = await asyncio.to_thread(
                    TaskStore(app.state.repository).create, binding["course_id"], request
                )
                app.state.service.translation_reuse.remember_admission(task)
        app.state.task_executor.schedule(binding["course_id"], task.task_id)
        return task

    @app.get(
        "/api/v1/desktop/sessions/{session_id}/translation-policy",
        response_model=TranslationPolicyView,
    )
    def translation_policy(session_id: str):
        binding = gateway.mapping(session_id)
        model, thinking = gateway.selected(session_id)
        config = app.state.config
        return TranslationPolicyView(
            course_id=binding["course_id"],
            chat_id=binding["chat_id"],
            settings=ModelSettings(
                model=model,
                thinking=thinking,
                max_output_tokens=config.max_output_tokens,
                timeout_seconds=config.model_timeout_seconds,
            ),
            policy_id=app.state.service.translation_reuse.policy_id(),
        )

    @app.get(
        "/api/v1/courses/{course_id}/learning/tasks/{task_id}/translation-cache-entry",
        response_model=TranslationCacheEntry,
    )
    def translation_cache_entry(course_id: str, task_id: str):
        return app.state.service.translation_reuse.entry(course_id, task_id)

    @app.post(
        "/api/v1/desktop/sessions/{session_id}/learning-task-intents", response_model=TaskView
    )
    async def learning_task_intent(session_id: str, value: TaskIntent):
        binding = gateway.mapping(session_id)
        model, thinking = gateway.selected(session_id)
        config = app.state.config
        from learning_app.workbench.task_coverage import TaskCoverage

        if value.chat_id is not None and value.chat_id != binding["chat_id"]:
            raise AppError("TASK_CHAT_SCOPE_DENIED", "任务对话与当前提交归属不符", 403)
        request = TaskAdmission(
            **value.model_dump(mode="json", exclude={"chat_id"}),
            chat_id=binding["chat_id"],
            settings=ModelSettings(
                model=model,
                thinking=thinking,
                max_output_tokens=config.max_output_tokens,
                timeout_seconds=config.model_timeout_seconds,
            ),
        )
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未接收新的学习任务", 409)
            task = await asyncio.to_thread(
                TaskCoverage(app.state.repository).admit, binding["course_id"], request
            )
        app.state.task_executor.schedule(binding["course_id"], task.task_id)
        return task

    @app.get("/desktop", include_in_schema=False)
    async def redirect():
        return RedirectResponse("/desktop/")

    @app.websocket("/desktop/api/remote.mux")
    async def mux(socket: WebSocket):
        protocols = socket.scope.get("subprotocols", [])
        csrf = protocols[0].removeprefix("learning.") if protocols else ""
        if (
            socket.headers.get("origin") not in app.state.config.allowed_origins
            or not protocols
            or not sessions.valid(socket.cookies.get(COOKIE_NAME), csrf)
        ):
            await socket.close(code=1008)
            return
        client = await gateway.browser()
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        await socket.accept(subprotocol=protocols[0])
        send_lock = asyncio.Lock()
        local_streams: dict[str, asyncio.Task] = {}
        stream_sessions: dict[str, str] = {}

        async def deliver(value):
            async with send_lock:
                await socket.send_text(value if isinstance(value, str) else json.dumps(value))

        async def workspace_feed(stream_id):
            previous = None
            while True:
                baseline = gateway.workspaces()
                fingerprint = json.dumps(baseline, sort_keys=True)
                if fingerprint != previous:
                    await deliver(
                        {
                            "type": "item",
                            "streamId": stream_id,
                            "value": {
                                "type": "baseline" if previous is None else "replace",
                                "value": baseline,
                            },
                        }
                    )
                    previous = fingerprint
                await asyncio.sleep(0.25)

        async def history_feed(stream_id, sid, request):
            try:
                snapshot = await gateway.history.opening(sid, request.get("maxMessages", 50))
                await deliver({"type": "item", "streamId": stream_id, "value": snapshot})
                await asyncio.Future()
            except (AppError, StorageError, ExecutionError) as error:
                await deliver({"type": "error", "streamId": stream_id, "error": {"code": error.code, "message": getattr(error, "message", "历史读取未完成"), "details": {}}})

        async with websockets.connect(
            f"ws://127.0.0.1:{gateway.service.execution.host.port}/api/remote.mux",
            additional_headers={"Cookie": cookie},
            proxy=None,
            close_timeout=2,
        ) as upstream:

            async def receive():
                while True:
                    raw = await socket.receive_text()
                    if len(raw) > 1_000_000:
                        await socket.close(code=1009)
                        return
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        await socket.close(code=1008, reason="DESKTOP_STREAM_INVALID")
                        return
                    stream_id = value.get("streamId", "")
                    kind = value.get("type")
                    if kind == "open":
                        method = _method(value.get("endpoint", ""))
                        if method not in _STREAM_METHODS:
                            await deliver(
                                {
                                    "type": "error",
                                    "streamId": stream_id,
                                    "error": {
                                        "code": "DESKTOP_METHOD_DENIED",
                                        "message": "此订阅不在学习试用范围内",
                                        "details": {"method": method[:100]},
                                    },
                                }
                            )
                            continue
                        request = _arguments(value)
                        sid = request.get("sessionId") or request.get("address", {}).get("sessionId")
                        if isinstance(sid, str) and sid.startswith("learning-history-"):
                            if method != "session/follow" or not gateway.history.mapping(sid):
                                await deliver({"type": "error", "streamId": stream_id, "error": {"code": "IMPORTED_EXECUTION_READ_ONLY", "message": "导入历史只读", "details": {}}})
                            else:
                                if stream_id in local_streams:
                                    local_streams[stream_id].cancel()
                                local_streams[stream_id] = asyncio.create_task(history_feed(stream_id, sid, request))
                            continue
                        if method == "workspace/follow":
                            if stream_id in local_streams:
                                local_streams[stream_id].cancel()
                            local_streams[stream_id] = asyncio.create_task(
                                workspace_feed(stream_id)
                            )
                            continue
                        if method == "session/follow":
                            address = request.get("address", {})
                            if address.get("kind") != "session":
                                await deliver(
                                    {
                                        "type": "error",
                                        "streamId": stream_id,
                                        "error": {
                                            "code": "DESKTOP_METHOD_DENIED",
                                            "message": "此试用仅支持当前课程对话",
                                            "details": {},
                                        },
                                    }
                                )
                                continue
                            try:
                                gateway.mapping(address.get("sessionId", ""))
                            except (AppError, StorageError):
                                await deliver(
                                    {
                                        "type": "error",
                                        "streamId": stream_id,
                                        "error": {
                                            "code": "DESKTOP_SESSION_UNMAPPED",
                                            "message": "请从课程入口打开对话",
                                            "details": {},
                                        },
                                    }
                                )
                                continue
                            stream_sessions[stream_id] = address["sessionId"]
                    elif kind == "cancel" and stream_id in local_streams:
                        local_streams.pop(stream_id).cancel()
                        continue
                    elif kind == "end":
                        # Read-only streams may half-close their empty uplink.
                        # This carries no tool/program input and keeps output alive.
                        if stream_id not in local_streams:
                            await upstream.send(raw)
                        continue
                    elif kind != "cancel":
                        # No bidirectional tool/program input through mux.
                        logger.warning(
                            "Native desktop stream frame denied: type=%s", str(kind)[:50]
                        )
                        await socket.close(code=1008, reason="DESKTOP_STREAM_INPUT_DENIED")
                        return
                    await upstream.send(raw)

            async def send():
                async for raw in upstream:
                    value = json.loads(raw)
                    await deliver(
                        gateway.project_view(value, stream_sessions.get(value.get("streamId")))
                    )

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in [*tasks, *local_streams.values()]:
                    task.cancel()
                await asyncio.gather(*tasks, *local_streams.values(), return_exceptions=True)

    @app.api_route("/desktop/{path:path}", methods=["GET", "HEAD", "POST"], include_in_schema=False)
    async def proxy(path: str, request: Request):
        if path in {"", "index.html"} and request.method in {"GET", "HEAD"} and app.state.config.desktop_enabled and app.state.ready:
            process = getattr(getattr(app.state.service.execution, "host", None), "process", None)
            if not app.state.service.execution_ready or (process is not None and process.poll() is not None):
                return RedirectResponse("/?read_only=1", status_code=307)
        gateway.service
        if path in _CLIENT_NAMES or path in _BUILT_CLIENT_NAMES:
            folder = ".runtime/desktop/client" if path in _BUILT_CLIENT_NAMES else "desktop/client"
            candidate = app.state.config.project_root / folder / path
            if not candidate.is_file():
                raise AppError("DESKTOP_BUILD_MISSING", "桌面学习模块尚未准备好", 503)
            return FileResponse(candidate)
        if "\\" in path or any(part == ".." for part in path.split("/")):
            raise AppError("DESKTOP_PATH_DENIED", "资源路径无效", 403)
        if path == "api/session/uploadFileBinary":
            if request.method != "POST":
                raise AppError("DESKTOP_METHOD_DENIED", "附件请求方式无效", 403)
            try:
                data = bytearray()
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_PDF_BYTES:
                        raise AppError("DOCUMENT_SIZE_INVALID", "文件超过整份附件上限", 422)
                    data.extend(chunk)
                value = await gateway.upload_file(
                    request.query_params.get("sessionId", ""),
                    request.query_params.get("name", ""),
                    bytes(data),
                    upload_id=request.headers.get("x-learning-upload-id"),
                    existing_document=request.headers.get("x-learning-existing-document"),
                )
                return JSONResponse({"ok": True, "value": value})
            except (AppError, StorageError, ExecutionError) as exc:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": getattr(exc, "message", "附件操作未完成，请核对原文件"),
                            "details": {},
                        },
                    }
                )
        if path == "api/session.export":
            if request.method not in {"GET", "HEAD"}:
                raise AppError("DESKTOP_METHOD_DENIED", "导出请求方式无效", 403)
            sid = request.query_params.get("sessionId", "")
            gateway.mapping(sid)
            if request.query_params.get("includeDescendants", "false") not in {"true", "false"}:
                raise AppError("DESKTOP_RPC_INVALID", "导出范围无效", 422)
            client = await gateway.browser()
            response = await client.get("/api/session.export", params={"sessionId": sid, "includeDescendants": "false"})
            return Response(response.content, status_code=response.status_code, headers={
                key: value for key, value in response.headers.items()
                if key.lower() in {"content-type", "content-disposition"}
            })
        if path.startswith("api/"):
            if request.method != "POST":
                raise AppError("DESKTOP_METHOD_DENIED", "原生接口请求方式无效", 403)
            body = await request.body()
            if len(body) > 16_000_000:
                raise AppError("DESKTOP_CONTENT_INVALID", "请求内容过大", 413)
            try:
                envelope = json.loads(body)
                method = _method(path[4:])
                if not isinstance(envelope, dict) or _method(envelope.get("method", "")) != method:
                    raise ValueError("method mismatch")
                value = await gateway.rpc(method, envelope)
            except (AppError, StorageError, ExecutionError) as exc:
                return JSONResponse(
                    {
                        "type": "server-response",
                        "rpcId": envelope.get("rpcId"),
                        "result": {
                            "ok": False,
                            "error": {
                                "code": exc.code,
                                "message": getattr(exc, "message", "操作未完成，请检查当前对话"),
                                "details": {},
                            },
                        },
                    }
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise AppError("DESKTOP_RPC_INVALID", "原生界面请求无效", 422) from exc
            if value is not None:
                if value is _NO_VALUE:
                    return JSONResponse(
                        {
                            "type": "server-response",
                            "rpcId": envelope.get("rpcId"),
                            "result": {"ok": True},
                        }
                    )
                return JSONResponse(
                    {
                        "type": "server-response",
                        "rpcId": envelope.get("rpcId"),
                        "result": {"ok": True, "value": value},
                    }
                )
            client = await gateway.browser()
            response = await client.post(
                "/" + path, content=body, headers={"Content-Type": "application/json"}
            )
            if method == "account/getProfile" and response.status_code == 200:
                return JSONResponse(project_account_picture(response.json()))
            if (
                method in {"session/page", "session/list", "session/search", "session/projections"}
                and response.status_code == 200
            ):
                native_request = _arguments(envelope)
                sid = native_request.get(
                    "sessionId", native_request.get("address", {}).get("sessionId")
                )
                return JSONResponse(gateway.project_view(response.json(), sid))
        else:
            if request.method not in {"GET", "HEAD"} or (
                path not in _STATIC_NAMES and not path.startswith(_STATIC_ROOTS)
            ):
                raise AppError("DESKTOP_PATH_DENIED", "此资源不在学习试用范围内", 403)
            client = await gateway.browser()
            query = ("?" + request.url.query) if request.url.query else ""
            response = await client.get("/" + path + query)
        content = response.content
        content_type = response.headers.get("content-type", "application/octet-stream")
        if path in {"", "index.html"} and response.status_code == 200:
            html = gateway.theme.project_index(response.text)
            # Keep upstream markup/styles. The classic script runs after the
            # official boot table exists and before its Vite application entry.
            html = html.replace(
                '<script type="module"',
                # The project owns its existing account/model
                # flow; this upstream public option skips only
                # the generic direct-provider Key onboarding.
                "<script>globalThis.__DSH_MODELS_ONBOARDING__="
                '{"credentialOnboarding":false}</script>'
                '<script src="library-components.js"></script>'
                '<script src="video-components.js"></script>'
                '<script src="draft-bridge.js"></script>'
                '<script src="answer-quotes.js"></script>'
                '<script src="course-library.js"></script>'
                '<script src="learning-bridge.js"></script><script type="module"',
                1,
            )
            content = html.encode("utf-8")
        return Response(
            content,
            response.status_code,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
