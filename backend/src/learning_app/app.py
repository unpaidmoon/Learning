import asyncio
import base64
import binascii
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from learning_app import __version__
from learning_app.accounts import install_account_routes
from learning_app.config import AppConfig, load_config
from learning_app.course_files import install_course_file_routes
from learning_app.data_location import ProfileLocation
from learning_app.data_routes import DataTransferService, install_data_routes
from learning_app.desktop import install_desktop_routes
from learning_app.desktop_update_routes import install_update_routes
from learning_app.desktop_updates import DesktopUpdates
from learning_app.documents import attachment_metadata, document_content, install_document_routes
from learning_app.drafts import install_draft_routes
from learning_app.dto import (
    AttachmentInput,
    AttachmentResponse,
    ChatResponse,
    CourseInput,
    CourseResponse,
    ErrorResponse,
    GenerationResponse,
    SessionResponse,
    SubmissionInput,
    SubmissionResponse,
)
from learning_app.errors import AppError
from learning_app.execution_supervisor import ExecutionSupervisor
from learning_app.health import Capabilities, HealthResponse
from learning_app.media import install_media_routes
from learning_app.runtime_control import install_runtime_routes
from learning_app.runtime_preferences import RuntimePowerKeeper, RuntimePreferences, resource_policy
from learning_app.runtime_settings_routes import install_runtime_settings_routes
from learning_app.security import COOKIE_NAME, BrowserSessions, LocalBoundaryMiddleware
from learning_app.storage import StorageError
from learning_app.video_previews import VideoPreviewCache, install_video_preview_routes
from learning_app.workbench.reading_routes import READING_RESOURCE_CSP, install_reading_routes
from learning_app.workbench.routes import install_workbench_routes


