"""Freeze actual source ranges before claiming full-task coverage."""

from __future__ import annotations

from sqlalchemy import insert, select

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.storage.repository import _hash

from . import schema as s
from .artifacts import ArtifactStore
from .common import now
from .models import (
    ArtifactRangeInput,
    DocumentGapRef,
    DocumentRangeInput,
    DocumentRef,
    ManifestCreate,
    SourceManifest,
    TaskUnitInput,
)
from .pdf_worker import PARSER_VERSION
from .sources import SourceStore


class ManifestStore:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.sources = SourceStore(repository)

    def create(self, course_id: str, value: ManifestCreate) -> SourceManifest:
        ranges = {}
        for item in value.documents:
            key = (item.asset_id, item.source_version)
            low, high = (
                item.first_page,
                item.last_page if item.last_page is not None else float("inf"),
            )
            if any(low <= end and begin <= high for begin, end in ranges.get(key, [])):
                raise AppError("SOURCE_RANGE_OVERLAP", "同一原版本的范围存在重叠，请合并范围", 422)
            ranges.setdefault(key, []).append((low, high))
        documents, artifacts, units = [], [], []
        for requested in value.documents:
            source = self.sources._source(course_id, requested.asset_id, requested.source_version)
            if source["path"].suffix.lower() == ".pdf":
                from .reading import DocumentReader

                reader = DocumentReader(self.repository)
                info = reader.info(course_id, requested.asset_id, requested.source_version)
                last = (
                    requested.last_page if requested.last_page is not None else info.page_count - 1
                )
                if requested.first_page > last or last >= info.page_count:
                    raise AppError("SOURCE_PAGE_RANGE_INVALID", "范围不在实际PDF页数内", 422)
                if len(units) + last - requested.first_page + 1 > 20_000:
                    raise AppError("SOURCE_UNIT_LIMIT", "指定范围超出单任务单位上限，未截断")
                documents.append(requested.model_copy(update={"last_page": last}))
                for index in range(requested.first_page, last + 1):
                    label = str(index + 1)
                    try:
                        page = reader.page(
                            course_id, requested.asset_id, requested.source_version, index
                        )
                        label = page.page_label
                        reference = DocumentRef(
                            asset_id=requested.asset_id,
                            source_version=requested.source_version,
                            source_digest=page.source_digest,
                            page_index=index,
                            page_label=page.page_label,
                            start=0,
                            end=len(page.text),
                            parser_version=page.parser_version,
                        )
                        text = page.text
                    except AppError as exc:
                        if exc.code in {"SOURCE_COURSE_DENIED", "SOURCE_KIND_DENIED"}:
                            raise
                        reference = DocumentGapRef(
                            asset_id=requested.asset_id,
                            source_version=requested.source_version,
                            source_digest=source["content_hash"],
                            page_index=index,
                            error_code=exc.code,
                            parser_version=PARSER_VERSION,
                        )
                        text = ""
                    snapshot = self.sources._save_verified(course_id, reference, text, "original")
                    units.append(
                        TaskUnitInput(
                            unit_id=f"unit_{_hash(snapshot.reference.model_dump(mode='json'))}",
                            label=f"{source['path'].name} · 第{label}页",
                            source_snapshot_id=snapshot.snapshot_id,
                        )
                    )
                continue
            text, source = self.sources.text(
                course_id, requested.asset_id, requested.source_version
            )
            if requested.first_page != 0 or requested.last_page not in {None, 0}:
                raise AppError("SOURCE_PAGE_RANGE_INVALID", "文本资料只有正文单位0", 422)
            if not text:
                raise AppError("SOURCE_EMPTY", "指定正文为空，无法作为已处理全文")
            snapshot = self.sources.capture_text(
                course_id, requested.asset_id, requested.source_version, 0, len(text)
            )
            documents.append(
                DocumentRangeInput(
                    asset_id=requested.asset_id,
                    source_version=requested.source_version,
                    first_page=0,
                    last_page=0,
                )
            )
            units.append(
                TaskUnitInput(
                    unit_id=f"unit_{_hash(snapshot.reference.model_dump(mode='json'))}",
                    label=source["path"].name,
                    source_snapshot_id=snapshot.snapshot_id,
                )
            )
        store = ArtifactStore(self.repository)
        for requested in value.artifacts:
            artifact = store.get(course_id, requested.artifact_id)
            if artifact.head_revision_id != requested.revision_id:
                raise AppError("SOURCE_REVISION_CHANGED", "成果已有新修订，旧范围不会改指新版")
            existing = {block.block_id: block for block in artifact.revision.body.blocks}
            selected = requested.block_ids or list(existing)
            if len(set(selected)) != len(selected) or any(
                identity not in existing for identity in selected
            ):
                raise AppError("SOURCE_BLOCK_RANGE_INVALID", "成果内容范围不符", 422)
            artifacts.append(
                ArtifactRangeInput(
                    artifact_id=requested.artifact_id,
                    revision_id=requested.revision_id,
                    block_ids=selected,
                )
            )
            for block_id in selected:
                snapshot = self.sources.capture_artifact(
                    course_id, requested.artifact_id, requested.revision_id, block_id
                )
                units.append(
                    TaskUnitInput(
                        unit_id=f"unit_{_hash(snapshot.reference.model_dump(mode='json'))}",
                        label=f"{artifact.title} · {block_id}",
                        source_snapshot_id=snapshot.snapshot_id,
                    )
                )
        if len(units) > 20_000:
            raise AppError("SOURCE_UNIT_LIMIT", "指定范围超出单任务单位上限，未截断")
        payload = {
            "course_id": course_id,
            "documents": [item.model_dump(mode="json") for item in documents],
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "units": [item.model_dump(mode="json") for item in units],
        }
        digest = _hash(payload)
        manifest = SourceManifest(
            manifest_id=f"scope_{digest}", scope_hash=digest, created_at=now(), **payload
        )
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = (
                conn.execute(
                    select(s.source_manifests).where(
                        s.source_manifests.c.manifest_id == manifest.manifest_id
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                return SourceManifest.model_validate(dict(prior))
            conn.execute(insert(s.source_manifests).values(**manifest.model_dump(mode="json")))
        return manifest

    def get(self, course_id: str, manifest_id: str) -> SourceManifest:
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.source_manifests).where(
                        s.source_manifests.c.manifest_id == manifest_id,
                        s.source_manifests.c.course_id == course_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("SOURCE_MANIFEST_DENIED", "范围清单不存在或不属于指定课程", 403)
        manifest = SourceManifest.model_validate(dict(row))
        payload = manifest.model_dump(
            exclude={"manifest_id", "scope_hash", "created_at"}, mode="json"
        )
        if _hash(payload) != manifest.scope_hash:
            raise AppError("SOURCE_MANIFEST_CORRUPT", "范围清单完整性不符")
        return manifest
