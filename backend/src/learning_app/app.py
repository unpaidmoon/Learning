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
from learning_app.health import Capabilities, HealthResponse
from learning_app.media import install_media_routes
from learning_app.runtime_control import install_runtime_routes
from learning_app.security import COOKIE_NAME, BrowserSessions, LocalBoundaryMiddleware
from learning_app.storage import StorageError


def create_app(config: AppConfig | None = None, *, repository=None, execution=None) -> FastAPI:
    config = config or load_config()
    sessions = BrowserSessions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from learning_app.service import GenerationService
        from learning_app.storage import Repository

        app.state.repository = repository or Repository(config.data_dir)
        app.state.service = GenerationService(app.state.repository, config, execution=execution)
        await app.state.service.open()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await app.state.service.close()
            app.state.repository.close()

    app = FastAPI(
        title="Learning Application API",
        version=__version__,
        lifespan=lifespan,
        responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    app.state.config = config
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
        if path.startswith("/api/") and path not in {"/api/v1/health", "/api/v1/session"}:
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
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
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
            else (
                "unavailable"
                if harness_ready or config.harness_enabled
                else "not_configured"
            )
        )
        return HealthResponse(
            instance_id=config.instance_id,
            ready=ready,
            configured_model=config.model,
            configured_thinking=config.thinking,
            capabilities=Capabilities(
                persistence="ready" if ready else "unavailable",
                video="ready" if ready else "unavailable",
                harness=harness,
                question_answering=question_answering,
            ),
        )

    @app.get("/api/v1/session", response_model=SessionResponse)
    async def browser_session(request: Request, response: Response):
        token, csrf = sessions.create(request.cookies.get(COOKIE_NAME))
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", path="/api/")
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
        if not data or len(data) > 10_000_000:
            raise AppError("IMAGE_SIZE_INVALID", "图片为空或超过10MB", 422)
        # Decode user-selected bytes only; no path, video or screen acquisition capability.
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise AppError("IMAGE_INVALID", "仅支持PNG、JPEG和WebP图片", 422)
                if image.width * image.height > 40_000_000:
                    raise AppError("IMAGE_SIZE_INVALID", "图片尺寸过大", 422)
                expected_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[
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
    install_draft_routes(app)
    install_runtime_routes(app)

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
