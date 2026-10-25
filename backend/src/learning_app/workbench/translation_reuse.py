"""Reuse a settled saved translation; never clone its run, usage, or mutable text."""

import hashlib
import secrets
import threading

from pydantic import Field
from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .artifacts import ArtifactStore
from .common import prior_operation, save_operation
from .models import (
    Contract,
    Identity,
    ModelSettings,
    SourceSnapshot,
    TaskView,
    TranslationReuseInfo,
)
from .runs import TaskRunStore
from .sources import SourceStore
from .tasks import TaskStore


class TranslationPolicyView(Contract):
    course_id: Identity
    chat_id: Identity
    settings: ModelSettings
    policy_id: str | None


class TranslationCacheEntry(Contract):
    status: str
    task_id: Identity
    policy_id: str | None = None
    settings: ModelSettings | None = None
    instruction: str | None = None
    source: SourceSnapshot | None = None
    text: str | None = Field(default=None, max_length=200_000)


class TranslationReuse:
    def __init__(self, repository, config, execution):
        self.repository, self.config, self.execution = repository, config, execution
        self.sources = SourceStore(repository)
        self._nonce = secrets.token_hex(32)
        self._guard = threading.RLock()
        self._admissions, self._dispatches = {}, {}

    def policy_id(self):
        execution = self.execution()
        if execution is None:
            return None
        scope = getattr(execution, "translation_cache_scope", None)
        if not scope:
            return None
        return _hash(
            {"instance": self.config.instance_id, "nonce": self._nonce, "provider_scope": scope}
        )

    def remember_admission(self, task):
        if task.kind == "translation":
            with self._guard:
                self._admissions.setdefault(task.task_id, self.policy_id())
                if len(self._admissions) > 512:
                    self._admissions.pop(next(iter(self._admissions)))

    def remember_dispatch(self, run_id):
        with self._guard:
            self._dispatches[run_id] = self.policy_id()
            if len(self._dispatches) > 512:
                self._dispatches.pop(next(iter(self._dispatches)))

    @staticmethod
    def same_text_range(left, right):
        if left.snapshot_id == right.snapshot_id:
            return True
        if (
            left.reference.kind not in {"document", "document_selection"}
            or right.reference.kind != left.reference.kind
        ):
            return False

        def scope(source):
            reference = source.reference.model_dump(mode="json")
            reference.pop("regions", None)
            if "parts" in reference:
                reference["parts"] = [
                    {key: value for key, value in part.items() if key != "regions"}
                    for part in reference["parts"]
                ]
            return {
                "course": source.course_id,
                "role": source.content_role,
                "text": source.text,
                "reference": reference,
            }

        return scope(left) == scope(right)

    def _completed(self, conn, task, policy):
        if (
            task.kind != "translation"
            or task.layout_version != 1
            or task.state != "completed"
            or len(task.units) != 1
            or policy is None
        ):
            return None
        unit = task.units[0]
        if unit.state != "completed" or not unit.run_id or not unit.result_artifact_id:
            return None
        with self._guard:
            if self._dispatches.get(unit.run_id) != policy:
                return None
        run = TaskRunStore._row(conn, unit.run_id)
        if run["state"] != "completed" or run["cancel_requested"] or run["error_code"]:
            return None
        if not TaskRunStore.input_proofs_settled(conn, unit.run_id):
            return None
        view = TaskRunStore._view(conn, unit.run_id)
        if view.usage_status != "known":
            return None
        publication = conn.execute(
            select(s.operations.c.result).where(
                s.operations.c.course_id == task.course_id,
                s.operations.c.kind == "artifact_create",
                s.operations.c.operation_id == "publish_" + unit.run_id,
            )
        ).scalar_one_or_none()
        if not publication or publication["outcome"] != "applied":
            return None
        artifact = ArtifactStore._view(conn, task.course_id, unit.result_artifact_id)
        if (
            artifact.kind != "translation"
            or artifact.head_revision_id != publication["artifact"]["head_revision_id"]
            or artifact.revision.author != "model"
            or artifact.content_role != "ai_generated"
        ):
            return None
        blocks = artifact.revision.body.blocks
        if any(
            block.kind != "markdown" or block.source_snapshot_ids != [unit.source_snapshot_id]
            for block in blocks
        ):
            return None
        text = "\n\n".join(block.markdown for block in blocks)
        if not text.strip() or len(text) > 200_000:
            return None
        return {
            "text": text,
            "artifact_id": artifact.artifact_id,
            "revision_id": artifact.head_revision_id,
            "run_id": unit.run_id,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    def entry(self, course_id, task_id):
        task = TaskStore(self.repository).get(course_id, task_id)
        if task.kind != "translation" or task.layout_version != 1 or len(task.units) != 1:
            return TranslationCacheEntry(status="ineligible", task_id=task_id)
        if task.state == "unknown":
            return TranslationCacheEntry(status="unknown", task_id=task_id)
        if task.state in {"queued", "running", "paused"}:
            return TranslationCacheEntry(status="pending", task_id=task_id)
        policy = self.policy_id()
        with self.repository.engine.connect() as conn:
            result = self._completed(conn, task, policy)
        if not result:
            return TranslationCacheEntry(status="ineligible", task_id=task_id)
        source = self.sources.snapshot(course_id, task.units[0].source_snapshot_id)
        try:
            self.sources.require_current(source)
        except AppError:
            return TranslationCacheEntry(status="ineligible", task_id=task_id)
        if not source.text.strip() or source.reference.kind == "document_image":
            return TranslationCacheEntry(status="ineligible", task_id=task_id)
        return TranslationCacheEntry(
            status="eligible",
            task_id=task_id,
            policy_id=policy,
            settings=task.settings,
            instruction=task.instruction,
            source=source,
            text=result["text"],
        )

    def reuse(self, course_id, value, candidate_id):
        payload = value.model_dump(mode="json")
        with self.repository._lock:
            with self.repository.engine.connect() as conn:
                prior = prior_operation(conn, course_id, "task_create", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            if (
                not candidate_id
                or value.kind != "translation"
                or len(value.units) != 1
                or value.scope_manifest_id
            ):
                return None
            source = self.sources.snapshot(course_id, value.units[0].source_snapshot_id)
            self.sources.require_current(source)
            with self.repository.engine.begin() as conn:
                try:
                    task = TaskStore._view(conn, course_id, candidate_id)
                except AppError:
                    return None
                if (
                    task.kind != "translation"
                    or task.layout_version != 1
                    or len(task.units) != 1
                    or task.chat_id != value.chat_id
                    or task.instruction != value.instruction
                    or task.settings != value.settings
                    or task.coverage_mode != value.coverage_mode
                    or task.sources_only != value.sources_only
                    or task.allow_external != value.allow_external
                    or task.scope_manifest_id is not None
                    or task.outline != value.outline
                    or task.units[0].label != value.units[0].label
                ):
                    return None
                original_source = SourceStore.in_transaction(
                    conn, course_id, task.units[0].source_snapshot_id
                )
                if not self.same_text_range(original_source, source):
                    return None
                if task.state == "unknown":
                    raise AppError(
                        "TASK_RECONCILIATION_REQUIRED", "已有相同翻译的执行结果待核对，未重新发送"
                    )
                policy = self.policy_id()
                if policy is None:
                    return None
                result = self._completed(conn, task, policy)
                with self._guard:
                    pending_policy = self._admissions.get(task.task_id)
                if result is None and not (
                    task.state in {"queued", "running", "paused"} and pending_policy == policy
                ):
                    return None
                info = TranslationReuseInfo(
                    operation_id=value.operation_id,
                    kind="saved_result" if result else "in_progress",
                    original_task_id=task.task_id,
                    artifact_id=result["artifact_id"] if result else None,
                    revision_id=result["revision_id"] if result else None,
                    text_sha256=result["text_sha256"] if result else None,
                    original_run_id=result["run_id"] if result else task.units[0].run_id,
                    requested_source_snapshot_id=source.snapshot_id,
                )
                response = task.model_copy(update={"translation_reuse": info})
                save_operation(
                    conn, course_id, "task_create", value.operation_id, payload, response
                )
                self.repository._hook("translation_reuse_before_commit", task_id=task.task_id)
            self.repository._hook("translation_reuse_after_commit", task_id=task.task_id)
            return response
