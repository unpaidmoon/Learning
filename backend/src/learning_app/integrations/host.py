"""Narrow host adapter. No direct writes to the host's source/task/media tables."""

import json
import re
import threading
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert, select, update

from learning_app.course_files import CourseDirectoryInput, CourseFileStore, CourseImportInput
from learning_app.errors import AppError
from learning_app.storage import schema as business

from . import schema as s
from .access import Access, Grant
from .common import content_hash as _hash
from .models import CataloguePage, ImportedItem, ImportPlan, ImportReceipt, VideoPart
from .private import PrivateStore


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:100]
    if value.upper().split(".")[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        value = "_" + value
    return value or "B站视频"


class IntegrationHost:
    def __init__(self, repository, project_root, *, provider_factory=None):
        self.repository, self.project_root = repository, project_root
        self.access = Access(repository, self.pause_remote)
        self.private = PrivateStore(repository.root)
        self.provider_factory = provider_factory
        self.lock = threading.RLock()
        self._provider = None
        self._media = None
        self._covers = None
        self._native = None

    @property
    def native(self):
        if self._native is None:
            from .native import NativeBilibili

            self._native = NativeBilibili(self)
        return self._native

    def pause_remote(self, connection_id):
        from learning_app.workbench.preparations import PreparationStore
        from learning_app.workbench.processing_models import PreparationControl

        store = PreparationStore(self.repository)
        for job in store.active():
            if not job["kind"].startswith("platform_"):
                continue
            view = store.get(job["course_id"], job["job_id"])
            if connection_id is None or view.parameters["connection_id"] == connection_id:
                try:
                    store.control(
                        job["course_id"],
                        job["job_id"],
                        "pause",
                        PreparationControl(
                            operation_id="revoke_" + uuid4().hex, expected_revision=view.revision
                        ),
                    )
                except AppError as exc:
                    if exc.code not in {"PREPARATION_CLOSED", "PREPARATION_REVISION_CONFLICT"}:
                        raise
        if self._media:
            self._media.revoke(connection_id)

    @property
    def provider(self):
        self.access.require_enabled()
        if self._provider is None:
            if self.provider_factory:
                self._provider = self.provider_factory(self.access, self.private, self.project_root)
            else:
                from .bilibili.provider import BilibiliProvider

                self._provider = BilibiliProvider(self.access, self.private, self.project_root)
        return self._provider

    @property
    def covers(self):
        if self._covers is None:
            from .covers import CoverCache

            self._covers = CoverCache(self)
        return self._covers

    @property
    def media(self):
        if self._media is None:
            from .media import RemoteMedia

            self._media = RemoteMedia(self)
        return self._media

    def course(self, course_id):
        with self.repository.engine.connect() as conn:
            if not conn.execute(
                select(business.courses.c.course_id).where(
                    business.courses.c.course_id == course_id
                )
            ).first():
                raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)

    def resolve(self, course_id, value):
        self.course(course_id)
        grant = self.access.grant(value.connection_id)
        collection = None
        if value.favorite_origin:
            if value.catalogue_id:
                raise AppError("BILI_IMPORT_ORIGIN_AMBIGUOUS", "请只使用一个明确的目录来源", 422)
            from .bilibili.favorites import Favorites

            collection = Favorites(self).selection(
                value.connection_id, value.favorite_origin, value.url, grant
            )
        if value.catalogue_id:
            from .bilibili.provider import video_url

            parent = self._catalogue_scope(course_id, value.connection_id, value.catalogue_id)
            bvid, _ = video_url(value.url)
            video = next((v for v in parent["payload"]["videos"] if v["bvid"] == bvid), None)
            if video is None:
                raise AppError(
                    "BILI_CATALOGUE_VIDEO_DENIED", "视频不属于所选目录页，未扩大解析范围", 403
                )
            collection = {
                key: parent["payload"][key]
                for key in ["group_id", "title", "catalogue_kind", "url", "provider_revision"]
            }
            collection.update(catalogue_id=value.catalogue_id, ordinal=video["ordinal"])
        payload = self.provider.resolve(grant, value.url)
        if collection:
            payload = {
                **payload,
                "collection": collection,
                "parts": [{**part, "collection": collection} for part in payload["parts"]],
            }
        self.access.check(grant)
        identity = self._save_plan(course_id, grant, payload)
        return ImportPlan(
            plan_id=identity,
            course_id=course_id,
            connection_id=grant.connection_id,
            title=payload["title"],
            parts=[
                VideoPart.model_validate({k: p[k] for k in VideoPart.model_fields if k in p})
                for p in payload["parts"]
            ],
            complete=payload["complete"],
            cover_path=self.covers.preview(grant, payload["parts"][0]),
            author=payload.get("author"),
        )

    def _save_plan(self, course_id, grant, payload):
        identity = "plan_" + uuid4().hex
        with self.repository.engine.begin() as conn:
            conn.execute(
                insert(s.plans).values(
                    plan_id=identity,
                    course_id=course_id,
                    connection_id=grant.connection_id,
                    module_epoch=grant.module_epoch,
                    connection_epoch=grant.connection_epoch,
                    payload=payload,
                )
            )
        return identity

    def _catalogue_scope(self, course_id, connection_id, catalogue_id):
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.plans).where(
                        s.plans.c.plan_id == catalogue_id,
                        s.plans.c.course_id == course_id,
                        s.plans.c.connection_id == connection_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["payload"].get("kind") != "catalogue":
            raise AppError(
                "BILI_CATALOGUE_SCOPE_DENIED", "目录页不属于当前课程和连接，请重新读取", 403
            )
        self.access.check(Grant(row["connection_id"], row["module_epoch"], row["connection_epoch"]))
        return row

    def inspect(self, course_id, value):
        from .bilibili.catalogue import is_catalogue_url

        if not is_catalogue_url(value.url):
            return self.resolve(course_id, value)
        return self.browse_catalogue(course_id, value)

    def browse_catalogue(self, course_id, value):
        from .bilibili.catalogue import browse_catalogue, catalogue_link

        self.course(course_id)
        locator = catalogue_link(value.url)
        grant = self.access.grant(value.connection_id)
        if value.catalogue_id:
            parent = self._catalogue_scope(course_id, value.connection_id, value.catalogue_id)
            if parent["payload"]["group_id"] != locator.group_id:
                raise AppError("BILI_CATALOGUE_SCOPE_DENIED", "翻页不能改变原目录身份", 403)
        payload = browse_catalogue(self.provider, grant, locator, value.page)
        self.access.check(grant)
        identity = self._save_plan(course_id, grant, payload)
        videos = [
            {
                **{k: v for k, v in video.items() if k != "cover_url"},
                "cover_path": self.covers.preview(grant, video),
            }
            for video in payload["videos"]
        ]
        return CataloguePage.model_validate(
            {
                **{k: v for k, v in payload.items() if k in CataloguePage.model_fields},
                "catalogue_id": identity,
                "course_id": course_id,
                "connection_id": grant.connection_id,
                "videos": videos,
            }
        )

    def _record_collection(self, asset_id, collection):
        if not collection:
            return
        with self.repository.engine.begin() as conn:
            row = (
                conn.execute(select(s.sources).where(s.sources.c.asset_id == asset_id))
                .mappings()
                .one()
            )
            payload = row["payload"]
            relations = payload.get(
                "collections", [payload["collection"]] if payload.get("collection") else []
            )
            keys = ["group_id", "ordinal", "provider_revision", "title"]
            if any(
                all(prior.get(key) == collection.get(key) for key in keys) for prior in relations
            ):
                return
            conn.execute(
                update(s.sources)
                .where(s.sources.c.asset_id == asset_id)
                .values(payload={**payload, "collections": [*relations, collection]})
            )

    def source(self, asset_id, version=None, course_id=None, *, connection=None):
        with (
            self.repository.engine.connect() if connection is None else nullcontext(connection)
        ) as conn:
            row = (
                conn.execute(select(s.sources).where(s.sources.c.asset_id == asset_id))
                .mappings()
                .first()
            )
        if row is None:
            return None
        if version is not None and row["source_version"] != version:
            raise AppError("SOURCE_VERSION_MISMATCH", "原平台来源版本不符", 409)
        if course_id is not None and row["course_id"] != course_id:
            raise AppError("SOURCE_COURSE_DENIED", "平台资料不属于当前课程", 403)
        return dict(row)

    def import_selected(self, course_id, value):
        with self.lock:
            with self.repository.engine.connect() as conn:
                plan = (
                    conn.execute(
                        select(s.plans).where(
                            s.plans.c.plan_id == value.plan_id, s.plans.c.course_id == course_id
                        )
                    )
                    .mappings()
                    .first()
                )
                previous = (
                    conn.execute(
                        select(s.operations).where(
                            s.operations.c.operation_id == value.operation_id
                        )
                    )
                    .mappings()
                    .first()
                )
            if not plan:
                raise AppError("IMPORT_PLAN_NOT_FOUND", "当前课程没有此导入计划", 404)
            if plan["payload"].get("kind") == "catalogue":
                raise AppError("BILI_PART_SELECTION_REQUIRED", "先选视频并读取分P，再确认导入", 422)
            payload = value.model_dump()
            digest = _hash(payload)
            if previous:
                if previous["course_id"] != course_id or previous["request_hash"] != digest:
                    raise AppError("IMPORT_OPERATION_CONFLICT", "相同操作身份绑定了其他范围", 409)
                if previous["state"] == "completed":
                    return ImportReceipt.model_validate(previous["receipt"])
                # Existing filesystem receipts are reconciled; never run an unknown file copy again.
            grant = Grant(plan["connection_id"], plan["module_epoch"], plan["connection_epoch"])
            self.access.check(grant)
            selected = set(value.object_ids)
            parts = [p for p in plan["payload"]["parts"] if p["object_id"] in selected]
            if len(selected) != len(value.object_ids) or len(parts) != len(selected):
                raise AppError("IMPORT_SELECTION_INVALID", "只能手选原计划中的具体分P", 422)
            if not previous:
                with self.repository.engine.begin() as conn:
                    conn.execute(
                        insert(s.operations).values(
                            operation_id=value.operation_id,
                            course_id=course_id,
                            connection_id=grant.connection_id,
                            kind="import",
                            request_hash=digest,
                            state="unknown",
                        )
                    )
            files = CourseFileStore(self.repository)
            destination = value.destination_path
            files.list(course_id, destination)
            with self.repository.engine.connect() as conn:
                known_objects = set(
                    conn.execute(
                        select(s.sources.c.object_id).where(
                            s.sources.c.course_id == course_id,
                            s.sources.c.connection_id == grant.connection_id,
                        )
                    ).scalars()
                )
            collection = plan["payload"].get("collection")
            if (plan["payload"].get("multipart") or collection) and not selected.issubset(
                known_objects
            ):
                if collection:
                    parent_name = (
                        safe_name(collection["title"])
                        + "-"
                        + _hash(
                            {"group": collection["group_id"], "connection": grant.connection_id}
                        )[:8]
                    )
                    parent = files.directory(
                        course_id,
                        CourseDirectoryInput(
                            operation_id="collection_"
                            + _hash(
                                {
                                    "course": course_id,
                                    "connection": grant.connection_id,
                                    "name": parent_name,
                                    "path": destination,
                                }
                            )[:40],
                            path=destination,
                            name=parent_name,
                        ),
                    )
                    destination = parent["entry"]["path"]
                folder = (
                    safe_name(plan["payload"]["title"])
                    + "-"
                    + _hash(
                        {
                            "group": plan["payload"].get(
                                "group_id", plan["payload"]["parts"][0].get("bvid")
                            ),
                            "connection": grant.connection_id,
                        }
                    )[:8]
                )
                if collection:
                    folder = f"{collection['ordinal']:06d} " + folder
                folder_op = (
                    "folder_"
                    + _hash(
                        {
                            "course": course_id,
                            "connection": grant.connection_id,
                            "folder": folder,
                            **({"parent": destination} if destination else {}),
                        }
                    )[:40]
                )
                result = files.directory(
                    course_id,
                    CourseDirectoryInput(operation_id=folder_op, path=destination, name=folder),
                )
                destination = result["entry"]["path"]
            items = []
            for part in parts:
                self.access.check(grant)
                with self.repository.engine.connect() as conn:
                    existing = (
                        conn.execute(
                            select(s.sources).where(
                                s.sources.c.course_id == course_id,
                                s.sources.c.connection_id == grant.connection_id,
                                s.sources.c.object_id == part["object_id"],
                            )
                        )
                        .mappings()
                        .first()
                    )
                if existing:
                    source = self.repository.resolve_source(
                        existing["asset_id"], existing["source_version"]
                    )
                    root = Path(files.folder(course_id)["root_path"])
                    files.media.attach_remote_source(source)
                    self._record_collection(existing["asset_id"], collection)
                    items.append(
                        ImportedItem(
                            asset_id=existing["asset_id"],
                            source_version=existing["source_version"],
                            path=Path(source["local_path"]).relative_to(root).as_posix(),
                            object_id=part["object_id"],
                            reused=True,
                        )
                    )
                    continue
                identity = _hash(
                    {
                        "course": course_id,
                        "connection": grant.connection_id,
                        "object": part["object_id"],
                    }
                )
                staging = self.repository.root / "integration-staging" / identity
                staging.mkdir(parents=True, exist_ok=True)
                descriptor = staging / (f"{part['page']:03d} {safe_name(part['title'])}.bili")
                content = json.dumps(
                    {
                        "platform": "bilibili",
                        "connection_id": grant.connection_id,
                        "object_id": part["object_id"],
                        "part": part,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
                if not descriptor.exists():
                    descriptor.write_bytes(content)
                elif descriptor.read_bytes() != content:
                    raise AppError(
                        "IMPORT_SOURCE_CHANGED", "相同远端对象的元数据已变化，请核对原版本", 409
                    )
                copy_op = "bili_import_" + identity[:40]
                try:
                    old = files.operation(course_id, copy_op)
                except AppError as exc:
                    if exc.code != "FILE_OPERATION_NOT_FOUND":
                        raise
                    old = None
                if old:
                    if old["state"] != "completed":
                        return ImportReceipt(
                            operation_id=value.operation_id,
                            course_id=course_id,
                            state="unknown",
                            items=items,
                        )
                    result = old["result"]
                else:
                    result = files.import_files(
                        course_id,
                        CourseImportInput(
                            operation_id=copy_op, paths=[str(descriptor)], destination=destination
                        ),
                    )
                entry = result["entries"][0]
                source = self.repository.resolve_source(entry["asset_id"], entry["source_version"])
                with self.repository.engine.begin() as conn:
                    conn.execute(
                        insert(s.sources).values(
                            asset_id=entry["asset_id"],
                            source_version=entry["source_version"],
                            course_id=course_id,
                            connection_id=grant.connection_id,
                            object_id=part["object_id"],
                            payload=part,
                        )
                    )
                files.media.attach_remote_source(source)
                items.append(
                    ImportedItem(
                        asset_id=entry["asset_id"],
                        source_version=entry["source_version"],
                        path=entry["path"],
                        object_id=part["object_id"],
                    )
                )
            receipt = ImportReceipt(
                operation_id=value.operation_id, course_id=course_id, state="completed", items=items
            )
            with self.repository.engine.begin() as conn:
                conn.execute(
                    update(s.operations)
                    .where(s.operations.c.operation_id == value.operation_id)
                    .values(state="completed", receipt=receipt.model_dump())
                )
            return receipt

    def subtitles(self, course_id, asset_id, version, cancelled=None):
        from learning_app.workbench.transcripts import TranscriptStore

        from .models import SubtitleResult

        row = self.source(asset_id, version, course_id)
        if not row:
            raise AppError("PLATFORM_SOURCE_NOT_FOUND", "平台来源不存在", 404)
        grant = self.access.grant(row["connection_id"])
        grant = self.access.scoped(grant, cancelled)
        result = self.provider.subtitle(grant, row["payload"])
        # Already received data remains bound to its original connection, including after disable.
        tracks = [
            TranscriptStore(self.repository).publish_platform(
                course_id,
                asset_id,
                version,
                t["language"],
                t["origin"],
                t["segments"],
                row["payload"]["duration_ms"],
            )
            for t in result["tracks"]
        ]
        return SubtitleResult(state=result["state"], track_ids=tracks)

    def queue(self, course_id, asset_id, version, kind):
        from learning_app.workbench.preparations import PreparationStore

        row = self.source(asset_id, version, course_id)
        if not row:
            raise AppError("PLATFORM_SOURCE_NOT_FOUND", "平台来源不存在", 404)
        self.access.grant(row["connection_id"])
        source = self.repository.resolve_source(asset_id, version)
        return PreparationStore(self.repository).publish_pdf(
            {
                "payload": dict(
                    course_id=course_id,
                    asset_id=asset_id,
                    source_version=version,
                    source_digest=source["content_hash"],
                    kind=kind,
                    scope="full",
                    parameters={
                        "connection_id": row["connection_id"],
                        "user_requested": True,
                        "scope_indexes": [0],
                        "source_title": row["payload"]["title"],
                    },
                ),
                "priority_unit": 0 if kind == "platform_subtitles" else None,
            }
        )

    def prepare(self, lease, cancelled, progress):
        row = self.source(lease["asset_id"], lease["source_version"], lease["course_id"])
        if row["connection_id"] != lease["parameters"]["connection_id"]:
            raise AppError("PLATFORM_TASK_SCOPE_CONFLICT", "旧任务不能改绑账号", 409)
        if cancelled.is_set():
            raise AppError("PREPARATION_INTERRUPTED", "处理已暂停", 409)
        copy = None
        if lease["kind"] == "platform_download":
            copy = self.media.download(lease, cancelled, progress)
        elif lease["kind"] == "platform_subtitles":
            result = self.subtitles(
                lease["course_id"], lease["asset_id"], lease["source_version"], cancelled
            )
            if result.state == "login_required":
                raise AppError(
                    "BILI_SUBTITLE_LOGIN_REQUIRED", "平台字幕需要连接B站账号；未当作无字幕", 409
                )
            if result.state in {"failed", "unknown"}:
                raise AppError("BILI_SUBTITLE_UNKNOWN", "平台字幕状态未知，未自动获取音频", 409)
            if result.state == "absent":
                self.media.prepare_audio(lease, cancelled, progress)
                from learning_app.workbench.preparations import PreparationStore
                from learning_app.workbench.processing_models import PreparationCreate

                store = PreparationStore(self.repository)
                store.publish_pdf(
                    store.define_asr(
                        lease["course_id"],
                        PreparationCreate(
                            kind="asr",
                            asset_id=lease["asset_id"],
                            source_version=lease["source_version"],
                        ),
                        self.project_root,
                        explicit=True,
                    )
                )
        else:
            raise AppError("PLATFORM_TASK_KIND_DENIED", "平台任务类型未获准", 403)
        return {
            "source_digest": lease["source_digest"],
            "unit_index": lease["unit_index"],
            "kind": lease["kind"],
            "copy": copy,
        }

    def publish_result(self, conn, lease, result):
        """Called only inside the host's live-lease completion transaction."""
        if result["kind"] != "platform_download":
            return
        artifact = result["copy"]
        path = Path(artifact["path"])
        if not path.is_file() or not path.resolve().is_relative_to(self.repository.root):
            raise AppError("PLATFORM_COPY_PATH_DENIED", "本地副本不在所属数据根内", 409)
        row = (
            conn.execute(
                select(s.sources).where(
                    s.sources.c.asset_id == lease["asset_id"],
                    s.sources.c.source_version == lease["source_version"],
                    s.sources.c.course_id == lease["course_id"],
                    s.sources.c.connection_id == lease["parameters"]["connection_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AppError("PLATFORM_TASK_SCOPE_CONFLICT", "副本发布范围与原任务不同", 409)
        if row["local_sha256"] and row["local_sha256"] != artifact["sha256"]:
            raise AppError("PLATFORM_COPY_VERSION_CONFLICT", "原本地副本版本不同，未覆盖", 409)
        conn.execute(
            update(s.sources)
            .where(s.sources.c.asset_id == lease["asset_id"])
            .values(local_path=str(path), local_sha256=artifact["sha256"])
        )