def create_app(
    config: AppConfig | None = None, *, repository=None, execution=None, execution_factory=None,
    integrations_provider_factory=None
) -> FastAPI:
    config = config or load_config()
    profile = ProfileLocation(config.data_dir)
    if repository is None:
        config = config.model_copy(update={"data_dir": profile.current()})
    sessions = BrowserSessions()

    @asynccontextmanager
    async def service_lifespan(app: FastAPI):
        from learning_app.data_activation import require_active_data
        from learning_app.service import GenerationService
        from learning_app.storage import Repository

        require_active_data(repository.root if repository is not None else config.data_dir)
        app.state.repository = repository or Repository(config.data_dir)
        from learning_app.integrations.host import IntegrationHost

        app.state.integrations = IntegrationHost(app.state.repository, config.project_root,
            provider_factory=integrations_provider_factory)
        app.state.repository.integrations = app.state.integrations
        from learning_app.data_maintenance import DataMaintenance

        DataMaintenance(app.state.repository).restore_local_jobs()
        app.state.runtime_preferences = RuntimePreferences(app.state.repository.root)
        app.state.desktop_updates = DesktopUpdates(
            app.state.repository.root, profile.anchor, config.project_root
        )
        app.state.data_transfers = DataTransferService(app)
        app.state.service = GenerationService(
            app.state.repository, config, execution=execution, execution_factory=execution_factory
        )
        if config.desktop_enabled:
            app.state.service.prepare_native_title = app.state.desktop_gateway.prepare_title
        await app.state.service.open()
        from learning_app.workbench.executor import TaskExecutor

        app.state.task_executor = TaskExecutor(
            app.state.repository,
            app.state.service,
            admission_gate=app.state.submission_gate,
            is_stopping=lambda: app.state.exit_prepared,
            wake_preparations=lambda: app.state.preparations.wake()
            if hasattr(app.state, "preparations")
            else None,
        )
        await app.state.task_executor.open()
        app.state.video_previews = VideoPreviewCache(
            app.state.repository,
            config.project_root,
            is_busy=lambda: any(not task.done() for task in tuple(app.state.service.tasks.values()))
            or (
                getattr(app.state, "preparations", None) is not None
                and app.state.preparations.store.has_urgent()
            ),
        )
        app.state.video_previews.start()
        from learning_app.workbench.processing import PreparationService

        app.state.preparations = PreparationService(
            app.state.repository,
            config.project_root,
            admission_gate=app.state.submission_gate,
            is_busy=lambda: any(not task.done() for task in tuple(app.state.service.tasks.values()))
            or app.state.video_previews.has_active_lease("interaction"),
            is_playing=lambda: app.state.video_previews.has_active_lease("playback"),
            is_interacting=lambda: app.state.video_previews.has_active_lease("interaction"),
            is_stopping=lambda: app.state.exit_prepared,
            get_resource_policy=lambda: resource_policy(
                app.state.runtime_preferences.effective().resource_mode
            ),
        )
        await app.state.preparations.open()

        def local_activity():
            return bool(
                app.state.preparations._active
                or app.state.task_executor._active_run
                or app.state.data_transfers.tasks
            ) or any(
                not task.done()
                and app.state.repository.get_generation(identity)["state"]
                in {"dispatching", "accepted", "running"}
                for identity, task in tuple(app.state.service.tasks.items())
            )

        app.state.power_keeper = RuntimePowerKeeper(app.state.runtime_preferences, local_activity)
        app.state.power_keeper.start()
        app.state.desktop_updates.start()
        app.state.execution_supervisor = ExecutionSupervisor(app)
        app.state.execution_supervisor.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await app.state.execution_supervisor.close()
            await app.state.desktop_updates.close()
            await asyncio.to_thread(app.state.power_keeper.close)
            await app.state.data_transfers.close()
            await app.state.preparations.close()
            await asyncio.to_thread(app.state.video_previews.close)
            await app.state.desktop_gateway.close()
            await app.state.task_executor.close()
            await app.state.service.close()
            app.state.repository.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with profile.lease():
            async with service_lifespan(app):
                yield

    app = FastAPI(
        title="Learning Application API",
        version=__version__,
        lifespan=lifespan,
        responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    app.state.config = config
    from learning_app.integrations.routes import install_integration_routes

    install_integration_routes(app)
    app.state.profile_location = profile
    app.state.ready = False
    app.add_middleware(LocalBoundaryMiddleware, config=config, sessions=sessions)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.message}, exc.status)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError):
        status = 404 if exc.code.endswith("NOT_FOUND") or exc.code == "ATTACHMENT_MISSING" else 409
        messages = {
            "ATTACHMENT_MISSING": "已确认的附件缺失，请检查原提交",
            "CHAT_EXECUTION_ACTIVE": "当前对话仍有活动或待对账生成",
            "SUBMISSION_CONFLICT": "同一提交身份的内容发生冲突",
            "ATTACHMENT_NOT_CONFIRMED": "图片尚未确认",
        }
        return JSONResponse(
            {"code": exc.code, "message": messages.get(exc.code, "数据操作未完成，请核对后重试")},
            status,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail)}, exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"code": "INPUT_INVALID", "message": "输入格式不正确"}, 422)

    @app.middleware("http")
    async def browser_capability(request: Request, call_next):
        path = request.url.path
        protected = path.startswith("/api/") or path.startswith("/desktop/api/")
        if protected and path not in {"/api/v1/health", "/api/v1/session"}:
            token = request.cookies.get(COOKIE_NAME)
            if not sessions.valid(token):
                return JSONResponse(
                    {"code": "SESSION_REQUIRED", "message": "请从本机应用页面连接"}, 403
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("x-learning-csrf", "")
                if not sessions.valid(token, csrf):
                    return JSONResponse(
                        {"code": "CSRF_DENIED", "message": "请求会话已失效，请重新连接"}, 403
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if config.mode == "start":
            fixed_reader = (
                path.startswith("/api/v1/reading/pdfjs/")
                and response.status_code == 200
                and response.headers.get("Content-Security-Policy") == READING_RESOURCE_CSP
            )
            if not fixed_reader:
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'; worker-src 'self' blob:"
                )
            if config.desktop_enabled and path.startswith("/desktop/"):
                # The pinned native boot table is rendered as classic inline
                # scripts by upstream. It carries no provider or Peer secrets.
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
                    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                )
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health(response: Response):
        ready = app.state.ready
        if not ready:
            response.status_code = 503
        harness_ready = ready and app.state.service.execution_ready
        harness = (
            "ready"
            if harness_ready
            else ("unavailable" if config.harness_enabled else "not_configured")
        )
        question_answering = (
            "ready"
            if harness_ready and app.state.service.question_answering_ready
            else ("unavailable" if harness_ready or config.harness_enabled else "not_configured")
        )
        return HealthResponse(
            instance_id=config.instance_id,
            ready=ready,
            configured_model=config.model,
            configured_thinking=config.thinking,
            capabilities=Capabilities(
                persistence="ready" if ready else "unavailable",
                video="ready" if ready else "unavailable",
                transcription="available" if ready else "unavailable",
                knowledge_base="available" if ready else "unavailable",
                harness=harness,
                question_answering=question_answering,
            ),
        )

    @app.get("/api/v1/session", response_model=SessionResponse)
    async def browser_session(request: Request, response: Response):
        token, csrf = sessions.create(request.cookies.get(COOKIE_NAME))
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            path="/" if config.desktop_enabled else "/api/",
        )
        return SessionResponse(csrf_token=csrf)

    @app.get("/api/v1/courses", response_model=list[CourseResponse])
    async def list_courses():
        return app.state.repository.list_courses()

    @app.post("/api/v1/courses", response_model=CourseResponse)
    async def create_course(value: CourseInput):
        return app.state.repository.create_course(value.title)

    @app.get("/api/v1/courses/{course_id}/chats", response_model=list[ChatResponse])
    async def list_chats(course_id: str):
        return app.state.repository.list_chats(course_id)

    @app.post("/api/v1/courses/{course_id}/chats", response_model=ChatResponse)
    async def create_chat(course_id: str):
        return app.state.repository.create_chat(course_id)

    @app.get("/api/v1/chats/{chat_id}", response_model=ChatResponse)
    async def get_chat(chat_id: str):
        return app.state.service.chat_view(chat_id)

    @app.post("/api/v1/attachments", response_model=AttachmentResponse)
    async def upload_attachment(value: AttachmentInput):
        try:
            data = base64.b64decode(value.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AppError("IMAGE_INVALID", "图片编码无效", 422) from exc
        if not data or len(data) > 20 * 1024 * 1024:
            raise AppError("IMAGE_SIZE_INVALID", "图片为空或超过20MiB", 422)
        # Decode user-selected bytes only; no path, video or screen acquisition capability.
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise AppError("IMAGE_INVALID", "仅支持PNG、JPEG、WebP和GIF图片", 422)
                if image.width * image.height > 64_000_000 or max(image.size) > 8192:
                    raise AppError("IMAGE_SIZE_INVALID", "图片尺寸过大", 422)
                expected_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}[
                    image.format
                ]
                if expected_type != value.media_type:
                    raise AppError("IMAGE_TYPE_MISMATCH", "图片类型与内容不一致", 422)
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise AppError("IMAGE_INVALID", "无法读取图片", 422) from exc
        result = app.state.repository.stage_attachment(data, value.media_type, confirmed=True)
        return {
            **result,
            "sha256": result["content_hash"],
            "size_bytes": result.get("byte_length", len(data)),
        }

    @app.get("/api/v1/attachments/{attachment_id}")
    async def read_attachment(attachment_id: str):
        attachment = app.state.repository.resolve_attachment(attachment_id)
        if not attachment["media_type"].startswith("image/") or attachment_metadata(
            app.state.repository, attachment_id
        )["kind"] == "file":
            return document_content(app.state.repository, attachment_id)
        return FileResponse(attachment["path"], media_type=attachment["media_type"])

    @app.post("/api/v1/submissions", response_model=SubmissionResponse)
    async def submit(value: SubmissionInput):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError(
                    "APPLICATION_EXIT_PREPARED", "应用正在安全退出，已保存内容仍保留", 409
                )
            return await app.state.service.submit(value.model_dump())

    @app.get("/api/v1/submissions/{submission_id}", response_model=SubmissionResponse)
    async def lookup_submission(submission_id: str):
        # Do not report absence while an admitted request is still preparing
        # its transaction under the shared submission/exit gate.
        async with app.state.submission_gate:
            return app.state.repository.get_submission(submission_id)

    @app.get("/api/v1/generations/{generation_id}", response_model=GenerationResponse)
    async def generation(generation_id: str):
        return app.state.service.generation_view(generation_id)

    @app.post("/api/v1/generations/{generation_id}/cancel", response_model=GenerationResponse)
    async def cancel(generation_id: str):
        await app.state.service.cancel(generation_id)
        return app.state.service.generation_view(generation_id)

    @app.post("/api/v1/generations/{generation_id}/reconcile", response_model=GenerationResponse)
    async def reconcile(generation_id: str):
        await app.state.service.reconcile(generation_id)
        return app.state.service.generation_view(generation_id)

    @app.websocket("/api/v1/ws")
    async def socket(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        token = websocket.cookies.get(COOKIE_NAME)
        protocols = websocket.scope.get("subprotocols", [])
        csrf = protocols[0].removeprefix("learning.") if protocols else ""
        if origin not in config.allowed_origins or not sessions.valid(token, csrf):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=protocols[0])
        try:
            while True:
                value = await websocket.receive_json()
                if set(value) != {"generation_id"}:
                    await websocket.close(code=1008)
                    return
                await websocket.send_json(app.state.service.generation_view(value["generation_id"]))
        except WebSocketDisconnect:
            pass

    install_account_routes(app)
    install_course_file_routes(app)
    install_media_routes(app)
    install_video_preview_routes(app)
    install_draft_routes(app)
    install_document_routes(app)
    install_runtime_routes(app)
    install_runtime_settings_routes(app)
    install_update_routes(app)
    install_data_routes(app)
    install_workbench_routes(app)
    install_reading_routes(app)
    install_desktop_routes(app, sessions)

    if config.mode == "start":
        index = config.static_dir / "index.html"
        if not index.is_file():
            raise AppError("BUILD_MISSING", "请先执行前端构建", 503)

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "接口不存在")
            candidate = (config.static_dir / path).resolve()
            if not candidate.is_relative_to(config.static_dir.resolve()):
                raise HTTPException(404, "页面不存在")
            if candidate.is_file():
                return FileResponse(candidate)
            if Path(path).suffix:
                raise HTTPException(404, "资源不存在")
            return FileResponse(index)

    return app
