"""Optional routes protected by the host browser session and CSRF boundary."""

from typing import Literal

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import Field

from learning_app.errors import AppError
from learning_app.workbench.models import Contract
from learning_app.workbench.processing_models import PreparationView

from .models import (
    AccountSourceRecovery,
    AnonymousSourceRecovery,
    CatalogueBrowse,
    CataloguePage,
    ConnectionInput,
    ConnectionView,
    ImportPlan,
    ImportReceipt,
    ImportSelection,
    LoginView,
    ModuleChange,
    ModuleView,
    Platform,
    RemoteAction,
    ResolveInput,
    SubtitleResult,
)


class Favorite(Contract):
    folder_id: str
    title: str
    count: int
    resource_type: Literal["folder", "collection"] = "folder"


class FavoriteVideo(Contract):
    resource_id: str
    bvid: str | None
    title: str
    available: bool
    unavailable_reason: Literal["removed", "unsupported", "identity_missing"] | None = None
    cover_path: str | None = None
    author: str | None = None
    duration_ms: int | None = None


class FavoritePage(Contract):
    items: list[FavoriteVideo]
    has_more: bool
    page: int
    total: int
    title: str
    folder_id: str
    kind: Literal["created", "collected"]


class FavoriteBrowse(Contract):
    connection_id: str
    folder_id: str = Field(pattern=r"^\d+$")
    page: int = Field(default=1, ge=1, le=10000)
    kind: Literal["created", "collected"] = "created"
    folder_page: int = Field(default=1, ge=1, le=10000)
    resource_type: Literal["folder", "collection"] = "folder"


class FavoriteFolderBrowse(Contract):
    connection_id: str
    kind: Literal["created", "collected"] = "created"
    page: int = Field(default=1, ge=1, le=10000)


class FavoriteFolderPage(Contract):
    items: list[Favorite]
    page: int
    kind: Literal["created", "collected"]
    total: int
    has_more: bool


def install_integration_routes(app: FastAPI):
    def host():
        return app.state.integrations

    @app.get("/api/v1/integrations/modules", response_model=list[ModuleView])
    def modules():
        return host().access.modules()

    @app.post("/api/v1/integrations/modules/{platform}", response_model=ModuleView)
    def module_change(platform: Platform, value: ModuleChange):
        result = host().access.change(platform, value.enabled)
        return result

    @app.post("/api/v1/integrations/bilibili/connect", response_model=ConnectionView)
    def connect(value: ConnectionInput):
        if not value.guest:
            raise AppError("BILI_LOGIN_REQUIRED", "请使用B站扫码连接", 409)
        return host().access.connect()

    @app.post("/api/v1/integrations/connections/{connection_id}/disconnect")
    def disconnect(connection_id: str):
        host().access.disconnect(connection_id, forget=lambda: host().private.remove(connection_id))
        return {"state": "disconnected"}

    @app.post("/api/v1/integrations/bilibili/login", response_model=LoginView)
    def login_start():
        from .bilibili.login import Login

        return Login(host()).start()

    @app.post("/api/v1/integrations/bilibili/login/{login_id}", response_model=LoginView)
    def login_poll(login_id: str):
        from .bilibili.login import Login

        return Login(host()).poll(login_id)

    @app.get("/api/v1/integrations/bilibili/login/{login_id}/qr.svg")
    def login_qr(login_id: str):
        from .bilibili.login import Login

        return Response(
            Login(host()).qr(login_id),
            media_type="image/svg+xml",
            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
        )

    @app.get("/api/v1/integrations/bilibili/favorites", response_model=list[Favorite])
    def favorites(connection_id: str):
        from .bilibili.favorites import Favorites

        return Favorites(host()).folders(connection_id)

    @app.post("/api/v1/integrations/bilibili/favorites/browse", response_model=FavoritePage)
    def favorite_browse(value: FavoriteBrowse):
        from .bilibili.favorites import Favorites

        return Favorites(host()).browse(value)

    @app.post("/api/v1/integrations/bilibili/favorites/folders", response_model=FavoriteFolderPage)
    def favorite_folders(value: FavoriteFolderBrowse):
        from .bilibili.favorites import Favorites

        return Favorites(host()).folder_page(value)

    @app.get("/api/v1/integrations/bilibili/covers/{preview_id}", include_in_schema=False)
    def preview_cover(preview_id: str):
        return host().covers.preview_response(preview_id)

    @app.post(
        "/api/v1/courses/{course_id}/integrations/bilibili/resolve", response_model=ImportPlan
    )
    def resolve(course_id: str, value: ResolveInput):
        return host().resolve(course_id, value)

    @app.post(
        "/api/v1/courses/{course_id}/integrations/bilibili/inspect",
        response_model=ImportPlan | CataloguePage,
    )
    def inspect_link(course_id: str, value: CatalogueBrowse):
        return host().inspect(course_id, value)

    @app.post("/api/v1/courses/{course_id}/integrations/import", response_model=ImportReceipt)
    def import_selected(course_id: str, value: ImportSelection):
        return host().import_selected(course_id, value)

    @app.post(
        "/api/v1/courses/{course_id}/integrations/{asset_id}/reconnect",
        response_model=AnonymousSourceRecovery | AccountSourceRecovery,
    )
    def reconnect_source(course_id: str, asset_id: str, value: RemoteAction):
        from .recovery import SourceRecovery

        return SourceRecovery(host()).begin(course_id, asset_id, value)

    @app.post(
        "/api/v1/courses/{course_id}/integrations/{asset_id}/subtitles",
        response_model=SubtitleResult,
    )
    def subtitles(course_id: str, asset_id: str, value: RemoteAction):
        return host().subtitles(course_id, asset_id, value.source_version)

    @app.get("/api/v1/courses/{course_id}/integrations/{asset_id}/cover", include_in_schema=False)
    def cover(course_id: str, asset_id: str, source_version: str):
        return host().covers.response(course_id, asset_id, source_version)

    @app.post(
        "/api/v1/courses/{course_id}/integrations/{asset_id}/download",
        response_model=PreparationView,
    )
    def download(course_id: str, asset_id: str, value: RemoteAction):
        result = host().queue(course_id, asset_id, value.source_version, "platform_download")
        app.state.preparations.wake()
        return result

    @app.post(
        "/api/v1/courses/{course_id}/integrations/{asset_id}/prepare-subtitles",
        response_model=PreparationView,
    )
    def prepare_subtitles(course_id: str, asset_id: str, value: RemoteAction):
        result = host().queue(course_id, asset_id, value.source_version, "platform_subtitles")
        app.state.preparations.wake()
        return result
