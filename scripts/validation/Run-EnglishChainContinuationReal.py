"""Continue known parts with a distinct, explicitly edited synthetic draft.

The original unknown attempt is never replayed or relabeled. This validation
clones owned frozen data, reconciles it read-only, and submits a new revision.
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.essays import EssayStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ArtifactUpdate, BlockChange, ModelSettings
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.practice_models import (
    ReviewControl,
    ReviewRatingInput,
    ReviewSessionInput,
)
from learning_app.workbench.reviews import ReviewStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def hashes(directory):
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def freeze():
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in [
            *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
            *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
            PROJECT_ROOT / "harness/runtime.lock.json",
            Path(__file__),
        ]
    }


async def run(args):
    for value in (args.run_id, args.source_run):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("OWNED_RUN_ID_INVALID")
    base = (PROJECT_ROOT / ".runtime/validation").resolve()
    old, out = base / args.source_run, base / args.run_id
    if old.resolve().parent != base or out.resolve().parent != base:
        raise ValueError("OWNED_RUN_PATH_INVALID")
    previous = json.loads((old / "summary.json").read_text("utf-8"))
    assert previous["steps"][-1]["state"] == "unknown"
    original_before = hashes(old / "data")
    out.mkdir(exist_ok=False)
    shutil.copytree(old / "data", out / "data")
    record = {
        "status": "not_run",
        "source_run": args.source_run,
        "source_before": freeze(),
        "input": "owned data clone; new explicitly edited synthetic draft; old unknown remains unknown",
        "layer": "actual provider + pinned Host; formal operations; synthetic user actions",
        "user_learning_experience": "not_run",
        "steps": [],
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
    repo = Repository(config.data_dir)
    old_task = previous["steps"][-1]
    course = old_task["course_id"]
    old_run = old_task["units"][0]["run_id"]
    assets, essays, reviews = ArtifactStore(repo), EssayStore(repo), ReviewStore(repo)
    service, executor = GenerationService(repo, config), None
    settings = ModelSettings(
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout,
    )
    try:
        await service.open()
        record["old_attempt_count_before"] = (
            service.execution.boundary.actual_outbound_attempts
        )
        assert (
            record["old_attempt_count_before"] == previous["actual_outbound_attempts"]
        )
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), 90)
        assert (
            service.execution.boundary.actual_outbound_attempts
            == record["old_attempt_count_before"]
        )
        assert TaskStore(repo).get(course, old_task["task_id"]).state == "unknown"
        record["readonly_reconciliation_without_replay"] = True

        async def complete(value):
            task = TaskCoverage(repo).admit(course, value)
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), args.timeout * 5 + 60)
            current = TaskStore(repo).get(course, task.task_id)
            record["steps"].append(current.model_dump(mode="json"))
            if current.state != "completed":
                raise RuntimeError(
                    "CONTINUATION_NOT_COMPLETED:" + current.kind + ":" + current.state
                )
            return current, assets.get(
                course, TaskCoverage(repo).get(course, task.task_id).target.artifact_id
            )

        draft = next(
            a
            for a in assets.list(course)
            if any(b.kind == "essay_draft" for b in a.revision.body.blocks)
        )
        rounds = essays.list(course, draft.artifact_id)
        assert len(rounds.items) == 2 and rounds.items[0].state == "unknown"
        first = essays.get(course, rounds.items[1].round_id)
        assert first.reference_assessment_id and first.user_assessment_id
        before_text = draft.revision.body.blocks[0].markdown
        # A new user-authored condition/version, not the same unknown request.
        replacement = draft.revision.body.blocks[0].model_copy(
            update={
                "markdown": before_text
                + " My friend enjoys short stories about nature."
            }
        )
        revised = assets.update(
            course,
            draft.artifact_id,
            ArtifactUpdate(
                operation_id="explicit_new_draft_after_unknown",
                expected_revision_id=draft.head_revision_id,
                changes=[BlockChange(block_id="draft", replacement=replacement)],
            ),
        ).artifact
        assert revised.head_revision_id != rounds.items[0].draft_revision_id
        assert revised.revision.body.blocks[0].markdown != before_text
        record["new_input_revision"] = revised.model_dump(mode="json")
        assert not reviews.today(course_id=course).items
        _, feedback = await complete(
            TaskAdmission(
                operation_id="review_distinct_third_draft",
                kind="essay_review",
                instruction="这是用户进一步写入新句子后的新版本。只批改这一实际完整稿，给原文精确定位的替换建议和100分参考判分。保留原稿，不冒充考试成绩。之前未知轮次不重跑。",
                target={"mode": "create", "title": "新修订稿独立参考批改"},
                settings=settings,
                essay_review={
                    "artifact_id": revised.artifact_id,
                    "revision_id": revised.head_revision_id,
                    "block_id": "draft",
                    "expected_round_number": 2,
                },
            )
        )
        third = essays.get(
            course, essays.list(course, revised.artifact_id).items[0].round_id
        )
        assert third.result["status"] == "reference_ready"
        assert third.draft.markdown == replacement.markdown
        for item in third.result["annotations"]:
            assert third.draft.markdown[item["start"] : item["end"]] == item["quote"]
        assert not reviews.today(course_id=course).items
        record["no_implicit_cards_after_reading_translation_and_feedback"] = True
        record["third_round"] = third.model_dump(mode="json")
        leaf = TaskCoverage(repo).get(course, previous["steps"][0]["task_id"]).leaves[0]
        snapshot = SourceStore(repo).snapshot(course, leaf.snapshot_ids[0])
        task, deck = await complete(
            TaskAdmission(
                operation_id="explicit_error_cards_after_new_draft",
                kind="card_set",
                sources_only=True,
                coverage_mode="complete",
                instruction="现在明确创建两张复习卡，分别练习 has/have 和 goes/go 的主谓一致，每张一个要点和正确例句。原英文规则、实际修订稿和AI反馈身份分开；AI反馈不能冒充教材。不建额外练习。",
                sources=[
                    {"kind": "selection", "snapshot_id": snapshot.snapshot_id},
                    {
                        "kind": "artifact",
                        "artifact_id": revised.artifact_id,
                        "revision_id": revised.head_revision_id,
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
        assert len([b for b in deck.revision.body.blocks if b.kind == "card"]) == 2
        roles = {
            SourceStore(repo).snapshot(course, sid).content_role
            for leaf in TaskCoverage(repo).get(course, task.task_id).leaves
            for sid in leaf.snapshot_ids
        }
        assert {"original", "mixed", "ai_generated"} <= roles
        record["source_roles"] = sorted(roles)
        queued = [
            i
            for i in reviews.today(course_id=course).items
            if i.artifact_id == deck.artifact_id
        ]
        assert len(queued) == 2
        item = queued[0]
        session = reviews.open_session(
            course,
            item.review_id,
            ReviewSessionInput(
                operation_id="open_new_card", expected_revision=item.schedule_revision
            ),
        )
        reviews.reveal(
            course,
            item.review_id,
            session.session_id,
            ReviewSessionInput(
                operation_id="reveal_new_card", expected_revision=item.schedule_revision
            ),
        )
        rating = reviews.rate(
            course,
            item.review_id,
            ReviewRatingInput(
                operation_id="rate_new_card",
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
                operation_id="pause_new_card",
                expected_revision=queued[1].schedule_revision,
                action="pause",
            ),
        )
        record["rating"], record["paused"] = (
            rating.model_dump(mode="json"),
            paused.model_dump(mode="json"),
        )
        exports = []
        for artifact in assets.list(course):
            target = out / (artifact.artifact_id + ".zip")
            with OfflineExporter(repo).build(
                course, artifact.artifact_id, artifact.head_revision_id
            ) as stream:
                with target.open("wb") as handle:
                    shutil.copyfileobj(stream, handle)
            exports.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "revision_id": artifact.head_revision_id,
                    "kind": artifact.kind,
                    "zip": target.name,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        record["exports"] = exports
        record["artifacts"] = [a.model_dump(mode="json") for a in assets.list(course)]
        assert TaskStore(repo).get(course, old_task["task_id"]).state == "unknown"
        current = [
            row
            for row in service.execution.boundary.records
            if row["generation_id"] == old_run
        ]
        assert (
            len(current) == 2
            and sum(bool(r.get("actual_outbound_attempt")) for r in current) == 2
        )
        record["old_unknown_unchanged_without_replay"] = True
        requests = list(service.execution.boundary.private.glob("*.request.json"))
        assert requests
        for path in requests:
            assert not any(
                token in path.read_text("utf-8")
                for token in ("EXCLUDED_READING_PREFIX", "EXCLUDED_READING_SUFFIX")
            )
        record["wire_canary_absent"] = True
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
        record["actual_outbound_attempts_cumulative"] = (
            service.execution.boundary.actual_outbound_attempts
        )
        record["actual_outbound_attempts_new"] = record[
            "actual_outbound_attempts_cumulative"
        ] - record.get("old_attempt_count_before", 0)
        record["boundary_records"] = service.execution.boundary.records
        with repo.engine.connect() as conn:
            record["chat_usage"] = [
                dict(x) for x in conn.execute(select(business.usage_records)).mappings()
            ]
            record["raw_usage"] = [
                dict(x) for x in conn.execute(select(s.run_usage)).mappings()
            ]
            record["run_states"] = [
                dict(x)
                for x in conn.execute(
                    select(s.task_runs.c.run_id, s.task_runs.c.state)
                ).mappings()
            ]
        repo.close()
    if record["status"] == "passed":
        cold = Repository(config.data_dir)
        try:
            assert len(EssayStore(cold).list(course, draft.artifact_id).items) == 3
            assert TaskStore(cold).get(course, old_task["task_id"]).state == "unknown"
            assert (
                ReviewStore(cold)
                .get(course, queued[0].review_id)
                .scheduler_state["reps"]
                == 1
            )
            assert (
                ReviewStore(cold).get(course, queued[1].review_id).queue_state
                == "paused"
            )
            record["cold_data_reopen"] = "passed; no new Host/provider dispatch"
        except Exception as error:
            record.update(status="failed", error="COLD_REOPEN:" + str(error))
        finally:
            cold.close()
    record["source_after"] = freeze()
    record["source_changed_during_run"] = (
        record["source_before"] != record["source_after"]
    )
    record["original_owned_data_unchanged"] = original_before == hashes(old / "data")
    if (
        record["source_changed_during_run"]
        or not record["original_owned_data_unchanged"]
    ):
        record["status"] = "failed"
    (out / "summary.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in (
                    "status",
                    "error",
                    "actual_outbound_attempts_new",
                    "old_unknown_unchanged_without_replay",
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
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
