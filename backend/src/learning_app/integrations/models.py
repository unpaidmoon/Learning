from typing import Literal

from pydantic import Field

from learning_app.workbench.models import Contract, Identity

Platform = Literal[
    "bilibili", "chaoxing", "maimemo", "zotero", "icourse", "rainclassroom", "xuetang", "zhihuishu"
]


class ModuleView(Contract):
    platform: Platform
    enabled: bool
    implemented: bool
    connection_id: str | None = None
    account_label: str | None = None
    connection_state: Literal["disconnected", "guest", "connected", "expired"]


class ModuleChange(Contract):
    enabled: bool


class ConnectionView(Contract):
    connection_id: Identity
    account_label: str
    state: Literal["guest", "connected", "expired", "disconnected"]


class ConnectionInput(Contract):
    guest: bool = True


class FavoriteOrigin(Contract):
    folder_id: str = Field(pattern=r"^[1-9]\d*$")
    kind: Literal["created", "collected"]
    resource_type: Literal["folder", "collection"]
    folder_page: int = Field(ge=1, le=10000)
    video_page: int = Field(ge=1, le=10000)


class ResolveInput(Contract):
    connection_id: Identity
    url: str = Field(min_length=1, max_length=2048)
    catalogue_id: Identity | None = None
    favorite_origin: FavoriteOrigin | None = None


class NativeBilibiliQuery(Contract):
    operation: Literal["links", "inspect", "tasks"]
    handle_id: Identity | None
    page: int = Field(ge=1, le=10000)


class CatalogueBrowse(ResolveInput):
    page: int = Field(default=1, ge=1, le=10000)


class CatalogueVideo(Contract):
    bvid: str
    title: str
    ordinal: int = Field(ge=1)
    cover_path: str | None = None
    author: str | None = None
    duration_ms: int | None = None


class CataloguePage(Contract):
    kind: Literal["catalogue"] = "catalogue"
    catalogue_id: Identity
    course_id: Identity
    connection_id: Identity
    catalogue_kind: Literal["collection", "series"]
    title: str
    url: str
    page: int
    total: int
    has_more: bool
    provider_revision: str | None
    videos: list[CatalogueVideo]


class VideoPart(Contract):
    object_id: str
    title: str
    page: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    availability: Literal["full", "restricted", "preview", "unknown"] = "unknown"


class ImportPlan(Contract):
    kind: Literal["video"] = "video"
    plan_id: Identity
    course_id: Identity
    connection_id: Identity
    title: str
    parts: list[VideoPart]
    complete: bool
    cover_path: str | None = None
    author: str | None = None


class ImportSelection(Contract):
    destination_path: str = Field(default="", max_length=32760)
    operation_id: Identity
    plan_id: Identity
    object_ids: list[str] = Field(min_length=1, max_length=1000)


class ImportedItem(Contract):
    asset_id: Identity
    source_version: Identity
    path: str
    object_id: str
    reused: bool = False


class ImportReceipt(Contract):
    operation_id: Identity
    course_id: Identity
    state: Literal["completed", "unknown"]
    items: list[ImportedItem]


class RemoteAction(Contract):
    operation_id: Identity
    source_version: Identity


class SubtitleResult(Contract):
    state: Literal["available", "absent", "login_required", "failed", "unknown"]
    track_ids: list[str] = []
    error_code: str | None = None


class LoginView(Contract):
    login_id: Identity
    url: str | None = None
    state: Literal["waiting", "scanned", "expired", "connected"]
    connection: ConnectionView | None = None


class AnonymousSourceRecovery(Contract):
    kind: Literal["anonymous"] = "anonymous"
    connection: ConnectionView


class AccountSourceRecovery(Contract):
    kind: Literal["login"] = "login"
    login: LoginView
    account_label: str
