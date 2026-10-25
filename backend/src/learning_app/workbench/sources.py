"""Immutable exact selections. Source lookup never authorizes video images."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.media import _read_locked
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .common import now
from .models import (
    AnswerRef,
    ArtifactBody,
    ArtifactRef,
    DocumentGapRef,
    DocumentImageRef,
    DocumentRef,
    DocumentRegionImageRef,
    DocumentSelectionRef,
    MessageRef,
    SnapshotSliceRef,
    SourceRef,
    SourceSnapshot,
    SubtitleRef,
    WebEvidenceRef,
    block_text,
)

reference_adapter = TypeAdapter(SourceRef)
TEXT_PARSER_VERSION = "utf8-text-v1"


class SourceStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _source(self, course_id: str, asset_id: str, version: str) -> dict:
        with self.repository.engine.connect() as conn:
            associated = conn.execute(
                select(business.course_assets).where(
                    business.course_assets.c.course_id == course_id,
                    business.course_assets.c.asset_id == asset_id,
                )
            ).first()
            if associated is None:
                raise AppError("SOURCE_COURSE_DENIED", "来源不属于已指定课程", 403)
        source = self.repository.resolve_source(asset_id, version)
        if source["status"] != "available":
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "原版本已缺失或改变，保留快照且不改指新版")
        files = CourseFileStore(self.repository)
        root = files._root(course_id)
        path = Path(source["local_path"])
        if not path.is_relative_to(root):
            raise AppError("SOURCE_COURSE_DENIED", "来源不在指定课程根内", 403)
        checked = files._path(root, path.relative_to(root).as_posix())
        if checked.suffix.lower() not in {".txt", ".md", ".markdown", ".pdf"}:
            raise AppError("SOURCE_KIND_DENIED", "该入口仅允许课程文档，不读取视频画面", 403)
        return {**source, "path": checked}

    def text(self, course_id: str, asset_id: str, version: str) -> tuple[str, dict]:
        source = self._source(course_id, asset_id, version)
        if source["path"].suffix.lower() == ".pdf":
            raise AppError("SOURCE_PAGE_REQUIRED", "PDF须通过指定页读取")
        with _read_locked(source["path"]) as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise AppError("SOURCE_TEXT_SIZE_LIMIT", "文本超过当前读取上限，未截断")
            if hashlib.sha256(data).hexdigest() != source["content_hash"]:
                raise AppError("SOURCE_VERSION_UNAVAILABLE", "读取时来源发生变化")
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise AppError("SOURCE_ENCODING_UNSUPPORTED", "文本须为UTF-8编码", 422) from exc
        if "\x00" in text:
            raise AppError("SOURCE_TEXT_INVALID", "文本包含二进制内容", 422)
        return text, source

    def capture_text(
        self,
        course_id: str,
        asset_id: str,
        version: str,
        start: int,
        end: int,
    ) -> SourceSnapshot:
        text, source = self.text(course_id, asset_id, version)
        if not 0 <= start < end <= len(text):
            raise AppError("SELECTION_RANGE_INVALID", "选区不在实际原文范围内", 422)
        reference = DocumentRef(
            asset_id=asset_id,
            source_version=version,
            source_digest=source["content_hash"],
            page_index=0,
            start=start,
            end=end,
            parser_version=TEXT_PARSER_VERSION,
        )
        return self._save_verified(course_id, reference, text[start:end], "original")

    def capture_artifact(
        self,
        course_id: str,
        artifact_id: str,
        revision_id: str,
        block_id: str,
    ) -> SourceSnapshot:
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.revisions, s.artifacts.c.course_id)
                    .join(
                        s.artifacts,
                        s.artifacts.c.artifact_id == s.revisions.c.artifact_id,
                    )
                    .where(
                        s.revisions.c.revision_id == revision_id,
                        s.revisions.c.artifact_id == artifact_id,
                        s.artifacts.c.course_id == course_id,
                        s.revisions.c.disposition == "published",
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("SOURCE_ARTIFACT_NOT_FOUND", "指定课程的成果修订不存在", 404)
        body = ArtifactBody.model_validate(row["body"])
        block = next((block for block in body.blocks if block.block_id == block_id), None)
        if block is None:
            raise AppError("SOURCE_BLOCK_NOT_FOUND", "指定内容块不存在", 404)
        # The source's actual role is preserved even when it quotes an original.
        return self._save_verified(
            course_id,
            ArtifactRef(
                artifact_id=artifact_id,
                revision_id=revision_id,
                block_id=block_id,
            ),
            block_text(block),
            row["content_role"],
        )

    def capture_message(
        self,
        course_id: str,
        message_id: str,
        revision_id: str,
        start: int,
        end: int,
    ) -> SourceSnapshot:
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(business.messages)
                    .join(business.chats)
                    .where(
                        business.messages.c.message_id == message_id,
                        business.messages.c.revision_id == revision_id,
                        business.chats.c.course_id == course_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("SOURCE_MESSAGE_NOT_FOUND", "指定课程的消息修订不存在", 404)
        text = row["text"]
        if not 0 <= start < end <= len(text):
            raise AppError("SELECTION_RANGE_INVALID", "选区不在实际消息范围内", 422)
        return self._save_verified(
            course_id,
            MessageRef(
                message_id=message_id,
                revision_id=revision_id,
                start=start,
                end=end,
            ),
            text[start:end],
            "ai_generated" if row["role"] == "assistant" else "user_note",
        )

    def _save_verified(
        self,
        course_id: str,
        reference: SourceRef,
        text: str,
        content_role: str,
    ) -> SourceSnapshot:
        with self.repository._lock, self.repository.engine.begin() as conn:
            return self.save_in_transaction(conn, course_id, reference, text, content_role)

    @staticmethod
    def save_in_transaction(conn, course_id, reference, text, content_role) -> SourceSnapshot:
        """Internal verified capture; the caller owns its authority and transaction."""
        payload = {
            "course_id": course_id,
            "reference": reference.model_dump(mode="json"),
            "text": text,
            "content_role": content_role,
        }
        digest = _hash(payload)
        snapshot = SourceSnapshot(
            snapshot_id=f"selection_{digest}",
            content_hash=digest,
            created_at=now(),
            **payload,
        )
        prior = (
            conn.execute(
                select(s.snapshots).where(s.snapshots.c.snapshot_id == snapshot.snapshot_id)
            )
            .mappings()
            .first()
        )
        if prior:
            return SourceSnapshot.model_validate(dict(prior))
        conn.execute(insert(s.snapshots).values(**snapshot.model_dump(mode="json")))
        return snapshot

    def snapshot(self, course_id: str, snapshot_id: str) -> SourceSnapshot:
        with self.repository.engine.connect() as conn:
            return self.in_transaction(conn, course_id, snapshot_id)

    def cached_image(self, snapshot: SourceSnapshot) -> bytes:
        """View immutable stored evidence; this does not grant a new model read."""
        if snapshot.reference.kind not in {
            "document_image",
            "document_region_image",
            "submitted_image",
        }:
            raise AppError("SOURCE_IMAGE_DENIED", "此快照没有已保存的图片", 403)
        try:
            data = self.image_path(snapshot.reference.image_sha256).read_bytes()
        except OSError as error:
            raise AppError("SOURCE_IMAGE_UNAVAILABLE", "已保存图片暂不可用，未改读原件") from error
        if hashlib.sha256(data).hexdigest() != snapshot.reference.image_sha256:
            raise AppError("SOURCE_IMAGE_CORRUPT", "已保存图片摘要不符，未改读其他范围")
        return data

    @staticmethod
    def in_transaction(conn: Connection, course_id: str, snapshot_id: str) -> SourceSnapshot:
        row = (
            conn.execute(
                select(s.snapshots).where(
                    s.snapshots.c.snapshot_id == snapshot_id,
                    s.snapshots.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            from .source_grants import granted_origin

            grant = granted_origin(conn, course_id, snapshot_id)
            if grant:
                row = (
                    conn.execute(
                        select(s.snapshots).where(
                            s.snapshots.c.snapshot_id == snapshot_id,
                            s.snapshots.c.course_id == grant["source_course_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if row and row["content_hash"] != grant["source_hash"]:
                    raise AppError("SOURCE_GRANT_CONFLICT", "跨课引用摘要与原快照不符")
            if row is None:
                raise AppError(
                    "SOURCE_SELECTION_DENIED", "选区不存在或不属于指定课程及明确引用", 403
                )
        snapshot = SourceSnapshot.model_validate(dict(row))
        payload = snapshot.model_dump(
            exclude={"snapshot_id", "content_hash", "created_at"}, mode="json"
        )
        if _hash(payload) != snapshot.content_hash:
            raise AppError("SOURCE_SNAPSHOT_CORRUPT", "来源快照完整性不符")
        return snapshot

    def require_current(
        self, snapshot: SourceSnapshot, *, allow_historical_artifacts: bool = False
    ) -> None:
        reference = snapshot.reference
        if isinstance(reference, SnapshotSliceRef):
            parent = self.snapshot(snapshot.course_id, reference.parent_snapshot_id)
            if (
                parent.content_hash != reference.parent_hash
                or reference.end > len(parent.text)
                or snapshot.text != parent.text[reference.start : reference.end]
                or snapshot.content_role != parent.content_role
            ):
                raise AppError("SOURCE_SLICE_CORRUPT", "分段与冻结来源不符")
            self.require_current(parent, allow_historical_artifacts=allow_historical_artifacts)
        elif reference.kind == "submitted_image":
            from .submitted_images import SubmittedImages

            with self.repository.engine.connect() as conn:
                SubmittedImages.validate(conn, self.repository, snapshot)
        elif isinstance(reference, DocumentImageRef):
            source = self._source(snapshot.course_id, reference.asset_id, reference.source_version)
            if (
                source["path"].suffix.lower() != ".pdf"
                or source["content_hash"] != reference.source_digest
            ):
                raise AppError("SOURCE_VERSION_UNAVAILABLE", "页面图像的原PDF版本不符")
            data = self.image_path(reference.image_sha256).read_bytes()
            if hashlib.sha256(data).hexdigest() != reference.image_sha256:
                raise AppError("SOURCE_IMAGE_CORRUPT", "已保存的授权文档页图像校验失败")
            if reference.kind == "document_region_image":
                parent = self.snapshot(snapshot.course_id, reference.parent_text_snapshot_id)
                if (
                    parent.content_hash != reference.parent_text_hash
                    or parent.content_role != "original"
                ):
                    raise AppError("DOCUMENT_REGION_PARENT_CONFLICT", "局部图原文字来源不符")
                self.require_current(parent)
        elif isinstance(reference, (DocumentRef, DocumentSelectionRef, DocumentGapRef)):
            source = self._source(snapshot.course_id, reference.asset_id, reference.source_version)
            if source["content_hash"] != reference.source_digest:
                raise AppError("SOURCE_VERSION_UNAVAILABLE", "来源摘要与快照不符")
            if isinstance(reference, DocumentGapRef):
                raise AppError(reference.error_code, "该页处理未完成，完整任务保留缺口")
            if not snapshot.text.strip():
                raise AppError("SOURCE_TEXT_NOT_READY", "该页尚无可用文字层，完整任务保留缺口")
        elif isinstance(reference, ArtifactRef):
            with self.repository.engine.connect() as conn:
                row = conn.execute(
                    select(s.artifacts.c.head_revision_id).where(
                        s.artifacts.c.artifact_id == reference.artifact_id,
                        s.artifacts.c.course_id == snapshot.course_id,
                    )
                ).first()
                historical = (
                    conn.execute(
                        select(s.revisions.c.revision_id).where(
                            s.revisions.c.artifact_id == reference.artifact_id,
                            s.revisions.c.revision_id == reference.revision_id,
                            s.revisions.c.disposition == "published",
                        )
                    ).first()
                    if allow_historical_artifacts and row is not None
                    else None
                )
            if row is None or (row[0] != reference.revision_id and historical is None):
                raise AppError("SOURCE_REVISION_CHANGED", "成果来源已有新修订，旧快照保留")
        elif isinstance(reference, MessageRef):
            # Saved message selections are immutable historical input, explicitly chosen.
            return
        elif isinstance(reference, AnswerRef):
            from .practice import PracticeStore

            value = PracticeStore(self.repository).get(snapshot.course_id, reference.answer_id)
            if (
                value.text != snapshot.text
                or value.question_revision_id != reference.question_revision_id
                or value.block_id != reference.block_id
                or snapshot.content_role != "user_note"
            ):
                raise AppError("SOURCE_ANSWER_CONFLICT", "实际作答快照与原提交不符")
        elif isinstance(reference, SubtitleRef):
            from .transcripts import TranscriptStore

            TranscriptStore(self.repository).validate_snapshot(snapshot)
            if not snapshot.text.strip():
                raise AppError("SOURCE_TEXT_NOT_READY", "该字幕已清空，未作为生成材料")
        elif isinstance(reference, WebEvidenceRef):
            # This is the immutable retrieved version. Reopening or indexing
            # it never silently fetches a newer website or any media.
            return
        else:
            raise AppError("SUBTITLE_SOURCE_NOT_READY", "字幕版本读取尚未就绪")

    def image_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AppError("SOURCE_IMAGE_DENIED", "页面图像标识无效", 403)
        target = self.repository.root / "learning-source-images" / f"{digest}.png"
        if not target.resolve().is_relative_to(self.repository.root):
            raise AppError("SOURCE_IMAGE_DENIED", "页面图像目录归属不符", 403)
        return target

    def slice(
        self, snapshot: SourceSnapshot, start: int, end: int, *, owner_course_id=None
    ) -> SourceSnapshot:
        if not 0 <= start < end <= len(snapshot.text):
            raise AppError("SOURCE_SLICE_RANGE_INVALID", "分段不在冻结原文内", 422)
        result = self._save_verified(
            snapshot.course_id,
            SnapshotSliceRef(
                parent_snapshot_id=snapshot.snapshot_id,
                parent_hash=snapshot.content_hash,
                start=start,
                end=end,
            ),
            snapshot.text[start:end],
            snapshot.content_role,
        )
        if owner_course_id is not None and owner_course_id != snapshot.course_id:
            from .source_grants import declare_snapshot

            with self.repository._lock, self.repository.engine.begin() as conn:
                self.in_transaction(conn, owner_course_id, snapshot.snapshot_id)
                declare_snapshot(
                    conn,
                    owner_course_id,
                    result.snapshot_id,
                    authority={
                        "kind": "derived_subset",
                        "parent_snapshot_id": snapshot.snapshot_id,
                    },
                )
        return result

    def capture_document_image(
        self, course_id: str, asset_id: str, version: str, page_index: int
    ) -> SourceSnapshot:
        import tempfile

        from .reading import DocumentReader

        self.repository.staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="document-page-", dir=self.repository.staging
        ) as directory:
            image, proof = DocumentReader(self.repository).render(
                course_id, asset_id, version, page_index, Path(directory)
            )
        source = self._source(course_id, asset_id, version)
        reference = DocumentImageRef(
            asset_id=asset_id,
            source_version=version,
            source_digest=source["content_hash"],
            page_index=page_index,
            parser_version=proof["parser_version"],
            image_sha256=proof["image_sha256"],
            width=proof["width"],
            height=proof["height"],
        )
        target = self.image_path(reference.image_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Digest names are immutable, and a competing identical writer is harmless.
        try:
            with target.open("xb") as stream:
                stream.write(image)
        except FileExistsError:
            if target.read_bytes() != image:
                raise AppError("SOURCE_IMAGE_CORRUPT", "已保存的文档页面图像校验失败") from None
        return self._save_verified(course_id, reference, "", "original")

    def capture_document_region(self, course_id: str, candidate) -> SourceSnapshot:
        from .document_regions import DocumentRegions

        crop = DocumentRegions(self.repository).render(course_id, candidate)
        origin = self.snapshot(course_id, candidate.parent_snapshot_id).course_id
        reference = DocumentRegionImageRef(
            asset_id=candidate.asset_id,
            source_version=candidate.source_version,
            source_digest=candidate.source_digest,
            page_index=candidate.page_index,
            parser_version=candidate.parser_version,
            image_sha256=crop.image_sha256,
            width=crop.width,
            height=crop.height,
            region_id=candidate.region_id,
            parent_text_snapshot_id=candidate.parent_snapshot_id,
            parent_text_hash=candidate.parent_hash,
            region_parser_version=candidate.region_parser_version,
            region=crop.actual_region,
            pixel_rect=crop.pixel_rect,
            image_to_pdf=crop.image_to_pdf,
        )
        target = self.image_path(reference.image_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(crop.image)
        except FileExistsError:
            if target.read_bytes() != crop.image:
                raise AppError("SOURCE_IMAGE_CORRUPT", "已保存的文档局部图校验失败") from None
        snapshot = self._save_verified(origin, reference, "", "original")
        if origin != course_id:
            from .source_grants import declare_snapshot

            with self.repository._lock, self.repository.engine.begin() as conn:
                declare_snapshot(
                    conn,
                    course_id,
                    snapshot.snapshot_id,
                    authority={
                        "kind": "document_region",
                        "parent_snapshot_id": candidate.parent_snapshot_id,
                    },
                )
        return snapshot

    def refresh_gap(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        reference = snapshot.reference
        if isinstance(reference, DocumentGapRef) or (
            isinstance(reference, DocumentRef) and not snapshot.text.strip()
        ):
            from .reading import DocumentReader

            page = DocumentReader(self.repository).page(
                snapshot.course_id,
                reference.asset_id,
                reference.source_version,
                reference.page_index,
            )
            if page.source_digest != reference.source_digest:
                raise AppError("SOURCE_VERSION_UNAVAILABLE", "缺口原版本已改变，未改指新来源")
            if not page.text.strip():
                raise AppError("SOURCE_TEXT_NOT_READY", "该页尚无可用文字层")
            return self._save_verified(
                snapshot.course_id,
                DocumentRef(
                    asset_id=reference.asset_id,
                    source_version=reference.source_version,
                    source_digest=page.source_digest,
                    page_index=page.page_index,
                    page_label=page.page_label,
                    start=0,
                    end=len(page.text),
                    parser_version=page.parser_version,
                ),
                page.text,
                "original",
            )
        self.require_current(snapshot)
        return snapshot
