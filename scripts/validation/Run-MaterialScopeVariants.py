"""Owned material-chain branches: long manual subtitle and source-version gap."""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactUpdate,
    BlockChange,
    MarkdownBlock,
    ModelSettings,
)
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore
from learning_app.workbench.transcript_models import SubtitleChange, TranscriptEdit
from learning_app.workbench.transcripts import TranscriptStore


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    base = PROJECT_ROOT / ".runtime/validation"
    original = base / "material-chain-practice-real-20261025-r1"
    preparation = json.loads(
        (base / "material-chain-prepare-20261024-r1/summary.json").read_text("utf-8")
    )
    before = hashes(original / "data")
    out = base / args.run_id
    out.mkdir(exist_ok=False)
    shutil.copytree(original / "data", out / "data")
    repo = Repository(out / "data")
    owner, origin = preparation["owner_course_id"], preparation["origin_course_id"]
    sources, tasks = SourceStore(repo), TaskCoverage(repo)
    planner = TaskPlanner(repo)
    settings = ModelSettings(
        model="deepseek-flash",
        thinking="high",
        max_output_tokens=16384,
        timeout_seconds=300,
    )
    record = {
        "status": "not_run",
        "actual_outbound_attempts_new": 0,
        "host_calls": 0,
        "layer": "formal source/plan/recovery interfaces on owned saved-data branches",
        "user_learning_experience": "not_run",
    }
    try:
        track = preparation["transcripts"][0]
        subtitles = TranscriptStore(repo)
        prior = subtitles.view(origin, track["track_id"])
        segment = prior.segments[0]
        long_line = (
            "Synthetic manual stress: A🧠B 3×4=12 cm²; keep every codepoint. " * 1000
        )
        assert "\n" not in long_line and 48000 < len(long_line) < 100000
        edit = subtitles.edit(
            origin,
            track["track_id"],
            TranscriptEdit(
                operation_id="explicit_synthetic_long_single_line",
                expected_edit_head_id=prior.edit_head_id,
                changes=[
                    SubtitleChange(
                        segment_id=segment.segment_id,
                        raw_revision_id=segment.raw_revision_id,
                        text=long_line,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                    )
                ],
            ),
        )
        after = subtitles.view(origin, track["track_id"])
        actual = next(x for x in after.segments if x.segment_id == segment.segment_id)
        assert (
            actual.raw_text == segment.raw_text
            and actual.text == long_line
            and actual.origin == "user_note"
        )
        declaration = {
            "kind": "transcript",
            "source_course_id": origin,
            "asset_id": track["asset_id"],
            "source_version": track["source_version"],
            "track_id": track["track_id"],
        }
        task = tasks.admit(
            owner,
            TaskAdmission(
                operation_id="long_line_scope",
                kind="handout",
                coverage_mode="complete",
                sources_only=True,
                instruction="完整整理明确选定的长单行字幕；这是手工压力fixture，不增加视频画面。",
                sources=[declaration],
                target={"mode": "create", "title": "长单行字幕范围fixture"},
                settings=settings,
            ),
        )
        tasks.refresh(owner, task.task_id)
        planner.plan(owner, task.task_id)
        coverage = tasks.get(owner, task.task_id)
        parent = next(
            sources.snapshot(owner, sid)
            for leaf in coverage.leaves
            for sid in leaf.snapshot_ids
            if sources.snapshot(owner, sid).text == long_line
        )
        assert parent.content_role == "user_note" and parent.course_id == origin
        pieces = []
        for unit in TaskStore(repo).get(owner, task.task_id).units:
            _, work_inputs = planner.inputs(owner, task.task_id, unit.unit_id)
            for item, snapshot in work_inputs:
                if item["representation_snapshot_id"] == parent.snapshot_id:
                    assert (
                        snapshot.text
                        == long_line[item["char_start"] : item["char_end"]]
                    )
                    assert item["char_end"] - item["char_start"] <= 24000
                    pieces.append((item["char_start"], item["char_end"], snapshot.text))
        pieces.sort()
        assert len(pieces) >= 3 and "".join(x[2] for x in pieces) == long_line
        assert pieces[0][0] == 0 and pieces[-1][1] == len(long_line)
        assert all(a[1] == b[0] for a, b in zip(pieces, pieces[1:]))
        record["long_line"] = {
            "codepoints": len(long_line),
            "chunks": [[a, b] for a, b, _ in pieces],
            "edit_revision": edit.revision_id,
            "source_role": parent.content_role,
            "source_course_id": parent.course_id,
            "reference": parent.reference.model_dump(mode="json"),
            "original_raw_preserved": True,
        }

        # New owned source root copies the exact original PDF bytes; historical
        # originals are never changed or relocated by this failure fixture.
        fault_root = out / "fault-course"
        fault_root.mkdir()
        pdf_path = next(Path(p) for p in preparation["originals"] if p.endswith(".pdf"))
        changed = fault_root / pdf_path.name
        shutil.copyfile(pdf_path, changed)
        files = CourseFileStore(repo)
        foreign = files.bind(None, str(fault_root), "自造来源变化分支")["course_id"]
        opened = files.open(foreign, changed.name)
        frozen_version = opened["source_version"]
        page = DocumentReader(repo).page(foreign, opened["asset_id"], frozen_version, 0)
        assert page.text
        changed.write_bytes(
            changed.read_bytes() + b"\n% owned replacement version canary\n"
        )
        with_error = None
        try:
            DocumentReader(repo).page(foreign, opened["asset_id"], frozen_version, 0)
        except AppError as error:
            with_error = error.code
        assert with_error == "SOURCE_VERSION_UNAVAILABLE"
        gap = tasks.admit(
            owner,
            TaskAdmission(
                operation_id="changed_source_scope",
                kind="handout",
                coverage_mode="complete",
                sources_only=True,
                instruction="完整整理这份冻结PDF与明确字幕；版本变化时留缺口，不能换新版。",
                sources=[
                    {
                        "kind": "document",
                        "source_course_id": foreign,
                        "asset_id": opened["asset_id"],
                        "source_version": frozen_version,
                    },
                    declaration,
                ],
                target={"mode": "create", "title": "来源变化与明确排除fixture"},
                settings=settings,
            ),
        )
        tasks.refresh(owner, gap.task_id)
        planner.plan(owner, gap.task_id)
        blocked = tasks.get(owner, gap.task_id)
        missing = next(
            x for x in blocked.sources if x.declaration["kind"] == "document"
        )
        assert (
            missing.state == "blocked"
            and missing.error_code == "SOURCE_VERSION_UNAVAILABLE"
        )
        current = TaskStore(repo).get(owner, gap.task_id)
        revised = TaskRecovery(repo).source_gap(
            owner,
            gap.task_id,
            SourceGapDecision(
                operation_id="explicit_exclude_changed_source",
                expected_revision=current.revision,
                decision="exclude",
                source_ids=[missing.source_id],
            ),
        )
        tasks.refresh(owner, gap.task_id)
        planner.plan(owner, gap.task_id)
        assert (
            revised.scope_revision == current.scope_revision + 1
            and revised.coverage_mode == "complete"
        )
        final = tasks.get(owner, gap.task_id)
        assert (
            next(
                x for x in final.sources if x.source_id == missing.source_id
            ).excluded_at_scope_revision
            is not None
        )
        assert not final.target.materialized
        record["changed_source"] = {
            "error_code": with_error,
            "original_source_version": frozen_version,
            "before": blocked.model_dump(mode="json"),
            "after": final.model_dump(mode="json"),
            "explicit_scope_revision": revised.scope_revision,
            "no_new_version_fallback": True,
        }
        # A separate deterministic event branch tests draft merge/partial
        # acceptance. These events are labeled fixture, never actual provider.
        sys.path.insert(0, str(PROJECT_ROOT / "backend/tests"))
        from test_task_coverage import finish_work

        partial_task = tasks.admit(
            owner,
            TaskAdmission(
                operation_id="known_gap_partial_event_branch",
                kind="handout",
                coverage_mode="complete",
                sources_only=True,
                instruction="合成本地事件测试：保留可用字幕草稿，来源缺口须显式接受部分。",
                sources=[
                    {
                        "kind": "document",
                        "source_course_id": foreign,
                        "asset_id": opened["asset_id"],
                        "source_version": frozen_version,
                    },
                    declaration,
                ],
                target={"mode": "create", "title": "本地事件部分草稿fixture"},
                settings=settings,
            ),
        )
        tasks.refresh(owner, partial_task.task_id)
        planner.plan(owner, partial_task.task_id)
        queued = TaskStore(repo).get(owner, partial_task.task_id)
        for index, unit in enumerate(queued.units):
            _, receipt = finish_work(
                repo,
                owner,
                queued,
                unit,
                text=f"合成本地事件草稿{index + 1}，不是实际模型生成内容或学习效果证据。",
            )
            if index == 0:
                ArtifactStore(repo).update(
                    owner,
                    receipt.artifact.artifact_id,
                    ArtifactUpdate(
                        operation_id="synthetic_concurrent_unrelated_note",
                        expected_revision_id=receipt.artifact.head_revision_id,
                        changes=[
                            BlockChange(
                                block_id="manual_marker",
                                replacement=MarkdownBlock(
                                    block_id="manual_marker",
                                    markdown="合成用户笔记：保留此无关修改。",
                                    attribution="user_authored",
                                ),
                            )
                        ],
                    ),
                )
        current = TaskStore(repo).reconcile(owner, partial_task.task_id)
        assert current.state == "needs_sources"
        accepted = TaskRecovery(repo).source_gap(
            owner,
            partial_task.task_id,
            SourceGapDecision(
                operation_id="synthetic_explicit_accept_partial",
                expected_revision=current.revision,
                decision="accept_partial",
            ),
        )
        assert accepted.state == "partial" and accepted.coverage_mode == "complete"
        partial_coverage = tasks.get(owner, partial_task.task_id)
        saved = ArtifactStore(repo).get(owner, partial_coverage.target.artifact_id)
        assert saved.content_role == "mixed" and any(
            b.block_id == "manual_marker" for b in saved.revision.body.blocks
        )
        record["partial_event_fixture"] = {
            "task": accepted.model_dump(mode="json"),
            "artifact_id": saved.artifact_id,
            "revision_id": saved.head_revision_id,
            "fixture_units": len(queued.units),
            "new_actual_provider_calls": 0,
            "unrelated_user_note_preserved": True,
        }
        record["status"] = "passed"
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        repo.close()
    record["original_owned_data_unchanged"] = before == hashes(original / "data")
    if not record["original_owned_data_unchanged"]:
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
                    "long_line",
                    "actual_outbound_attempts_new",
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
    raise SystemExit(run(parser.parse_args()))
