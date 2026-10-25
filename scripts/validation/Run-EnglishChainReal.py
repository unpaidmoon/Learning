"""One owned course: exact reading -> translation -> two drafts -> explicit cards.

Uses actual pinned Harness/provider calls and the formal learning interfaces.
All text is self-authored; human actions are explicit synthetic test actions.
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.essay_models import (
    EssayAssessmentInput,
    EssaySuggestionInput,
)
from learning_app.workbench.essays import EssayStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    EssayDraftBlock,
    EssayDraftContent,
    ModelSettings,
)
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.practice_models import (
    ReviewControl,
    ReviewRatingInput,
    ReviewSessionInput,
)
from learning_app.workbench.reviews import ReviewStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "resources/workbench").glob("*.lock.json")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        Path(__file__),
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    out = PROJECT_ROOT / ".runtime/validation" / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "steps": [],
        "input": "self-authored English text and two synthetic essay drafts; no user material",
        "layer": "actual provider + pinned native Harness + persistent formal learning interfaces",
        "user_learning_experience": "not_run",
    }
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        data_dir=out / "data",
        runtime_dir=out / "runtime",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (out / "summary.json").write_text(json.dumps(record), "utf-8")
        return 2
    material = out / "synthetic-course"
    material.mkdir()
    selected = (
        "Use has with he, she, or it. Use have with I, you, we, or they.\n\n"
        "In the simple present, she goes to the library, but I go to the library."
    )
    canaries = ["EXCLUDED_READING_PREFIX", "EXCLUDED_READING_SUFFIX"]
    original = material / "english.txt"
    original.write_bytes((canaries[0] + "\n" + selected + "\n" + canaries[1]).encode("utf-8"))
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    course = CourseFileStore(repo).bind(
        None, str(material), "英语阅读到作文和建卡组合验证"
    )["course_id"]
    opened = CourseFileStore(repo).open(course, original.name)
    full, _ = SourceStore(repo).text(
        course, opened["asset_id"], opened["source_version"]
    )
    begin = full.index(selected)
    source = SourceStore(repo).capture_text(
        course,
        opened["asset_id"],
        opened["source_version"],
        begin,
        begin + len(selected),
    )
    assert source.text == selected
    settings = ModelSettings(
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout,
    )
    service, executor = GenerationService(repo, config), None
    assets, essays, reviews = ArtifactStore(repo), EssayStore(repo), ReviewStore(repo)
    translation = draft = accepted = deck = first = second = None
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()

        async def complete(value):
            task = TaskCoverage(repo).admit(course, value)
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), args.timeout * 5 + 60)
            current = TaskStore(repo).get(course, task.task_id)
            record["steps"].append(current.model_dump(mode="json"))
            if current.state != "completed":
                raise RuntimeError(
                    "ENGLISH_CHAIN_TASK_NOT_COMPLETE:"
                    + current.kind
                    + ":"
                    + current.state
                )
            target = TaskCoverage(repo).get(course, task.task_id).target
            return task, assets.get(course, target.artifact_id)

        translation_input = TaskAdmission(
            operation_id="exact_translation",
            kind="aligned_translation",
            sources_only=True,
            instruction="只将实际选中的两段英文对照译成中文，保留段落，不补前后文或练习。",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "主谓一致英文对照"},
            settings=settings,
        )
        translation_task, translation = await complete(translation_input)
        assert (
            "".join(b.translation.source_text for b in translation.revision.body.blocks)
            == selected
        )
        assert translation.revision.content_role == "ai_generated"
        attempts_before = service.execution.boundary.actual_outbound_attempts
        assert (
            TaskCoverage(repo).admit(course, translation_input).task_id
            == translation_task.task_id
        )
        assert service.execution.boundary.actual_outbound_attempts == attempts_before
        assert not reviews.today(course_id=course).items
        text = (
            "I has a good friend. She go to the library every day. We reads books together. "
            "Reading help me learn new ideas. I wants to write clear English. "
            "Last weekend we visit the library and borrow two interesting books."
        )
        draft = assets.create(
            course,
            ArtifactCreate(
                operation_id="synthetic_actual_draft",
                kind="essay",
                title="我的英语学习习惯",
                body=ArtifactBody(
                    blocks=[
                        EssayDraftBlock(
                            block_id="draft",
                            markdown=text,
                            attribution="user_authored",
                            source_snapshot_ids=[source.snapshot_id],
                            essay=EssayDraftContent(
                                topic="Describe your reading habit and one past visit. This is a short learning draft, not an examination. Grade only the actual writing with the supplied 100-point reference rubric."
                            ),
                        )
                    ]
                ),
            ),
        ).artifact

        async def grade(value, round_number):
            _, feedback = await complete(
                TaskAdmission(
                    operation_id=f"essay_round_{round_number}",
                    kind="essay_review",
                    instruction="按实际作文给参考判分、理由和原文精确定位的可采纳替换建议。保留完整实际稿，不能替用户改写。分数不是正式考试成绩。",
                    target={
                        "mode": "create",
                        "title": f"作文第{round_number + 1}轮参考批改",
                    },
                    essay_review={
                        "artifact_id": value.artifact_id,
                        "revision_id": value.head_revision_id,
                        "block_id": "draft",
                        "expected_round_number": round_number,
                    },
                    settings=settings,
                )
            )
            actual = essays.get(
                course, essays.list(course, value.artifact_id).items[0].round_id
            )
            assert actual.result["status"] == "reference_ready"
            for item in actual.result["annotations"]:
                assert (
                    actual.draft.markdown[item["start"] : item["end"]] == item["quote"]
                )
            return actual, feedback

        first, _ = await grade(draft, 0)
        assert first.draft.markdown == text
        assert (
            assets.get(course, draft.artifact_id).head_revision_id
            == draft.head_revision_id
        )
        annotations = [
            i
            for i, item in enumerate(first.result["annotations"])
            if item["replacement"] != item["quote"]
            and item["kind"] in {"replace", "ins", "del"}
        ]
        if not annotations:
            raise RuntimeError("NO_ACTIONABLE_ESSAY_SUGGESTION")
        override = essays.assess(
            course,
            first.round_id,
            EssayAssessmentInput(
                operation_id="synthetic_user_override",
                expected_revision=first.assessment_revision,
                origin="user_override",
                total=min(100, first.result["reference_evaluation"]["total"] + 3),
                reason="合成用户动作：记录独立改判，保留AI原始参考分，不作为学科评分标准。",
            ),
        )
        accepted = essays.apply_suggestions(
            course,
            first.round_id,
            EssaySuggestionInput(
                operation_id="explicit_accept_one",
                expected_revision_id=draft.head_revision_id,
                annotation_indices=[annotations[0]],
            ),
        ).artifact
        assert (
            accepted.artifact_id == draft.artifact_id
            and accepted.content_role == "mixed"
        )
        second, feedback = await grade(accepted, 1)
        assert first.draft_revision_id != second.draft_revision_id
        assert (
            assets.get(course, draft.artifact_id).head_revision_id
            == accepted.head_revision_id
        )
        assert not reviews.today(course_id=course).items
        record["no_implicit_cards_after_translation_and_two_reviews"] = True
        card_task, deck = await complete(
            TaskAdmission(
                operation_id="explicit_error_cards",
                kind="card_set",
                sources_only=True,
                coverage_mode="complete",
                instruction="现在明确创建两张复习卡，分别练习 has/have 和 goes/go 的主谓一致。每张一个要点，给正确例句。综合实际原英文规则、修订作文及第二轮AI参考反馈；区分三者身份，AI反馈不能冒充教材。不要创建额外练习。",
                sources=[
                    {"kind": "selection", "snapshot_id": source.snapshot_id},
                    {
                        "kind": "artifact",
                        "artifact_id": accepted.artifact_id,
                        "revision_id": accepted.head_revision_id,
                    },
                    {
                        "kind": "artifact",
                        "artifact_id": feedback.artifact_id,
                        "revision_id": feedback.head_revision_id,
                    },
                ],
                target={"mode": "create", "title": "明确创建的主谓一致复习卡"},
                settings=settings,
            )
        )
        cards = [b for b in deck.revision.body.blocks if b.kind == "card"]
        assert len(cards) == 2
        coverage = TaskCoverage(repo).get(course, card_task.task_id)
        roles = {
            SourceStore(repo).snapshot(course, sid).content_role
            for leaf in coverage.leaves
            for sid in leaf.snapshot_ids
        }
        assert {"original", "mixed", "ai_generated"} <= roles
        today = reviews.today(course_id=course)
        queued = [item for item in today.items if item.artifact_id == deck.artifact_id]
        assert len(queued) == 2 and all(x.kind == "card" and x.eligible for x in queued)
        item = queued[0]
        session = reviews.open_session(
            course,
            item.review_id,
            ReviewSessionInput(
                operation_id="synthetic_open_recall",
                expected_revision=item.schedule_revision,
            ),
        )
        reviews.reveal(
            course,
            item.review_id,
            session.session_id,
            ReviewSessionInput(
                operation_id="synthetic_reveal",
                expected_revision=item.schedule_revision,
            ),
        )
        rating = reviews.rate(
            course,
            item.review_id,
            ReviewRatingInput(
                operation_id="synthetic_rate",
                expected_revision=item.schedule_revision,
                session_id=session.session_id,
                rating=3,
            ),
        )
        assert rating.item.scheduler_state["reps"] == 1
        paused = reviews.control(
            course,
            queued[1].review_id,
            ReviewControl(
                operation_id="explicit_pause_card",
                expected_revision=queued[1].schedule_revision,
                action="pause",
            ),
        )
        record.update(
            first_round=first.model_dump(mode="json"),
            second_round=second.model_dump(mode="json"),
            user_override=override.model_dump(mode="json"),
            source_roles=sorted(roles),
            rating=rating.model_dump(mode="json"),
            paused_card=paused.model_dump(mode="json"),
        )
        # Formal exports preserve exact current revisions; no helper backend in the ZIP.
        exported = []
        for artifact in assets.list(course):
            with OfflineExporter(repo).build(
                course, artifact.artifact_id, artifact.head_revision_id
            ) as stream:
                target = out / f"{artifact.artifact_id}.zip"
                with target.open("wb") as handle:
                    shutil.copyfileobj(stream, handle)
            exported.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "revision_id": artifact.head_revision_id,
                    "kind": artifact.kind,
                    "zip": target.name,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        record["exports"] = exported
        record["artifacts"] = [a.model_dump(mode="json") for a in assets.list(course)]
        corpus = CourseCorpus(repo)
        corpus.discover(course, force=True)
        for _ in range(60):
            status = corpus.status(course)
            if status.scan_state == "running":
                corpus.discover(course)
            elif any(x.state in {"pending", "indexing"} for x in status.documents):
                corpus.index_next(course)
            else:
                break
        record["corpus_status"] = corpus.status(course).model_dump(mode="json")
        requests = list(service.execution.boundary.private.glob("*.request.json"))
        assert requests
        for request in requests:
            raw = request.read_text("utf-8")
            assert not any(token in raw for token in canaries)
        assert not any(row.get("images") for row in service.execution.boundary.records)
        record["wire_scope_checks"] = {
            "request_files": len(requests),
            "unselected_canaries": 0,
            "images": 0,
        }
        record["status"] = "passed"
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        with repo.engine.connect() as conn:
            run_rows = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
            record["chat_usage"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
            record["raw_usage"] = [
                dict(row) for row in conn.execute(select(s.run_usage)).mappings()
            ]
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, r["task_id"], r["run_id"])
            .model_dump(mode="json")
            for r in run_rows
        ]
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
        )
        record["boundary_records"] = service.execution.boundary.records
        record["original_unchanged"] = (
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        )
        record["source_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        repo.close()
    if record["status"] == "passed":
        cold = Repository(config.data_dir)
        try:
            assert (
                ArtifactStore(cold).get(course, draft.artifact_id).head_revision_id
                == accepted.head_revision_id
            )
            assert len(EssayStore(cold).list(course, draft.artifact_id).items) == 2
            assert (
                ReviewStore(cold).get(course, queued[1].review_id).queue_state
                == "paused"
            )
            assert (
                ReviewStore(cold)
                .get(course, queued[0].review_id)
                .scheduler_state["reps"]
                == 1
            )
            record["cold_data_reopen"] = "passed; no Host or new provider calls"
        except Exception as error:
            record.update(status="failed", error="COLD_REOPEN:" + str(error))
        finally:
            cold.close()
    if record["source_changed_during_run"] or not record["original_unchanged"]:
        record["status"] = "failed"
    (out / "summary.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {
                key: record.get(key)
                for key in (
                    "status",
                    "error",
                    "actual_outbound_attempts",
                    "source_changed_during_run",
                    "cold_data_reopen",
                )
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
