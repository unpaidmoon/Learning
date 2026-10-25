"""Authenticated user operations; model write tools do not call these routes."""

import asyncio
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from learning_app.course_files import CourseFileOpenResponse, CourseFileStore
from learning_app.errors import AppError

from . import schema as s
from .artifact_scope import install_artifact_scope_routes
from .artifacts import ArtifactStore
from .audio_routes import install_audio_routes
from .audio_sources import AudioSources
from .book_routes import install_book_routes
from .corpus_routes import install_corpus_routes
from .essay_routes import install_essay_routes
from .manifests import ManifestStore
from .models import (
    ArtifactCreate,
    ArtifactReceipt,
    ArtifactRestore,
    ArtifactRevision,
    ArtifactSelectionInput,
    ArtifactUpdate,
    ArtifactView,
    DocumentGapRef,
    DocumentImageRef,
    DocumentRef,
    DocumentSelectionRef,
    LearningOperationView,
    ManifestCreate,
    MessageSelectionInput,
    OcrPageIndex,
    OcrPageLayer,
    PdfDocumentInfo,
    PdfPageContent,
    PdfSelectionInput,
    ReadingOpenInput,
    ReadingState,
    ReadingStateUpdate,
    SearchHit,
    SnapshotSliceRef,
    SourceManifest,
    SourceSnapshot,
    SourceTextRange,
    SubtitleRef,
    TaskAction,
    TaskCreate,
    TaskGapDecision,
    TaskOutline,
    TaskRunView,
    TaskView,
    TextSelectionInput,
)
from .offline_routes import install_offline_routes
from .path_routes import install_path_routes
from .practice_routes import install_practice_routes
from .preparations import PreparationStore
from .processing_models import (
    CurrentPagePreparation,
    PreparationControl,
    PreparationCreate,
    PreparationProgress,
    PreparationView,
    ReadingFocusInput,
)
from .reading import DocumentReader
from .research_routes import install_research_routes
from .runs import TaskRunStore
from .sources import SourceStore
from .task_controls import TaskRecovery
from .task_coverage import TaskCoverage
from .task_scope_models import SourceGapDecision, TaskAdmission, TaskCoverageView
from .tasks import TaskStore
from .workspace import (
    ArtifactIndexPage,
    RevisionIndexPage,
    TaskIndexPage,
    WorkspaceQuery,
    WorkspaceStore,
)


