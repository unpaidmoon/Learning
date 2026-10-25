"""Actual fixed Host: outline once, incremental book pages and stable real answers."""

import asyncio
import json
import threading

import pytest
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.book_models import (
    BookChapterLabel,
    BookDirectoryUpdate,
    BookOutlineConfirm,
)
from learning_app.workbench.books import BookStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactRestore,
    ArtifactUpdate,
    BlockChange,
    MarkdownBlock,
)
from learning_app.workbench.practice import PracticeStore
from learning_app.workbench.practice_models import AnswerInput
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
@pytest.mark.parametrize("supplied", [False, True])
async def test_book_outline_then_read_first_chapter_while_next_generates_and_keep_question_pin(
    tmp_path, supplied
):
    repo, course, root, source = learning_course(tmp_path)
    (root / "second.txt").write_text("第二份合成资料：保留真实来源与不同修订。", "utf-8")
    second = CourseFileStore(repo).open(course, "second.txt")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="explicit_book",
            kind="book",
            instruction="把这两份资料组织成两章交互书，每章含真实来源和练习。",
            coverage_mode="complete",
            sources=[
                {
                    "kind": "document",
                    "asset_id": source.reference.asset_id,
                    "source_version": source.reference.source_version,
                },
                {
                    "kind": "document",
                    "asset_id": second["asset_id"],
                    "source_version": second["source_version"],
                },
            ],
            target={"mode": "create", "title": "逐章保存的合成书"},
            settings=settings(),
            sources_only=True,
            outline=[
                {"chapter_id": "part_1", "title": "第一章", "objective": "核对实际内容"},
                {"chapter_id": "part_2", "title": "第二章", "objective": "保留不同来源"},
            ]
            if supplied
            else [],
            outline_supplied=supplied,
        ),
    )
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    original_fixture = boundary._fixture
    later_entered, continue_later = threading.Event(), threading.Event()

    def book_fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        if run["input_contract"].get("phase") == "outline":
            context = run["input_contract"]["book_context"]
            chapters = context["supplied_outline"] or [
                {"chapter_id": "part_1", "title": "第一章", "objective": "核对实际内容"},
                {"chapter_id": "part_2", "title": "第二章", "objective": "保留不同来源"},
            ]
            value = {
                "chapters": [
                    {**chapter, "source_ids": [context["sources"][index]["source_id"]]}
                    for index, chapter in enumerate(chapters)
                ],
                "rationale": "按来源组织，仅据完整资料清单拟目录。",
            }
            assert not request.get("tools") and source.text not in json.dumps(
                request["messages"], ensure_ascii=False
            )
        else:
            page = run["input_contract"]["book"]
            if page["chapter_id"] == "part_2":
                later_entered.set()
                assert continue_later.wait(15)
            ids = [item["snapshot_id"] for item in run["input_contract"]["inputs"]]
            value = {
                "blocks": [
                    {
                        "block_id": "text",
                        "kind": "markdown",
                        "markdown": "本页保留实际材料的原意。",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": ids,
                    },
                    {
                        "block_id": "question",
                        "kind": "question",
                        "markdown": "请按材料核对本页来源。",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": ids,
                        "question": {
                            "question_type": "short_answer",
                            "options": [],
                            "reference_answer": "核对指定原件和版本。",
                            "explanation": "不能把其他修订当成原来源。",
                            "answer_authority": "ai_reference",
                        },
                    },
                ]
            }
        return original_fixture(
            handler,
            request,
            {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
            row,
            observer,
        )

    boundary._fixture = book_fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            if not supplied:
                await asyncio.wait_for(executor._queue.join(), timeout=25)
                current = TaskStore(repo).get(course, task.task_id)
                outline = BookStore(repo).outline(course, task.task_id)
                assert current.state == "awaiting_outline" and not outline.confirmed
                assert ArtifactStore(repo).list(course) == []
                assert (
                    sum(
                        s.processed_leaf_count
                        for s in TaskCoverage(repo).get(course, task.task_id).sources
                    )
                    == 0
                )
                confirmation = BookOutlineConfirm(
                    operation_id="confirm_once",
                    expected_revision=current.revision,
                    proposal_hash=outline.proposal_hash,
                    outline=outline.proposal,
                )
                confirmed = BookStore(repo).confirm(course, task.task_id, confirmation)
                assert BookStore(repo).confirm(course, task.task_id, confirmation) == confirmed
                executor.schedule(course, task.task_id)
            async with asyncio.timeout(25):
                while not later_entered.is_set():
                    await asyncio.sleep(0.01)
            first = ArtifactStore(repo).list(course)[0]
            first_view = BookStore(repo).get(course, first.artifact_id)
            assert (
                len(first_view.chapters[0].pages) == 1 and first_view.chapters[0].state == "ready"
            )
            assert first_view.chapters[1].pages == []
            page = first_view.chapters[0].pages[0].layout
            question_id, pin = next(iter(page.question_revisions.items()))
            assert pin == first.head_revision_id
            actual = PracticeStore(repo).submit(
                course,
                first.artifact_id,
                AnswerInput(
                    operation_id="actual_user_answer",
                    question_revision_id=pin,
                    block_id=question_id,
                    text="我核对了本页原件。",
                ),
            )
            text = next(
                b
                for b in first.revision.body.blocks
                if isinstance(b, MarkdownBlock) and b.kind == "markdown"
            )
            edited = ArtifactStore(repo).update(
                course,
                first.artifact_id,
                ArtifactUpdate(
                    operation_id="manual_during_next_page",
                    expected_revision_id=first.head_revision_id,
                    changes=[
                        BlockChange(
                            block_id=text.block_id,
                            replacement=text.model_copy(
                                update={
                                    "markdown": "用户在下一页生成时改了本页。",
                                    "attribution": "user_authored",
                                }
                            ),
                        )
                    ],
                ),
            )
            assert edited.outcome == "applied"
            continue_later.set()
            await asyncio.wait_for(executor._queue.join(), timeout=25)
            final = TaskStore(repo).get(course, task.task_id)
            assert final.state == "completed"
            assert BookStore(repo).outline(course, task.task_id).confirmation_count == 1
            artifact = ArtifactStore(repo).get(course, first.artifact_id)
            view = BookStore(repo).get(course, first.artifact_id)
            assert (
                len(ArtifactStore(repo).list(course)) == 1
                and len(artifact.revision.body.blocks) == 4
            )
            assert artifact.content_role == "mixed" and any(
                b.markdown == "用户在下一页生成时改了本页。" for b in artifact.revision.body.blocks
            )
            assert all(ch.state == "ready" and len(ch.pages) == 1 for ch in view.chapters)
            assert view.chapters[0].pages[0].layout.question_revisions[question_id] == pin
            assert PracticeStore(repo).get(course, actual.answer_id).question_revision_id == pin
            assert len(boundary.records) == 3 and boundary.actual_outbound_attempts == 0
            assert (
                len(
                    BookStore(repo)
                    .get(course, first.artifact_id, first.head_revision_id)
                    .chapters[1]
                    .pages
                )
                == 0
            )
            for unit in final.units:
                executor._finish(course, task.task_id, unit.run_id)
            assert len(boundary.records) == 3 and len(ArtifactStore(repo).list(course)) == 1
            directory = BookDirectoryUpdate(
                operation_id="reorder_directory",
                expected_revision_id=artifact.head_revision_id,
                chapters=[
                    BookChapterLabel(
                        chapter_id="part_2", title="用户调整第二章", objective="保留不同来源"
                    ),
                    BookChapterLabel(chapter_id="part_1", title="第一章", objective="核对实际内容"),
                ],
            )
            renamed = BookStore(repo).update_directory(course, artifact.artifact_id, directory)
            assert renamed.outcome == "applied"
            changed = BookStore(repo).get(course, artifact.artifact_id)
            assert changed.chapters[0].chapter.title == "用户调整第二章"
            assert (
                BookStore(repo)
                .get(course, artifact.artifact_id, artifact.head_revision_id)
                .chapters[0]
                .chapter.title
                == "第一章"
            )
            assert (
                BookStore(repo).update_directory(course, artifact.artifact_id, directory) == renamed
            )
            conflict = BookStore(repo).update_directory(
                course,
                artifact.artifact_id,
                directory.model_copy(update={"operation_id": "parallel_directory"}),
            )
            assert conflict.outcome == "conflict"
            assert (
                ArtifactStore(repo).get(course, artifact.artifact_id).head_revision_id
                == renamed.artifact.head_revision_id
            )
            old = ArtifactStore(repo).restore(
                course,
                artifact.artifact_id,
                ArtifactRestore(
                    operation_id="restore_old_book",
                    expected_revision_id=renamed.artifact.head_revision_id,
                    restore_revision_id=first.head_revision_id,
                ),
            )
            restored = BookStore(repo).get(course, artifact.artifact_id)
            assert (
                len(restored.chapters[1].pages) == 0
                and restored.chapters[0].pages[0].layout.question_revisions[question_id] == pin
            )
            full = ArtifactStore(repo).restore(
                course,
                artifact.artifact_id,
                ArtifactRestore(
                    operation_id="restore_full_book",
                    expected_revision_id=old.artifact.head_revision_id,
                    restore_revision_id=artifact.head_revision_id,
                ),
            )
            original_question = next(
                b for b in full.artifact.revision.body.blocks if b.block_id == question_id
            )
            new_question = ArtifactStore(repo).update(
                course,
                artifact.artifact_id,
                ArtifactUpdate(
                    operation_id="change_question_stem",
                    expected_revision_id=full.artifact.head_revision_id,
                    changes=[
                        BlockChange(
                            block_id=question_id,
                            replacement=original_question.model_copy(
                                update={"markdown": "用户更改了本题条件，旧答案仍归原题。"}
                            ),
                        )
                    ],
                ),
            )
            updated = BookStore(repo).get(course, artifact.artifact_id)
            assert (
                updated.chapters[0].pages[0].layout.question_revisions[question_id]
                == new_question.artifact.head_revision_id
            )
            assert PracticeStore(repo).get(course, actual.answer_id).question_revision_id == pin
            assert len(boundary.records) == 3
        finally:
            continue_later.set()
            await executor.close()
            await service.close()
            repo.close()
