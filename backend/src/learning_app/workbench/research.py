"""Research projections over the existing task/run ledger, never a second queue."""

from deeptutor_reuse.research_records import (
    ResearchTopicView,
    TopicBlock,
    TopicStatus,
    TraceReference,
)
from deeptutor_reuse.research_recovery import review_research_recovery
from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .models import SourceSnapshot
from .research_models import (
    ResearchCheckpoint,
    ResearchEvidence,
    ResearchExternalReadView,
    ResearchScope,
    ResearchView,
)
from .sources import SourceStore
from .tasks import TaskStore


class ResearchStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def topics(task):
        request = ResearchScope.model_validate((task["scope_spec"] or {}).get("research") or {})
        # The full instruction stays in the prompt. This is a display label,
        # not a lossy substitute for the research question or source scope.
        return [
            {"topic_id": "topic_" + _hash({"title": title})[:32], "title": title}
            for title in (request.topics or [task["instruction"][:200]])
        ]

    @classmethod
    def plans(cls, task, batches, external=None):
        return [
            (items, {**topic, "phase": "evidence", **({"external": external} if external else {})})
            for topic in cls.topics(task)
            for items in batches
        ]

    @classmethod
    def validate_work(cls, task, spec):
        context = spec.get("research")
        if (
            task["kind"] != "research"
            or not context
            or context.get("phase") not in {"evidence", "synthesis"}
            or {key: context.get(key) for key in ("topic_id", "title")} not in cls.topics(task)
        ):
            raise AppError("RESEARCH_WORK_SCOPE_CHANGED", "研究工作不属于原主题与阶段", 403)
        return context

    @staticmethod
    def _evidence(conn, task, unit):
        context = unit["work_spec"]["research"]
        inherited = context["phase"] == "synthesis"
        if inherited:
            from .research_synthesis import ResearchSynthesis

            wanted = set(
                ResearchSynthesis.validate(
                    conn, task, context, before_ordinal=unit["ordinal"], allow_retired=True
                )
            )
        else:
            wanted = None
        rows = conn.execute(
            select(s.run_inputs)
            .join(s.task_runs)
            .where(
                s.task_runs.c.task_id == task["task_id"],
                *([] if inherited else [s.run_inputs.c.run_id == unit["run_id"]]),
            )
        ).mappings()
        result = {}
        for row in rows:
            if (row["proof"] or {}).get("state") not in {"actual_outbound", "fixture_dispatch"}:
                continue
            if wanted is not None and row["snapshot_id"] not in wanted:
                continue
            result.setdefault(
                row["snapshot_id"],
                ResearchEvidence(
                    source=SourceStore.in_transaction(conn, task["course_id"], row["snapshot_id"]),
                    read_in_run_id=row["run_id"],
                    from_current_run=row["run_id"] == unit["run_id"],
                ),
            )
        from .research_external import ResearchExternal

        for producer in conn.execute(
            select(s.task_runs.c.run_id).where(s.task_runs.c.task_id == task["task_id"])
        ).scalars():
            if not inherited and producer != unit["run_id"]:
                continue
            for identity in ResearchExternal.cited_sources(conn, producer):
                if wanted is not None and identity not in wanted:
                    continue
                result.setdefault(
                    identity,
                    ResearchEvidence(
                        source=SourceStore.in_transaction(conn, task["course_id"], identity),
                        read_in_run_id=producer,
                        from_current_run=producer == unit["run_id"],
                    ),
                )
        return list(result.values())

    def evidence(self, course_id, task_id, unit_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            unit = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.unit_id == unit_id,
                    )
                )
                .mappings()
                .first()
            )
            if (
                task["kind"] != "research"
                or not unit
                or not (unit["work_spec"] or {}).get("research")
            ):
                raise AppError("RESEARCH_WORK_NOT_FOUND", "研究工作不属于此任务", 404)
            return self._evidence(conn, task, unit)

    def get(self, course_id, task_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["kind"] != "research":
                raise AppError("RESEARCH_TASK_REQUIRED", "此任务不是研究任务", 404)
            units = list(
                conn.execute(
                    select(s.task_units)
                    .where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                    .order_by(s.task_units.c.ordinal)
                ).mappings()
            )
            records, checkpoints = [], []
            available_sources, available_evidence = set(), {}
            for unit in units:
                context = (unit["work_spec"] or {}).get("research")
                if not context:
                    continue
                actual = self._evidence(conn, task, unit)
                references, traces = [], []
                # Prepared source identities are not proof of a read.
                for prepared in unit["work_spec"]["inputs"]:
                    snapshot = SourceStore.in_transaction(conn, course_id, prepared["snapshot_id"])
                    reference = {
                        "asset_id": snapshot.snapshot_id,
                        "source_version": snapshot.content_hash,
                        "content_role": snapshot.content_role,
                    }
                    references.append(reference)
                    available_sources.add(tuple(reference.values()))
                for item in actual:
                    snapshot: SourceSnapshot = item.source
                    reference = {
                        "asset_id": snapshot.snapshot_id,
                        "source_version": snapshot.content_hash,
                        "content_role": snapshot.content_role,
                    }
                    references.append(reference)
                    available_sources.add(tuple(reference.values()))
                    available_evidence[(snapshot.snapshot_id, snapshot.content_hash)] = (
                        snapshot.content_hash
                    )
                    traces.append(
                        TraceReference(
                            tool_id=unit["unit_id"],
                            citation_id=snapshot.snapshot_id,
                            tool_type="frozen_task_input",
                            query=context["title"],
                            summary="",
                            evidence_asset_id=snapshot.snapshot_id,
                            evidence_version=snapshot.content_hash,
                            evidence_sha256=snapshot.content_hash,
                            operation_id=item.read_in_run_id,
                        )
                    )
                status = (
                    TopicStatus.COMPLETED
                    if unit["state"] == "completed"
                    else TopicStatus.RESEARCHING
                    if unit["state"] in {"dispatching", "unknown"}
                    else TopicStatus.FAILED
                    if unit["state"] in {"failed", "missing"}
                    else TopicStatus.PENDING
                )
                record = TopicBlock(
                    block_id=unit["unit_id"],
                    sub_topic=context["title"],
                    overview=task["instruction"],
                    status=status,
                    tool_traces=traces,
                    created_at=task["created_at"],
                    updated_at=task["updated_at"],
                    metadata={"source_refs": references},
                )
                # The projection never turns a merely prepared input into a
                # completed read, or a title match into source identity.
                records.append(record)
                checkpoints.append(
                    ResearchCheckpoint(
                        unit_id=unit["unit_id"],
                        topic_id=context["topic_id"],
                        title=context["title"],
                        state=unit["state"],
                        phase=context["phase"],
                        run_id=unit["run_id"],
                        error_code=unit["error_code"],
                        evidence_count=len(traces),
                        evidence_from_prior_runs=sum(not i.from_current_run for i in actual),
                    )
                )
            review = review_research_recovery(
                records,
                available_sources=frozenset(available_sources),
                available_evidence=available_evidence,
            )
            statistics = ResearchTopicView(tuple(records)).get_statistics()
            statistics["evidence_references"] = statistics.pop("total_tool_calls")
            from .research_schema import external_reads

            reads = []
            for read in conn.execute(
                select(external_reads)
                .where(external_reads.c.task_id == task_id)
                .order_by(external_reads.c.created_at)
            ).mappings():
                responses = [e for e in read["network_events"] if e["kind"] == "response_received"]
                reads.append(
                    ResearchExternalReadView(
                        effect_id=read["effect_id"],
                        run_id=read["run_id"],
                        kind=read["native_call"]["tool_name"],
                        state=read["state"],
                        query=read["request"].get("query"),
                        url=read["request"].get("url"),
                        transport_source=read["request"].get("transport_source", "unknown"),
                        requests_started=sum(
                            e["kind"] == "request_started" for e in read["network_events"]
                        ),
                        responses_observed=len(responses),
                        last_http_status=responses[-1]["facts"]["http_status"]
                        if responses
                        else None,
                        error_code=read["error_code"],
                        created_at=read["created_at"],
                        finished_at=read["finished_at"],
                        reused_evidence=bool((read["result"] or {}).get("reused_evidence")),
                    )
                )
            return ResearchView(
                task_id=task_id,
                task_revision=task["revision"],
                allow_external=task["allow_external"],
                topics=[t["title"] for t in self.topics(task)],
                checkpoints=checkpoints,
                statistics=statistics,
                reconciliation_required=sorted(
                    set(review.reconciliation_required)
                    | {u["unit_id"] for u in units if u["state"] in {"dispatching", "unknown"}}
                ),
                source_decision_required=[
                    identity
                    for identity, _ in review.blocked
                    if next(u for u in units if u["unit_id"] == identity)["state"]
                    in {"completed", "failed", "missing"}
                ],
                external_reads=reads,
            )
