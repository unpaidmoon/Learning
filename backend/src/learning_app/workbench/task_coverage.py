"""Durable source declarations and local enumeration for explicit learning tasks.

This module never invokes a language model, extracts a video frame, or grants QA
tools the task's whole-source authority. Work is planned separately from leaves.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import insert, select, update

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .artifacts import ArtifactStore
from .common import new_id, now, prior_operation, save_operation
from .models import DocumentRef, TaskView
from .sources import SourceStore
from .task_scope_models import (
    TaskAdmission,
    TaskCoverageView,
    TaskLeafView,
    TaskSourceView,
    TaskTargetView,
)
from .task_source_courses import grant_representations, source_course, validate_origins
from .tasks import TaskStore

ARTIFACT_KIND = {
    "note": "note",
    "aligned_translation": "translation",
    "translation": "translation",
    "handout": "handout",
    "question_set": "question_set",
    "card_set": "card_set",
    "essay_review": "essay",
    "answer_review": "solution",
    "deep_solution": "solution",
    "visualization": "visualization",
    "learning_path": "learning_path",
    "book": "book",
    "research": "report",
}
DOCUMENT_EXTENSIONS = {".txt", ".md", ".markdown", ".pdf"}
AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
}
# These are material-like files whose unsupported status must remain a gap.
OTHER_MATERIAL_EXTENSIONS = {
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".epub",
    ".srt",
    ".vtt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".avi",
    ".wmv",
    ".aac",
}
MAX_LEAVES = 20_000


class TaskCoverage:
    def __init__(self, repository):
        self.repository = repository
        self.sources = SourceStore(repository)

    def admit(self, course_id: str, value: TaskAdmission) -> TaskView:
        """Persist the request before parsing a PDF or probing/transcribing audio."""
        payload = value.model_dump(
            mode="json",
            exclude={
                key
                for key in ("answer_review", "essay_review", "research")
                if getattr(value, key) is None
            },
        )
        for declaration in payload["sources"]:
            if declaration.get("source_course_id") in {None, course_id}:
                declaration.pop("source_course_id", None)
            if declaration.get("kind") == "document" and not declaration.get("text_only"):
                declaration.pop("text_only", None)
        declarations = payload["sources"]
        serialized = [_hash(declaration) for declaration in declarations]
        if len(set(serialized)) != len(serialized):
            raise AppError("TASK_SOURCE_DUPLICATE", "同一来源范围重复声明", 422)
        if value.kind == "research" and value.allow_external:
            declarations = [
                *declarations,
                {
                    "kind": "external_research",
                    "urls": value.research.urls if value.research else [],
                    "complete_text": value.coverage_mode == "complete",
                    "label": "指定外部网页"
                    if value.research and value.research.urls
                    else "外部公开研究",
                },
            ]
        if value.target.mode == "edit":
            # The user has explicitly identified these exact target blocks.
            # Snapshot their own content/role; do not implicitly read ancestors.
            target_source = {
                "kind": "artifact",
                "artifact_id": value.target.artifact_id,
                "revision_id": value.target.expected_revision_id,
                "block_ids": value.target.block_ids,
            }
            declarations = (
                [*declarations, target_source]
                if target_source not in declarations
                else declarations
            )
        with self.repository.engine.connect() as conn:
            prior = prior_operation(conn, course_id, "task_create", value.operation_id, payload)
        if prior is not None:
            return TaskView.model_validate(prior)
        review_binding = None
        essay_input = None
        if value.kind == "answer_review":
            from .practice import PracticeStore

            answer, question, actual = PracticeStore(self.repository).review_inputs(
                course_id, value.answer_review.answer_id
            )
            review_binding = dict(
                answer_id=answer["answer_id"],
                expected_revision=value.answer_review.expected_revision,
                parent_assessment_id=answer["effective_assessment_id"],
                question_snapshot_id=question.snapshot_id,
                answer_snapshot_id=actual.snapshot_id,
            )
            declarations = [
                {"kind": "selection", "snapshot_id": source.snapshot_id}
                for source in (question, actual)
            ]
        if value.kind == "essay_review":
            from .essays import EssayStore

            block, source = EssayStore(self.repository).review_input(course_id, value.essay_review)
            essay_input = (block, source)
            declarations = [{"kind": "selection", "snapshot_id": source.snapshot_id}]
        course_members, membership_error = None, None
        for declaration in payload["sources"]:
            if declaration["kind"] == "course":
                try:
                    course_members = self._course_members(
                        declaration.get("source_course_id") or course_id,
                        declaration["include_generated"],
                    )
                except (AppError, OSError) as exc:
                    membership_error = getattr(exc, "code", "SOURCE_IO_UNAVAILABLE")
        with self.repository._lock, self.repository.engine.begin() as conn:
            return self._admit_in_transaction(
                conn,
                course_id,
                value,
                payload,
                declarations,
                course_members=course_members,
                membership_error=membership_error,
                review_binding=review_binding,
                essay_input=essay_input,
            )

    def _admit_in_transaction(
        self,
        conn,
        course_id,
        value,
        payload,
        declarations,
        *,
        course_members=None,
        membership_error=None,
        review_binding=None,
        essay_input=None,
        origin=None,
    ):
        """Shared atomic admission; native callers prevalidate their frozen grant."""
        prior = prior_operation(conn, course_id, "task_create", value.operation_id, payload)
        if prior is not None:
            return TaskView.model_validate(prior)
        if review_binding:
            from . import practice_schema as practice
            from .practice import PracticeStore

            current_answer = PracticeStore._answer(conn, course_id, review_binding["answer_id"])
            if current_answer["assessment_revision"] != review_binding["expected_revision"]:
                raise AppError(
                    "ASSESSMENT_VERSION_CONFLICT", "评价已变化，未开始基于过期版本的批改"
                )
            if conn.execute(
                select(practice.answer_reviews.c.task_id).where(
                    practice.answer_reviews.c.answer_id == review_binding["answer_id"],
                    practice.answer_reviews.c.expected_revision
                    == review_binding["expected_revision"],
                )
            ).first():
                raise AppError(
                    "ASSESSMENT_REVIEW_EXISTS",
                    "此作答版本已有参考评价任务，请核对原任务而不重复提交",
                )
        if not conn.execute(
            select(business.courses.c.course_id).where(business.courses.c.course_id == course_id)
        ).first():
            raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)
        if (
            value.chat_id
            and not conn.execute(
                select(business.chats.c.chat_id).where(
                    business.chats.c.course_id == course_id,
                    business.chats.c.chat_id == value.chat_id,
                )
            ).first()
        ):
            raise AppError("TASK_CHAT_DENIED", "任务对话不属于指定课程", 403)
        identity, stamp = new_id("task"), now()
        validate_origins(conn, course_id, declarations)
        from .source_grants import declare_snapshot

        for declaration in declarations:
            if declaration["kind"] == "selection":
                declare_snapshot(
                    conn,
                    course_id,
                    declaration["snapshot_id"],
                    authority={"kind": "task_selection", "task_id": identity},
                )
        if value.target.mode == "edit":
            artifact = ArtifactStore._view(conn, course_id, value.target.artifact_id)
            base = ArtifactStore._revision(
                conn, artifact.artifact_id, value.target.expected_revision_id
            )
            if base.disposition != "published" or not set(value.target.block_ids).issubset(
                {block.block_id for block in base.body.blocks}
            ):
                raise AppError("TASK_TARGET_SCOPE_DENIED", "指定修订或内容块不属于可修改范围", 403)
            target = dict(
                reserved_artifact_id=artifact.artifact_id,
                materialized_artifact_id=artifact.artifact_id,
                mode="edit",
                artifact_kind=artifact.kind,
                title=base.title,
                base_revision_id=base.revision_id,
                expected_revision_id=base.revision_id,
                write_scope={"block_ids": value.target.block_ids},
            )
            from .range_edits import RangeEdits

            range_scope = RangeEdits.scope(base, value.target.block_ids)
            if range_scope:
                target["write_scope"]["range_edit"] = range_scope
                retained = []
                for declaration in declarations:
                    if declaration["kind"] == "selection":
                        ref = SourceStore.in_transaction(
                            conn, course_id, declaration["snapshot_id"]
                        ).reference
                        if (
                            ref.kind == "artifact"
                            and ref.artifact_id == artifact.artifact_id
                            and ref.revision_id == base.revision_id
                            and ref.block_id in value.target.block_ids
                        ):
                            continue  # The same target is read through bounded field slices.
                    retained.append(declaration)
                # This internal declaration grants only the target text field.
                # In a translation the original-language prefix is not input.
                declarations = [
                    {**declaration, "target_field": "markdown"}
                    if declaration.get("kind") == "artifact"
                    and declaration.get("artifact_id") == artifact.artifact_id
                    and declaration.get("revision_id") == base.revision_id
                    and declaration.get("block_ids") == value.target.block_ids
                    else declaration
                    for declaration in retained
                ]
        else:
            target = dict(
                reserved_artifact_id=new_id("learning"),
                mode="create",
                artifact_kind=ARTIFACT_KIND[value.kind],
                title=value.target.title,
                write_scope={"task_owned_slots": True},
            )
        conn.execute(
            insert(s.tasks).values(
                task_id=identity,
                course_id=course_id,
                chat_id=value.chat_id,
                kind=value.kind,
                instruction=value.instruction,
                coverage_mode=value.coverage_mode,
                revision=0,
                scope_revision=0,
                state="queued",
                settings=value.settings.model_dump(mode="json"),
                sources_only=value.sources_only,
                allow_external=value.allow_external,
                outline=[item.model_dump(mode="json") for item in value.outline],
                outline_confirmed=value.kind != "book"
                or value.outline_supplied
                or value.target.mode == "edit",
                layout_version=2,
                plan_revision=0,
                scope_spec={
                    "sources": declarations,
                    "source_chunker": "aligned-paragraph-cp-v1"
                    if value.kind == "aligned_translation"
                    else "deepstudent-lines-lossless-v1",
                    **({"origin": origin} if origin else {}),
                    **(
                        {"research": value.research.model_dump(mode="json")}
                        if value.research
                        else {}
                    ),
                },
                created_at=stamp,
                updated_at=stamp,
            )
        )
        conn.execute(
            insert(s.task_targets).values(
                task_id=identity, structure_revision=0, state="reserved", **target
            )
        )
        if review_binding:
            conn.execute(insert(practice.answer_reviews).values(task_id=identity, **review_binding))
        if essay_input:
            from .essays import EssayStore

            EssayStore.bind(
                conn,
                course_id,
                identity,
                value.essay_review,
                essay_input[0],
                essay_input[1].snapshot_id,
            )
        for ordinal, declaration in enumerate(declarations):
            source_id = self._insert_source(conn, identity, ordinal, declaration)
            if declaration["kind"] == "course":
                if membership_error is None:
                    conn.execute(
                        update(s.task_sources)
                        .where(s.task_sources.c.source_id == source_id)
                        .values(frozen=course_members)
                    )
                else:
                    conn.execute(
                        update(s.task_sources)
                        .where(s.task_sources.c.source_id == source_id)
                        .values(
                            state="blocked",
                            error_code=membership_error,
                        )
                    )
        conn.execute(
            insert(s.task_events).values(
                task_id=identity, revision=0, kind="created", detail=payload, created_at=stamp
            )
        )
        self.repository._hook("learning_task_before_commit", task_id=identity)
        result = TaskStore._view(conn, course_id, identity)
        save_operation(conn, course_id, "task_create", value.operation_id, payload, result)
        return result

    @staticmethod
    def _insert_source(conn, task_id, ordinal, declaration):
        identity = "tsource_" + _hash({"task": task_id, "declaration": declaration})
        if not conn.execute(
            select(s.task_sources.c.source_id).where(s.task_sources.c.source_id == identity)
        ).first():
            conn.execute(
                insert(s.task_sources).values(
                    source_id=identity,
                    task_id=task_id,
                    ordinal=ordinal,
                    declaration=declaration,
                    state="declared",
                    updated_at=now(),
                )
            )
        return identity

    def _source_update(self, source_id, **changes):
        with self.repository._lock, self.repository.engine.begin() as conn:
            if not self._active_source(conn, source_id):
                return
            conn.execute(
                update(s.task_sources)
                .where(s.task_sources.c.source_id == source_id)
                .values(updated_at=now(), **changes)
            )

    def _freeze_leaves(self, row, frozen, leaves):
        with self.repository._lock, self.repository.engine.begin() as conn:
            if not self._active_source(conn, row["source_id"]):
                return
            previous = conn.execute(
                select(s.task_sources.c.expected_leaf_count).where(
                    s.task_sources.c.source_id == row["source_id"]
                )
            ).scalar_one()
            if previous is not None:
                return
            current_count = len(
                conn.execute(
                    select(s.task_source_leaves.c.leaf_id).where(
                        s.task_source_leaves.c.task_id == row["task_id"]
                    )
                ).all()
            )
            if current_count + len(leaves) > MAX_LEAVES:
                raise AppError("SOURCE_UNIT_LIMIT", "指定范围超出单任务单位上限，未截断")
            owner = conn.execute(
                select(s.tasks.c.course_id).where(s.tasks.c.task_id == row["task_id"])
            ).scalar_one()
            origin = source_course(owner, row)
            title = (
                conn.execute(
                    select(business.courses.c.title).where(business.courses.c.course_id == origin)
                ).scalar_one()
                if origin != owner
                else None
            )
            for ordinal, item in enumerate(leaves):
                locator, label = item
                if origin != owner:
                    locator = {**locator, "source_course_id": origin}
                    label = f"{title} · {label}"
                leaf_id = "leaf_" + _hash({"source": row["source_id"], "locator": locator})
                conn.execute(
                    insert(s.task_source_leaves).values(
                        leaf_id=leaf_id,
                        task_id=row["task_id"],
                        source_id=row["source_id"],
                        ordinal=ordinal,
                        label=label[:500],
                        locator=locator,
                        required_representations=[],
                        representations=[],
                        state="pending",
                        updated_at=now(),
                    )
                )
            conn.execute(
                update(s.task_sources)
                .where(s.task_sources.c.source_id == row["source_id"])
                .values(
                    frozen=frozen,
                    expected_leaf_count=len(leaves),
                    state="preparing" if leaves else "ready",
                    error_code=None,
                    updated_at=now(),
                )
            )

    @staticmethod
    def _stat(path):
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]

    @staticmethod
    def _active_source(conn, source_id):
        return bool(
            conn.execute(
                select(s.task_sources.c.source_id)
                .join(s.tasks)
                .where(
                    s.task_sources.c.source_id == source_id,
                    s.task_sources.c.excluded_at_scope_revision.is_(None),
                    s.tasks.c.state.in_(["queued", "running", "needs_sources"]),
                )
            ).first()
        )

    def _course_members(self, course_id, include_generated):
        files = CourseFileStore(self.repository)
        root, members = files._root(course_id), []
        for parent, directories, names in os.walk(root, followlinks=False):
            # Course membership is captured at admission; hashes are then frozen
            # only if the member still has this same identity and metadata.
            for name in sorted(directories + names):
                path = files._path(root, Path(parent, name).relative_to(root).as_posix())
                if (
                    path.is_file()
                    and path.suffix.lower()
                    in DOCUMENT_EXTENSIONS | AUDIO_EXTENSIONS | OTHER_MATERIAL_EXTENSIONS
                ):
                    members.append(
                        {
                            "relative_path": path.relative_to(root).as_posix(),
                            "stat": self._stat(path),
                        }
                    )
                    if len(members) > 1000:
                        raise AppError(
                            "SOURCE_COURSE_LIMIT", "课程资料数量超过当前任务上限，未截断"
                        )
        frozen = {"members": members}
        if include_generated:
            frozen["artifacts"] = [
                {
                    "kind": "artifact",
                    "artifact_id": item.artifact_id,
                    "revision_id": item.head_revision_id,
                    "block_ids": [],
                }
                for item in ArtifactStore(self.repository).list(course_id)
            ]
        return frozen

    def _enumerate_course(self, course_id, row):
        frozen = row["frozen"]
        if frozen is None:
            frozen = self._course_members(course_id, row["declaration"]["include_generated"])
            self._source_update(row["source_id"], frozen=frozen)
        with self.repository._lock, self.repository.engine.begin() as conn:
            if not self._active_source(conn, row["source_id"]):
                return
            highest = max(
                conn.execute(
                    select(s.task_sources.c.ordinal).where(
                        s.task_sources.c.task_id == row["task_id"]
                    )
                ).scalars(),
                default=0,
            )
            for offset, member in enumerate(frozen["members"] + frozen.get("artifacts", []), 1):
                declaration = (
                    {"kind": "course_member", **member} if "relative_path" in member else member
                )
                if row["declaration"].get("source_course_id"):
                    declaration = {
                        **declaration,
                        "source_course_id": row["declaration"]["source_course_id"],
                    }
                self._insert_source(conn, row["task_id"], highest + offset, declaration)
            conn.execute(
                update(s.task_sources)
                .where(s.task_sources.c.source_id == row["source_id"])
                .values(state="ready", expected_leaf_count=0, error_code=None, updated_at=now())
            )

    def _enumerate(self, course_id, row):
        request = row["declaration"]
        kind = request["kind"]
        if kind != "selection":
            course_id = source_course(course_id, row)
        if kind == "external_research":
            self._source_update(
                row["source_id"],
                expected_leaf_count=0,
                state="preparing",
                frozen={
                    "kind": kind,
                    "external_epoch": (row["frozen"] or {}).get("external_epoch", 0),
                },
            )
            return
        if kind == "course":
            self._enumerate_course(course_id, row)
            return
        frozen = row["frozen"]
        if kind == "course_member":
            files = CourseFileStore(self.repository)
            root = files._root(course_id)
            path = files._path(root, request["relative_path"])
            if frozen is None:
                if self._stat(path) != request["stat"]:
                    raise AppError("SOURCE_VERSION_UNAVAILABLE", "冻结课程成员已改变，未读取新版本")
                with files._lock:
                    source = files._register(course_id, root, path)
                if self._stat(path) != request["stat"]:
                    raise AppError("SOURCE_VERSION_UNAVAILABLE", "冻结课程成员在登记时改变")
                extension = path.suffix.lower()
                frozen = {
                    "kind": "document"
                    if extension in DOCUMENT_EXTENSIONS
                    else "transcript"
                    if extension in AUDIO_EXTENSIONS
                    else "unsupported",
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                    "first_page": 0,
                    "last_page": None,
                    **(
                        {"source_course_id": request["source_course_id"]}
                        if request.get("source_course_id")
                        else {}
                    ),
                }
                self._source_update(row["source_id"], frozen=frozen)
            request, kind = frozen, frozen["kind"]
        if kind == "unsupported":
            raise AppError("SOURCE_FORMAT_UNSUPPORTED", "此资料格式尚不能完整读取，已保留范围缺口")
        if kind == "selection":
            snapshot = self.sources.snapshot(course_id, request["snapshot_id"])
            self.sources.require_current(snapshot, allow_historical_artifacts=True)
            self._freeze_leaves(row, request, [({"snapshot_id": snapshot.snapshot_id}, "所选内容")])
        elif kind == "document":
            source = self.sources._source(course_id, request["asset_id"], request["source_version"])
            last, first = request.get("last_page"), request.get("first_page", 0)
            if source["path"].suffix.lower() == ".pdf":
                from .reading import DocumentReader

                info = DocumentReader(self.repository).info(
                    course_id, request["asset_id"], request["source_version"]
                )
                last = info.page_count - 1 if last is None else last
                if not first <= last < info.page_count:
                    raise AppError("SOURCE_PAGE_RANGE_INVALID", "范围不在实际PDF页数内", 422)
            else:
                if first != 0 or last not in {None, 0}:
                    raise AppError("SOURCE_PAGE_RANGE_INVALID", "文本正文单位为0", 422)
                last = 0
            if last - first + 1 > MAX_LEAVES:
                raise AppError("SOURCE_UNIT_LIMIT", "文档范围超出当前上限，未截断")
            frozen = {
                **request,
                "kind": "document",
                "last_page": last,
                "source_digest": source["content_hash"],
                "format": source["path"].suffix.lower(),
            }
            self._freeze_leaves(
                row,
                frozen,
                [
                    (
                        {"page_index": i},
                        f"{source['path'].name} · 第{i + 1}页"
                        + ("文字" if frozen.get("text_only") else ""),
                    )
                    for i in range(first, last + 1)
                ],
            )
        elif kind == "artifact":
            with self.repository.engine.connect() as conn:
                ArtifactStore._view(conn, course_id, request["artifact_id"])
                revision = ArtifactStore._revision(
                    conn, request["artifact_id"], request["revision_id"]
                )
            blocks = {block.block_id: block for block in revision.body.blocks}
            selected = request.get("block_ids") or list(blocks)
            if (
                revision.disposition != "published"
                or len(set(selected)) != len(selected)
                or not set(selected).issubset(blocks)
            ):
                raise AppError("SOURCE_BLOCK_RANGE_INVALID", "成果修订或内容范围不符", 422)
            self._freeze_leaves(
                row,
                request,
                [({"block_id": block}, f"{revision.title} · {block}") for block in selected],
            )
        elif kind == "transcript":
            self._enumerate_transcript(course_id, row, request)

    def _enumerate_transcript(self, course_id, row, request):
        from .audio_sources import AudioSources
        from .preparations import PreparationStore
        from .processing_models import PreparationCreate
        from .transcripts import WINDOW_MS, TranscriptStore

        AudioSources(self.repository).current(
            course_id, request["asset_id"], request["source_version"]
        )
        frozen = row["frozen"] if row["frozen"] and "track_id" in row["frozen"] else None
        if frozen is None:
            track_id = request.get("track_id")
            job_id = None
            if track_id is None:
                store = PreparationStore(self.repository)
                definition = store.define_asr(
                    course_id,
                    PreparationCreate(
                        kind="asr",
                        asset_id=request["asset_id"],
                        source_version=request["source_version"],
                    ),
                    PROJECT_ROOT,
                )
                progress = store.publish_pdf(definition, summary=True)
                track_id, job_id = definition["payload"]["parameters"]["track_id"], progress.job_id
            with self.repository.engine.connect() as conn:
                track = TranscriptStore._track(conn, course_id, track_id)
                if (track["asset_id"], track["source_version"]) != (
                    request["asset_id"],
                    request["source_version"],
                ):
                    raise AppError("TASK_SUBTITLE_SCOPE_DENIED", "字幕轨不属于冻结资料版本", 403)
                if job_id is None:
                    for candidate in conn.execute(
                        select(s.preparations)
                        .where(
                            s.preparations.c.course_id == course_id,
                            s.preparations.c.asset_id == request["asset_id"],
                            s.preparations.c.source_version == request["source_version"],
                            s.preparations.c.kind == "asr",
                            s.preparations.c.scope == "full",
                        )
                        .order_by(s.preparations.c.created_at.desc())
                    ).mappings():
                        if candidate["parameters"].get("track_id") == track_id:
                            job_id = candidate["job_id"]
                            break
            if job_id is None:
                store = PreparationStore(self.repository)
                definition = store.define_asr_track(course_id, track_id, PROJECT_ROOT)
                job_id = store.publish_pdf(definition, summary=True).job_id
            frozen = {
                **request,
                "kind": "transcript",
                "track_id": track_id,
                "edit_head_id": track["edit_head_id"],
            }
            self._source_update(
                row["source_id"], frozen=frozen, preparation_job_id=job_id, state="preparing"
            )
        with self.repository.engine.connect() as conn:
            track = TranscriptStore._track(conn, course_id, frozen["track_id"])
        if track["probe"] is None:
            self._preparation_pending(course_id, row)
            return
        probe = track["probe"]
        start = max(probe["audio_start_ms"], request.get("start_ms", 0))
        end = min(probe["audio_end_ms"], request.get("end_ms") or probe["audio_end_ms"])
        if start >= end:
            raise AppError("SOURCE_TIME_RANGE_INVALID", "所选范围不包含有效音轨", 422)
        frozen = {**frozen, "start_ms": start, "end_ms": end, "probe_hash": _hash(probe)}
        indexes = [
            i for i in probe["unit_indexes"] if i * WINDOW_MS < end and (i + 1) * WINDOW_MS > start
        ]
        self._freeze_leaves(
            row, frozen, [({"unit_index": i}, f"字幕 · {i * 30}–{(i + 1) * 30}秒") for i in indexes]
        )

    def _preparation_pending(self, course_id, row):
        with self.repository.engine.connect() as conn:
            job_id = conn.execute(
                select(s.task_sources.c.preparation_job_id).where(
                    s.task_sources.c.source_id == row["source_id"]
                )
            ).scalar_one()
        if job_id:
            from .preparations import PreparationStore

            progress = PreparationStore(self.repository).summary(course_id, job_id)
            if progress.state in {"failed", "needs_source", "paused", "cancelled"}:
                raise AppError(
                    "TASK_PREPARATION_" + progress.state.upper(), "本地资料准备未完成，等待明确恢复"
                )
        else:
            raise AppError(
                "TASK_SUBTITLE_NOT_READY", "此冻结字幕轨尚不完整，未替换轨道或静默补用其他版本"
            )

    def _apply_preparation_retry(self, course_id, source):
        course_id = source_course(course_id, source)
        request = (source["frozen"] or {}).get("preparation_retry")
        if not request or request["state"] != "pending":
            return True
        from .preparations import PreparationStore
        from .processing_models import PreparationControl

        with self.repository._lock:
            with self.repository.engine.connect() as conn:
                if not self._active_source(conn, source["source_id"]):
                    return False
            try:
                # A durable user decision precedes this idempotent local
                # control. Keep the job/track/parameters and the manual CAS.
                PreparationStore(self.repository).control(
                    course_id,
                    source["preparation_job_id"],
                    "retry",
                    PreparationControl(
                        operation_id=request["operation_id"],
                        expected_revision=request["expected_revision"],
                    ),
                )
            except AppError as exc:
                self._source_update(
                    source["source_id"],
                    state="blocked",
                    error_code=exc.code,
                    frozen={
                        **source["frozen"],
                        "preparation_retry": {
                            **request,
                            "state": "blocked",
                            "error_code": exc.code,
                        },
                    },
                )
                return False
            self.repository._hook(
                "task_preparation_retry_after_control",
                task_id=source["task_id"],
                job_id=source["preparation_job_id"],
            )
            self._source_update(
                source["source_id"],
                frozen={**source["frozen"], "preparation_retry": {**request, "state": "applied"}},
            )
        return True

    @staticmethod
    def _representation(snapshot, kind="text"):
        return {
            "snapshot_id": snapshot.snapshot_id,
            "content_hash": snapshot.content_hash,
            "content_role": snapshot.content_role,
            "representation": kind,
            "text_length": len(snapshot.text),
        }

    def _prepare_leaf(self, course_id, source, leaf):
        owner_course_id = course_id
        if "snapshot_id" not in leaf["locator"]:
            course_id = source_course(course_id, source)
        frozen, locator = source["frozen"], leaf["locator"]
        proof = None
        if "snapshot_id" in locator:
            snapshot = self.sources.snapshot(course_id, locator["snapshot_id"])
            with self.repository.engine.connect() as conn:
                kind = conn.execute(
                    select(s.tasks.c.kind).where(s.tasks.c.task_id == source["task_id"])
                ).scalar_one()
            if kind in {"translation", "aligned_translation"} and snapshot.reference.kind in {
                "document_image",
                "document_region_image",
                "submitted_image",
            }:
                raise AppError(
                    "SOURCE_TRANSLATION_TEXT_REQUIRED", "对照翻译范围是文字，图片未作为输入发送"
                )
            self.sources.require_current(snapshot, allow_historical_artifacts=True)
            representations = [
                self._representation(
                    snapshot,
                    "image"
                    if snapshot.reference.kind
                    in {"document_image", "document_region_image", "submitted_image"}
                    else "text",
                )
            ]
        elif frozen["kind"] == "document":
            if frozen["format"] == ".pdf":
                from .reading import DocumentReader

                page = DocumentReader(self.repository).page(
                    course_id, frozen["asset_id"], frozen["source_version"], locator["page_index"]
                )
                coverage = page.coverage
                # Plain glyph order does not preserve superscripts, columns or
                # tables made solely from text objects. Full-document authority
                # therefore includes the visible layout, except proven blanks.
                visual = (
                    coverage is None
                    or coverage.object_count > 0
                    or coverage.annotation_count > 0
                    or coverage.unmapped_characters > 0
                )
                if frozen.get("text_only"):
                    if (
                        page.coverage
                        and page.coverage.unmapped_characters
                        and page.text_status != "ocr"
                    ):
                        raise AppError(
                            "SOURCE_TRANSLATION_TEXT_UNMAPPED",
                            "该页有尚不能可靠读取的文字，已保留范围缺口",
                        )
                    if page.text_status == "needs_ocr" and visual:
                        return self._wait_pdf_text(course_id, source)
                    if not page.text.strip() and visual:
                        raise AppError(
                            "SOURCE_TRANSLATION_TEXT_NOT_READY",
                            "该页未得到可核验文字，已保留文字翻译范围缺口",
                        )
                    visual = False
                representations = []
                if page.text.strip():
                    snapshot = self.sources._save_verified(
                        course_id,
                        DocumentRef(
                            asset_id=frozen["asset_id"],
                            source_version=frozen["source_version"],
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
                    representations.append(self._representation(snapshot))
                if visual:
                    snapshot = self.sources.capture_document_image(
                        course_id, frozen["asset_id"], frozen["source_version"], page.page_index
                    )
                    representations.append(self._representation(snapshot, "image"))
                if not representations:
                    proof = {
                        "local_empty_page": coverage.model_dump(mode="json"),
                        "parser_version": page.parser_version,
                    }
            else:
                text, _ = self.sources.text(course_id, frozen["asset_id"], frozen["source_version"])
                if text.strip():
                    snapshot = self.sources.capture_text(
                        course_id, frozen["asset_id"], frozen["source_version"], 0, len(text)
                    )
                    representations = [self._representation(snapshot)]
                else:
                    representations, proof = (
                        [],
                        {"local_empty_text": True, "source_digest": frozen["source_digest"]},
                    )
        elif frozen["kind"] == "artifact":
            snapshot = self.sources.capture_artifact(
                course_id, frozen["artifact_id"], frozen["revision_id"], locator["block_id"]
            )
            self.sources.require_current(snapshot, allow_historical_artifacts=True)
            if frozen.get("target_field") == "markdown":
                with self.repository.engine.connect() as conn:
                    revision = ArtifactStore._revision(
                        conn, frozen["artifact_id"], frozen["revision_id"]
                    )
                block = next(b for b in revision.body.blocks if b.block_id == locator["block_id"])
                offset = (
                    len(block.translation.source_text) + 2 if block.kind == "translation" else 0
                )
                if snapshot.text[offset : offset + len(block.markdown)] != block.markdown:
                    raise AppError("TASK_EDIT_FIELD_CONFLICT", "目标正文与冻结来源不符")
                snapshot = self.sources.slice(snapshot, offset, offset + len(block.markdown))
            representations = [self._representation(snapshot)]
        elif frozen["kind"] == "transcript":
            representations, proof = self._prepare_transcript_leaf(course_id, source, leaf)
            if representations is None:
                return False
        else:
            raise AppError("TASK_SOURCE_DECLARATION_INVALID", "来源声明类型无效", 422)
        if not representations and proof is None:
            raise AppError("TASK_SOURCE_COVERAGE_UNKNOWN", "未能证明此单位已完整读取")
        with self.repository._lock, self.repository.engine.begin() as conn:
            if not self._active_source(conn, source["source_id"]):
                return False
            current = conn.execute(
                select(s.task_source_leaves.c.excluded_at_scope_revision).where(
                    s.task_source_leaves.c.leaf_id == leaf["leaf_id"]
                )
            ).scalar_one()
            if current is not None:
                return False
            grant_representations(conn, owner_course_id, course_id, source, leaf, representations)
            conn.execute(
                update(s.task_source_leaves)
                .where(s.task_source_leaves.c.leaf_id == leaf["leaf_id"])
                .values(
                    state="ready" if representations else "empty",
                    representations=representations,
                    required_representations=[item["representation"] for item in representations],
                    locator={**locator, **({"empty_proof": proof} if proof else {})},
                    error_code=None,
                    updated_at=now(),
                )
            )
        return True

    def _wait_pdf_text(self, course_id, source):
        from .preparations import PreparationStore

        store = PreparationStore(self.repository)
        if source["preparation_job_id"]:
            job = store.get(course_id, source["preparation_job_id"])
        else:
            frozen = source["frozen"]
            job = store.create_pdf_range(
                course_id,
                frozen["asset_id"],
                frozen["source_version"],
                frozen["first_page"],
                frozen["last_page"],
                PROJECT_ROOT,
            )
            self._source_update(
                source["source_id"], preparation_job_id=job.job_id, state="preparing"
            )
            source["preparation_job_id"] = job.job_id
        if job.state in {"failed", "needs_source", "paused", "cancelled", "completed"}:
            raise AppError(
                "SOURCE_TRANSLATION_OCR_" + job.state.upper(),
                "文字准备尚未得到该页可用结果，已保留缺口及处理状态",
            )
        return False

    def _prepare_transcript_leaf(self, course_id, source, leaf):
        from .transcript_models import SubtitleCapture
        from .transcripts import TranscriptStore

        frozen, index = source["frozen"], leaf["locator"]["unit_index"]
        with self.repository.engine.connect() as conn:
            track = TranscriptStore._track(conn, course_id, frozen["track_id"])
            row = (
                conn.execute(
                    select(s.transcript_units).where(
                        s.transcript_units.c.track_id == frozen["track_id"],
                        s.transcript_units.c.unit_index == index,
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                patches = TranscriptStore._edit(conn, frozen["track_id"], frozen["edit_head_id"])
                segments = TranscriptStore._raw_segments(row)
        if row is None:
            self._preparation_pending(course_id, source)
            return None, None
        representations = []
        for segment in segments:
            patch = patches.get(segment.segment_id)
            revision_id = frozen["edit_head_id"] if patch else segment.raw_revision_id
            snapshot = TranscriptStore(self.repository).capture(
                course_id,
                frozen["track_id"],
                SubtitleCapture(segment_id=segment.segment_id, revision_id=revision_id),
            )
            ref = snapshot.reference
            if (
                ref.end_ms <= frozen["start_ms"]
                or ref.start_ms >= frozen["end_ms"]
                or not snapshot.text.strip()
            ):
                continue
            # Boundary segments are frozen and visible in the coverage ledger;
            # never pretend time clipping can remove corresponding text exactly.
            if ref.start_ms < frozen["start_ms"] or ref.end_ms > frozen["end_ms"]:
                raise AppError(
                    "TASK_SUBTITLE_BOUNDARY_UNRESOLVED",
                    "字幕片段跨越指定边界，请按片段选择或调整范围",
                )
            representations.append(self._representation(snapshot))
        return representations, {
            "local_audio_unit": row["revision_id"],
            "raw_payload_hash": row["payload_hash"],
            "probe_hash": _hash(track["probe"]),
        }

    def refresh(self, course_id, task_id, *, budget=8):
        """Bounded resumable local preparation. Errors are persisted, not truncated."""
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["layout_version"] != 2 or task["state"] in {
                "paused",
                "cancelled",
                "completed",
                "partial",
                "unknown",
            }:
                return False
            sources = [
                dict(row)
                for row in conn.execute(
                    select(s.task_sources)
                    .where(
                        s.task_sources.c.task_id == task_id,
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                    .order_by(s.task_sources.c.ordinal)
                ).mappings()
            ]
        enumerated = 0
        for source in sources:
            if not self._apply_preparation_retry(course_id, source):
                continue
            if (
                source["state"] not in {"declared", "preparing"}
                or source["expected_leaf_count"] is not None
            ):
                continue
            if enumerated >= budget:
                break
            with self.repository.engine.connect() as conn:
                if not self._active_source(conn, source["source_id"]):
                    return False
            enumerated += 1
            try:
                self._enumerate(course_id, source)
            except (AppError, OSError) as exc:
                self._source_update(
                    source["source_id"],
                    state="blocked",
                    error_code=getattr(exc, "code", "SOURCE_IO_UNAVAILABLE"),
                )
        with self.repository.engine.connect() as conn:
            sources = {
                row["source_id"]: dict(row)
                for row in conn.execute(
                    select(s.task_sources).where(s.task_sources.c.task_id == task_id)
                ).mappings()
            }
            leaves = [
                dict(row)
                for row in conn.execute(
                    select(s.task_source_leaves)
                    .join(s.task_sources)
                    .where(
                        s.task_source_leaves.c.task_id == task_id,
                        s.task_source_leaves.c.state == "pending",
                        s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                    .order_by(s.task_sources.c.ordinal, s.task_source_leaves.c.ordinal)
                ).mappings()
            ]
        for leaf in leaves[:budget]:
            source = sources[leaf["source_id"]]
            if source["excluded_at_scope_revision"] is not None:
                continue
            with self.repository.engine.connect() as conn:
                if not self._active_source(conn, source["source_id"]):
                    return False
            try:
                self._prepare_leaf(course_id, source, leaf)
            except (AppError, OSError) as exc:
                with self.repository._lock, self.repository.engine.begin() as conn:
                    if not self._active_source(conn, source["source_id"]):
                        continue
                    conn.execute(
                        update(s.task_source_leaves)
                        .where(s.task_source_leaves.c.leaf_id == leaf["leaf_id"])
                        .values(
                            state="blocked",
                            error_code=getattr(exc, "code", "SOURCE_IO_UNAVAILABLE"),
                            updated_at=now(),
                        )
                    )
        with self.repository._lock, self.repository.engine.begin() as conn:
            for source in sources.values():
                if (
                    source["expected_leaf_count"] is None
                    or source["excluded_at_scope_revision"] is not None
                ):
                    continue
                states = list(
                    conn.execute(
                        select(s.task_source_leaves.c.state).where(
                            s.task_source_leaves.c.source_id == source["source_id"],
                            s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                        )
                    ).scalars()
                )
                empty_book_source = task["kind"] in {"book", "research"} and (
                    (bool(states) and all(state == "empty" for state in states))
                    or (
                        source["declaration"]["kind"] == "course"
                        and source["expected_leaf_count"] == 0
                        and not (source["frozen"] or {}).get("members")
                        and not (source["frozen"] or {}).get("artifacts")
                    )
                )
                state = (
                    "preparing"
                    if "pending" in states
                    else "blocked"
                    if "blocked" in states or empty_book_source
                    else "ready"
                )
                error = (
                    "TASK_SOURCE_EMPTY"
                    if empty_book_source
                    else source["error_code"]
                    if state == "blocked"
                    else None
                )
                if source["declaration"]["kind"] == "external_research":
                    from .research_external import ResearchExternal

                    state, error = ResearchExternal.source_state(conn, source)
                conn.execute(
                    update(s.task_sources)
                    .where(s.task_sources.c.source_id == source["source_id"])
                    .values(
                        state=state,
                        error_code=error,
                        updated_at=now(),
                    )
                )
        return bool(leaves) or any(
            item["state"] in {"declared", "preparing"} for item in sources.values()
        )

    def get(self, course_id, task_id) -> TaskCoverageView:
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            target = (
                conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task_id))
                .mappings()
                .first()
            )
            if target is None:
                raise AppError("TASK_LAYOUT_LEGACY", "此任务保留原始单元与成果记录", 409)
            leaves = [
                dict(row)
                for row in conn.execute(
                    select(s.task_source_leaves)
                    .join(s.task_sources)
                    .where(s.task_source_leaves.c.task_id == task_id)
                    .order_by(s.task_sources.c.ordinal, s.task_source_leaves.c.ordinal)
                ).mappings()
            ]
            sources = []
            completed = self.completed_inputs(conn, task_id)
            source_rows = list(
                conn.execute(
                    select(s.task_sources)
                    .where(s.task_sources.c.task_id == task_id)
                    .order_by(s.task_sources.c.ordinal)
                ).mappings()
            )
            asset_ids = {row["declaration"].get("asset_id") for row in source_rows}
            labels = (
                dict(
                    conn.execute(
                        select(business.assets.c.asset_id, business.assets.c.display_name)
                        .join(business.course_assets)
                        .where(
                            business.course_assets.c.course_id.in_(
                                {source_course(course_id, row) for row in source_rows}
                            ),
                            business.assets.c.asset_id.in_(asset_ids),
                        )
                    ).all()
                )
                if asset_ids
                else {}
            )
            for row in source_rows:
                owned = [leaf for leaf in leaves if leaf["source_id"] == row["source_id"]]
                sources.append(
                    TaskSourceView(
                        label=row["declaration"].get("label")
                        or row["declaration"].get("relative_path")
                        or labels.get(row["declaration"].get("asset_id"))
                        or (owned[0]["label"] if owned else f"资料 {row['ordinal'] + 1}"),
                        **{
                            key: row[key]
                            for key in (
                                "source_id",
                                "ordinal",
                                "state",
                                "expected_leaf_count",
                                "error_code",
                                "preparation_job_id",
                                "excluded_at_scope_revision",
                            )
                        },
                        # Internal frozen file-member paths/metadata never become tool authority.
                        declaration={
                            key: value for key, value in row["declaration"].items() if key != "stat"
                        },
                        ready_leaf_count=sum(leaf["state"] in {"ready", "empty"} for leaf in owned),
                        processed_leaf_count=sum(
                            self.leaf_processed(conn, leaf, completed.get(leaf["leaf_id"], []))
                            for leaf in owned
                        ),
                    )
                )
            return TaskCoverageView(
                task_id=task_id,
                scope_revision=task["scope_revision"],
                plan_revision=task["plan_revision"],
                sources=sources,
                leaves=[
                    TaskLeafView(
                        **{
                            key: leaf[key]
                            for key in (
                                "leaf_id",
                                "source_id",
                                "ordinal",
                                "label",
                                "locator",
                                "state",
                                "required_representations",
                                "error_code",
                                "excluded_at_scope_revision",
                            )
                        },
                        snapshot_ids=[item["snapshot_id"] for item in leaf["representations"]],
                    )
                    for leaf in leaves
                ],
                target=TaskTargetView(
                    artifact_id=target["reserved_artifact_id"],
                    materialized=target["materialized_artifact_id"] is not None,
                    **{
                        key: target[key]
                        for key in (
                            "mode",
                            "artifact_kind",
                            "title",
                            "expected_revision_id",
                            "state",
                            "last_receipt",
                        )
                    },
                ),
            )

    @staticmethod
    def completed_inputs(conn, task_id):
        result = {}
        rows = conn.execute(
            select(s.run_inputs)
            .join(s.task_runs, s.task_runs.c.run_id == s.run_inputs.c.run_id)
            .join(
                s.task_units,
                (s.task_units.c.task_id == s.task_runs.c.task_id)
                & (s.task_units.c.unit_id == s.task_runs.c.unit_id)
                & (s.task_units.c.run_id == s.task_runs.c.run_id),
            )
            .where(
                s.task_runs.c.task_id == task_id,
                s.task_units.c.state == "completed",
                s.task_units.c.result_artifact_id.is_not(None),
                s.task_units.c.excluded_at_scope_revision.is_(None),
            )
        ).mappings()
        for row in rows:
            result.setdefault(row["leaf_id"], []).append(row)
        return result

    @staticmethod
    def leaf_processed(conn, leaf, rows=None):
        if leaf["state"] == "empty":
            return True
        if leaf["state"] != "ready":
            return False
        rows = (
            rows
            if rows is not None
            else list(
                conn.execute(
                    select(s.run_inputs)
                    .join(s.task_runs, s.task_runs.c.run_id == s.run_inputs.c.run_id)
                    .join(
                        s.task_units,
                        (s.task_units.c.task_id == s.task_runs.c.task_id)
                        & (s.task_units.c.unit_id == s.task_runs.c.unit_id)
                        & (s.task_units.c.run_id == s.task_runs.c.run_id),
                    )
                    .where(
                        s.run_inputs.c.leaf_id == leaf["leaf_id"],
                        s.task_units.c.state == "completed",
                        s.task_units.c.result_artifact_id.is_not(None),
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            )
        )
        for rep in leaf["representations"]:
            matching = [
                row
                for row in rows
                if (row["proof"] or {}).get("representation_snapshot_id") == rep["snapshot_id"]
                and (row["proof"] or {}).get("state") in {"actual_outbound", "fixture_dispatch"}
            ]
            if rep["representation"] == "image":
                if not matching:
                    return False
                continue
            ranges = sorted((row["char_start"], row["char_end"]) for row in matching)
            covered = 0
            for start, end in ranges:
                if start is None or start > covered:
                    return False
                covered = max(covered, end)
            if covered < rep["text_length"]:
                return False
        return True