def install_workbench_routes(app: FastAPI) -> None:
    install_artifact_scope_routes(app)
    install_offline_routes(app)
    install_audio_routes(app)
    install_practice_routes(app)
    install_path_routes(app)
    install_essay_routes(app)
    install_corpus_routes(app)
    install_book_routes(app)
    install_research_routes(app)
    prefix = "/api/v1/courses/{course_id}/learning"

    def artifacts() -> ArtifactStore:
        return ArtifactStore(app.state.repository)

    def sources() -> SourceStore:
        return SourceStore(app.state.repository)

    def tasks() -> TaskStore:
        return TaskStore(app.state.repository)

    @app.get(prefix + "/artifact-index", response_model=ArtifactIndexPage)
    def artifact_index(
        course_id: str,
        cursor: str | None = Query(None, max_length=300),
        limit: int = Query(30, ge=1, le=100),
        query: str = Query("", max_length=200),
    ):
        return WorkspaceStore(app.state.repository).artifacts(
            course_id, WorkspaceQuery(cursor=cursor, limit=limit, query=query)
        )

    @app.get(prefix + "/task-index", response_model=TaskIndexPage)
    def task_index(
        course_id: str,
        cursor: str | None = Query(None, max_length=300),
        limit: int = Query(30, ge=1, le=100),
        query: str = Query("", max_length=200),
    ):
        return WorkspaceStore(app.state.repository).tasks(
            course_id, WorkspaceQuery(cursor=cursor, limit=limit, query=query)
        )

    @app.get(prefix + "/artifacts/{artifact_id}/versions", response_model=RevisionIndexPage)
    def revision_index(
        course_id: str,
        artifact_id: str,
        cursor: str | None = Query(None, max_length=300),
        limit: int = Query(30, ge=1, le=100),
    ):
        return WorkspaceStore(app.state.repository).revisions(
            course_id, artifact_id, WorkspaceQuery(cursor=cursor, limit=limit)
        )

    @app.get(
        prefix + "/artifacts/{artifact_id}/versions/{revision_id}", response_model=ArtifactRevision
    )
    def exact_revision(course_id: str, artifact_id: str, revision_id: str):
        return WorkspaceStore(app.state.repository).revision(course_id, artifact_id, revision_id)

    @app.post(prefix + "/task-intents", response_model=TaskView)
    async def admit_task(course_id: str, value: TaskAdmission):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未接受新的任务")
            result = await asyncio.to_thread(
                TaskCoverage(app.state.repository).admit, course_id, value
            )
        app.state.task_executor.schedule(course_id, result.task_id)
        return result

    @app.get(prefix + "/tasks/{task_id}/coverage", response_model=TaskCoverageView)
    def task_coverage(course_id: str, task_id: str):
        return TaskCoverage(app.state.repository).get(course_id, task_id)

    @app.post(prefix + "/tasks/{task_id}/source-gap", response_model=TaskView)
    async def task_source_gap(course_id: str, task_id: str, value: SourceGapDecision):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未改变任务范围")
            result = TaskRecovery(app.state.repository).source_gap(course_id, task_id, value)
        app.state.task_executor.schedule(course_id, task_id)
        return result

    @app.post(prefix + "/tasks/{task_id}/work/{unit_id}/retry", response_model=TaskView)
    async def retry_task_work(course_id: str, task_id: str, unit_id: str, value: TaskAction):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未重试生成工作")
            result = TaskRecovery(app.state.repository).retry_work(
                course_id, task_id, unit_id, value
            )
        app.state.task_executor.schedule(course_id, task_id)
        return result

    @app.get(prefix + "/preparations", response_model=list[PreparationView])
    def list_preparations(course_id: str):
        return PreparationStore(app.state.repository).list(course_id)

    @app.get(prefix + "/preparations/{job_id}", response_model=PreparationView)
    def preparation(course_id: str, job_id: str):
        return PreparationStore(app.state.repository).get(course_id, job_id)

    @app.get(prefix + "/preparations/{job_id}/summary", response_model=PreparationProgress)
    def preparation_summary(course_id: str, job_id: str):
        return PreparationStore(app.state.repository).summary(course_id, job_id)

    @app.post(prefix + "/preparations", response_model=PreparationProgress)
    async def prepare_source(course_id: str, value: PreparationCreate):
        if app.state.exit_prepared:
            raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未准备新的资料处理")
        definition = await asyncio.to_thread(
            PreparationStore(app.state.repository).define_source,
            course_id,
            value,
            app.state.config.project_root,
        )
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未启动新的资料处理")
            result = PreparationStore(app.state.repository).publish_pdf(definition, summary=True)
        app.state.preparations.wake()
        return result

    @app.post(prefix + "/preparations/{job_id}/{action}", response_model=PreparationView)
    async def control_preparation(
        course_id: str,
        job_id: str,
        action: Literal["pause", "resume", "cancel", "retry"],
        value: PreparationControl,
    ):
        async with app.state.submission_gate:
            if app.state.exit_prepared and action in {"resume", "retry"}:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未恢复资料处理")
            result = await asyncio.to_thread(
                PreparationStore(app.state.repository).control, course_id, job_id, action, value
            )
        app.state.preparations.interrupt(job_id)
        return result

    document_prefix = prefix + "/documents/{asset_id}/{source_version}"

    @app.get(document_prefix, response_model=PdfDocumentInfo)
    def document_info(course_id: str, asset_id: str, source_version: str):
        return DocumentReader(app.state.repository).info(course_id, asset_id, source_version)

    @app.get(document_prefix + "/pages/{page_index}", response_model=PdfPageContent)
    def document_page(course_id: str, asset_id: str, source_version: str, page_index: int):
        return DocumentReader(app.state.repository).page(
            course_id, asset_id, source_version, page_index
        )

    @app.post(document_prefix + "/reading-state", response_model=ReadingState)
    def begin_reading(course_id: str, asset_id: str, source_version: str, value: ReadingOpenInput):
        return DocumentReader(app.state.repository).begin(
            course_id, asset_id, source_version, value
        )

    @app.get(document_prefix + "/ocr/{page_index}", response_model=OcrPageLayer)
    def ocr_page_layer(course_id: str, asset_id: str, source_version: str, page_index: int):
        return DocumentReader(app.state.repository).ocr_layer(
            course_id, asset_id, source_version, page_index
        )

    @app.get(document_prefix + "/ocr-index", response_model=OcrPageIndex)
    def ocr_page_index(
        course_id: str,
        asset_id: str,
        source_version: str,
        after_page: int = Query(default=-1, ge=-1),
    ):
        return DocumentReader(app.state.repository).ocr_index(
            course_id, asset_id, source_version, after_page
        )

    @app.post(document_prefix + "/focus/release")
    async def release_reading_focus(
        course_id: str, asset_id: str, source_version: str, value: ReadingFocusInput
    ):
        async with app.state.submission_gate:
            released = DocumentReader(app.state.repository).release_focus(
                course_id, asset_id, source_version, value
            )
        return {"released": released}

    @app.post(document_prefix + "/prepare-current", response_model=CurrentPagePreparation)
    async def prepare_current_page(
        course_id: str, asset_id: str, source_version: str, value: ReadingFocusInput
    ):
        reader, store = DocumentReader(app.state.repository), PreparationStore(app.state.repository)
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未准备资料")
            reader.focus(course_id, asset_id, source_version, value)
        request = PreparationCreate(
            kind="pdf_ocr",
            asset_id=asset_id,
            source_version=source_version,
            priority_unit=value.page_index,
        )
        limitation = None
        try:
            definition = await asyncio.to_thread(
                store.define_pdf, course_id, request, app.state.config.project_root
            )
        except AppError as exc:
            if exc.code != "PREPARATION_UNIT_LIMIT":
                raise
            limitation = exc.code
            definition = None
        # Define a possible single-page job without a second PDF parse.
        if definition:
            payload = definition["payload"]
            single = {
                "payload": {
                    **payload,
                    "scope": "unit",
                    "parameters": {**payload["parameters"], "scope_indexes": [value.page_index]},
                },
                "priority_unit": value.page_index,
            }
        else:
            single = await asyncio.to_thread(
                store.define_pdf,
                course_id,
                request.model_copy(update={"scope": "unit", "unit_index": value.page_index}),
                app.state.config.project_root,
            )
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未启动资料处理")
            with app.state.repository._lock:
                reader.assert_focus(course_id, asset_id, source_version, value)
                batch = store.publish_pdf(definition, summary=True) if definition else None
                current = batch
                if batch is None or batch.state in {
                    "paused",
                    "cancelled",
                    "failed",
                    "needs_source",
                }:
                    current = store.publish_pdf(single, summary=True)
        app.state.preparations.wake()
        return CurrentPagePreparation(batch=batch, current=current, limitation_code=limitation)

    @app.post(document_prefix + "/reading-state/save", response_model=ReadingState)
    def save_reading(course_id: str, asset_id: str, source_version: str, value: ReadingStateUpdate):
        return DocumentReader(app.state.repository).save(course_id, asset_id, source_version, value)

    @app.post(prefix + "/source-manifests", response_model=SourceManifest)
    def create_manifest(course_id: str, value: ManifestCreate):
        return ManifestStore(app.state.repository).create(course_id, value)

    @app.get(prefix + "/source-manifests/{manifest_id}", response_model=SourceManifest)
    def manifest(course_id: str, manifest_id: str):
        return ManifestStore(app.state.repository).get(course_id, manifest_id)

    @app.get(prefix + "/tasks/{task_id}/runs/{run_id}", response_model=TaskRunView)
    def run(course_id: str, task_id: str, run_id: str):
        return TaskRunStore(app.state.repository).get(course_id, task_id, run_id)

    @app.get(prefix + "/artifacts", response_model=list[ArtifactView])
    def list_artifacts(course_id: str):
        return artifacts().list(course_id)

    @app.post(prefix + "/artifacts", response_model=ArtifactReceipt)
    def create_artifact(course_id: str, value: ArtifactCreate):
        return artifacts().create(course_id, value)

    @app.get(prefix + "/artifacts/{artifact_id}", response_model=ArtifactView)
    def artifact(course_id: str, artifact_id: str):
        return artifacts().get(course_id, artifact_id)

    @app.post(prefix + "/artifacts/{artifact_id}/update", response_model=ArtifactReceipt)
    def update_artifact(course_id: str, artifact_id: str, value: ArtifactUpdate):
        return artifacts().update(course_id, artifact_id, value)

    @app.get(prefix + "/artifacts/{artifact_id}/revisions", response_model=list[ArtifactRevision])
    def artifact_history(course_id: str, artifact_id: str):
        return artifacts().history(course_id, artifact_id)

    @app.post(prefix + "/artifacts/{artifact_id}/restore", response_model=ArtifactReceipt)
    def restore_artifact(course_id: str, artifact_id: str, value: ArtifactRestore):
        return artifacts().restore(course_id, artifact_id, value)

    @app.get(prefix + "/search", response_model=list[SearchHit])
    def search(
        course_id: str,
        query: str = Query(min_length=1, max_length=500),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        return artifacts().search(course_id, query, limit=limit)

    @app.post(prefix + "/selections/text", response_model=SourceSnapshot)
    def select_text(course_id: str, value: TextSelectionInput):
        return sources().capture_text(
            course_id,
            value.asset_id,
            value.source_version,
            value.start,
            value.end,
        )

    @app.post(prefix + "/selections/artifact", response_model=SourceSnapshot)
    def select_artifact(course_id: str, value: ArtifactSelectionInput):
        return sources().capture_artifact(
            course_id,
            value.artifact_id,
            value.revision_id,
            value.block_id,
        )

    @app.post(prefix + "/selections/pdf", response_model=SourceSnapshot)
    def select_pdf(course_id: str, value: PdfSelectionInput):
        return DocumentReader(app.state.repository).capture(course_id, value)

    @app.post(prefix + "/selections/message", response_model=SourceSnapshot)
    def select_message(course_id: str, value: MessageSelectionInput):
        return sources().capture_message(
            course_id,
            value.message_id,
            value.revision_id,
            value.start,
            value.end,
        )

    @app.get(prefix + "/selections/{snapshot_id}", response_model=SourceSnapshot)
    def selection(course_id: str, snapshot_id: str):
        return sources().snapshot(course_id, snapshot_id)

    @app.post(prefix + "/selections/{snapshot_id}/slice", response_model=SourceSnapshot)
    def selection_slice(course_id: str, snapshot_id: str, value: SourceTextRange):
        return sources().slice(
            sources().snapshot(course_id, snapshot_id),
            value.start,
            value.end,
            owner_course_id=course_id,
        )

    @app.get(prefix + "/selections/{snapshot_id}/image", response_class=FileResponse)
    def selection_image(course_id: str, snapshot_id: str):
        snapshot = sources().snapshot(course_id, snapshot_id)
        if snapshot.reference.kind not in {
            "document_image",
            "document_region_image",
            "submitted_image",
        }:
            raise AppError("SOURCE_IMAGE_DENIED", "此快照未包含已授权图像", 403)
        sources().cached_image(snapshot)
        return FileResponse(
            sources().image_path(snapshot.reference.image_sha256),
            media_type=getattr(snapshot.reference, "media_type", "image/png"),
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post(prefix + "/selections/{snapshot_id}/open", response_model=CourseFileOpenResponse)
    def open_selection_source(course_id: str, snapshot_id: str):
        snapshot = sources().snapshot(course_id, snapshot_id)
        reference = snapshot.reference
        while isinstance(reference, SnapshotSliceRef):
            snapshot = sources().snapshot(snapshot.course_id, reference.parent_snapshot_id)
            reference = snapshot.reference
        if isinstance(reference, SubtitleRef):
            original = AudioSources(app.state.repository).current(
                snapshot.course_id, reference.asset_id, reference.source_version
            )
        elif isinstance(
            reference, (DocumentRef, DocumentSelectionRef, DocumentGapRef, DocumentImageRef)
        ):
            original = sources()._source(
                snapshot.course_id, reference.asset_id, reference.source_version
            )
        else:
            raise AppError(
                "SOURCE_JUMP_KIND_UNSUPPORTED", "该来源须从对应的成果或对话视图打开", 422
            )
        if original["content_hash"] != reference.source_digest:
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "来源摘要与已保存选区不符")
        files = CourseFileStore(app.state.repository)
        root = files._root(snapshot.course_id)
        opened = files.open(snapshot.course_id, original["path"].relative_to(root).as_posix())
        if (
            opened["asset_id"] != reference.asset_id
            or opened["source_version"] != reference.source_version
        ):
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "原版本已改变，未跳到新版来源")
        return opened

    @app.get(prefix + "/tasks", response_model=list[TaskView])
    def list_tasks(course_id: str):
        return tasks().list(course_id)

    @app.post(prefix + "/tasks", response_model=TaskView)
    async def create_task(course_id: str, value: TaskCreate):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未接收新的学习生成")
            result = await asyncio.to_thread(tasks().create, course_id, value)
        app.state.task_executor.schedule(course_id, result.task_id)
        return result

    @app.get(prefix + "/tasks/{task_id}", response_model=TaskView)
    def task(course_id: str, task_id: str):
        return tasks().get(course_id, task_id)

    @app.post(prefix + "/tasks/{task_id}/outline", response_model=TaskView)
    def confirm_outline(course_id: str, task_id: str, value: TaskOutline):
        return tasks().confirm_outline(course_id, task_id, value)

    @app.post(prefix + "/tasks/{task_id}/gap-decision", response_model=TaskView)
    async def decide_gap(course_id: str, task_id: str, value: TaskGapDecision):
        result = await asyncio.to_thread(tasks().decide_gap, course_id, task_id, value)
        app.state.task_executor.schedule(course_id, task_id)
        return result

    @app.post(prefix + "/tasks/{task_id}/control/{action}", response_model=TaskView)
    async def control_task(
        course_id: str,
        task_id: str,
        action: Literal["pause", "resume", "cancel"],
        value: TaskAction,
    ):
        async with app.state.submission_gate:
            if action == "resume" and app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未恢复新的学习生成")
            result = tasks().control(course_id, task_id, action, value)
        if action in {"pause", "cancel"}:
            executor = getattr(app.state, "task_executor", None)
            if executor is not None:
                await executor.cancel_task(course_id, task_id)
        elif action == "resume":
            app.state.task_executor.schedule(course_id, task_id)
        return result

    @app.post(prefix + "/tasks/{task_id}/reconcile", response_model=TaskView)
    async def reconcile_task(course_id: str, task_id: str):
        app.state.task_executor.request_reconciliation(course_id, task_id)
        return tasks().reconcile(course_id, task_id)

    @app.get(prefix + "/operations/{operation_id}", response_model=LearningOperationView)
    def operation(course_id: str, operation_id: str):
        with app.state.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.operation_id == operation_id,
                        s.operations.c.course_id == course_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("LEARNING_OPERATION_NOT_FOUND", "尚无该操作的持久回执", 404)
        return LearningOperationView(
            **{
                key: row[key]
                for key in ("operation_id", "course_id", "kind", "result", "created_at")
            }
        )
