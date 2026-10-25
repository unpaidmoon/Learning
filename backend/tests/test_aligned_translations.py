"""Fixed original scanner adaptation; invalid suffixes never imply completion."""

import io
import json

import pytest
from PIL import Image
from sqlalchemy import select
from test_requested_learning import chat_scope
from workbench_support import learning_course

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.workbench import schema as s
from learning_app.workbench.aligned_translation import body, source_ranges, work_budget
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.content_processing import ContentProcessor
from learning_app.workbench.models import ArtifactCreate, ModelSettings, TranslationBlock
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def stream(*values):
    return "\n".join(json.dumps(value, ensure_ascii=False) for value in values)


def test_original_object_scanner_retains_exact_escaped_and_unicode_segments():
    parts = [
        {"src": '  A {brace} and "quote" 🙂\n', "tgt": "花括号、引号与表情。\n"},
        {"src": "Next paragraph.  ", "tgt": "下一段。  "},
    ]
    result = ContentProcessor().aligned_translation(stream(*parts, {"done": True}))
    assert result["status"] == "complete" and result["segments"] == parts
    assert result["audit"] == {
        "invalidObjects": 0,
        "outsideText": False,
        "afterDone": False,
        "doneCount": 1,
        "objects": 3,
        "incomplete": False,
    }


@pytest.mark.parametrize(
    "raw",
    [
        stream({"src": "First.", "tgt": "首段。"}) + '\n{"src":"broken',
        stream({"src": "First.", "tgt": "首段。"}) + '\n{"src":}\n{"done":true}',
        stream({"src": "First.", "tgt": "首段。"}),
        stream(
            {"src": "First.", "tgt": "首段。"}, {"done": True}, {"src": "Late.", "tgt": "迟到。"}
        ),
        stream({"src": "First.", "tgt": "首段。"}, {"done": True}, {"done": True}),
        stream({"src": "First.", "tgt": "首段。", "path": "not-allowed"}, {"done": True}),
        stream({"src": "First.", "tgt": ""}, {"done": True}),
        stream({"src": "", "tgt": "invented"}, {"done": True}),
        "prose\n" + stream({"src": "First.", "tgt": "首段。"}, {"done": True}),
        "```json\n" + stream({"src": "First.", "tgt": "首段。"}, {"done": True}) + "\n```",
        json.dumps([{"src": "First.", "tgt": "首段。"}, {"done": True}]),
        stream({"done": True, "src": "First.", "tgt": "首段。"}),
    ],
    ids=[
        "incomplete-tail",
        "malformed-final",
        "missing-done",
        "after-done",
        "duplicate-done",
        "extra-field",
        "empty-target",
        "empty-source",
        "outside-prose",
        "fence",
        "array",
        "mixed-done",
    ],
)
def test_valid_prefix_does_not_hide_invalid_or_incomplete_final_output(raw):
    result = ContentProcessor().aligned_translation(raw)
    assert result["status"] == "invalid"


def test_nontranslatable_whitespace_can_only_remain_exact():
    good = ContentProcessor().aligned_translation(
        stream({"src": "\n\n", "tgt": "\n\n"}, {"done": True})
    )
    bad = ContentProcessor().aligned_translation(
        stream({"src": "\n\n", "tgt": " "}, {"done": True})
    )
    assert good["status"] == "complete" and bad["status"] == "invalid"


def test_paragraph_budget_keeps_whitespace_and_avoids_breaking_english_words():
    text = "First 🙂 paragraph has several English words.\n\nSecond paragraph keeps its words and spacing.\r\nThird paragraph."
    ranges = source_ranges(text, 45)
    assert "".join(text[start:end] for start, end in ranges) == text
    assert all(0 < end - start <= 45 for start, end in ranges)
    assert all(text[end - 1].isspace() for _, end in ranges[:-1])


def selected(repo, course, root, text):
    (root / "english.txt").write_bytes(text.encode("utf-8"))
    opened = CourseFileStore(repo).open(course, "english.txt")
    return SourceStore(repo).capture_text(
        course, opened["asset_id"], opened["source_version"], 0, len(text)
    )


def test_segment_across_frozen_inputs_maps_exact_codepoint_ranges_and_is_verified_on_save(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    original = selected(repo, course, root, "First 🙂.\n\nSecond.")
    a, b = (
        SourceStore(repo).slice(original, 0, 8),
        SourceStore(repo).slice(original, 8, len(original.text)),
    )
    value = body(
        stream(
            {"src": original.text[:11], "tgt": "第一部分。"},
            {"src": original.text[11:], "tgt": "后半部分。"},
            {"done": True},
        ),
        [a, b],
    )
    first = value.blocks[0]
    assert isinstance(first, TranslationBlock)
    assert [
        (part.snapshot_id, part.start, part.end) for part in first.translation.source_parts
    ] == [(a.snapshot_id, 0, 8), (b.snapshot_id, 0, 3)]
    saved = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="aligned_save", kind="translation", title="两段对照", body=value
            ),
        )
        .artifact
    )
    assert saved.revision.body.blocks[0].translation.source_text == original.text[:11]
    altered = first.model_copy(
        update={"translation": first.translation.model_copy(update={"source_text": "X" * 11})}
    )
    with pytest.raises(AppError, match="对照原文"):
        ArtifactStore(repo).create(
            course,
            ArtifactCreate(
                operation_id="forged",
                kind="translation",
                title="不能改原文",
                body={"blocks": [altered]},
            ),
        )
    with pytest.raises(AppError, match="没有逐字覆盖"):
        body(stream({"src": original.text[:-1], "tgt": "缺字"}, {"done": True}), [a, b])
    repo.close()


def test_aligned_task_packs_to_actual_output_budget_and_keeps_normal_tasks_unchanged(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    text = ("A short English sentence.\n" * 180) + "End 🙂."
    source = selected(repo, course, root, text)
    settings = ModelSettings(
        model="deepseek-flash", thinking="high", max_output_tokens=4096, timeout_seconds=120
    )

    def make(kind, operation):
        task = TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id=operation,
                kind=kind,
                instruction="翻译指定文字" if kind == "aligned_translation" else "整理指定文字",
                coverage_mode="complete",
                sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
                target={"mode": "create", "title": operation},
                sources_only=True,
                settings=settings,
            ),
        )
        TaskCoverage(repo).refresh(course, task.task_id)
        TaskPlanner(repo).plan(course, task.task_id)
        return TaskStore(repo).get(course, task.task_id)

    aligned = make("aligned_translation", "aligned")
    ordinary = make("handout", "ordinary")
    assert len(aligned.units) == 3 and len(ordinary.units) == 1
    actual = []
    for unit in aligned.units:
        _, inputs = TaskPlanner(repo).inputs(course, aligned.task_id, unit.unit_id)
        assert sum(len(item.text) for _, item in inputs) <= work_budget(settings.model_dump())
        actual.extend(item.text for _, item in inputs)
    assert "".join(actual) == text
    repo.close()


def test_submitted_image_cannot_be_reinterpreted_as_a_text_translation_input(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    data = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(data, format="PNG")
    image = repo.stage_attachment(data.getvalue())
    chat_scope(repo, course, source, "请根据图片生成学习笔记", attachments=[image["attachment_id"]])
    with repo.engine.connect() as conn:
        rows = list(conn.execute(select(s.snapshots)).mappings())
    captured = next(row for row in rows if row["reference"]["kind"] == "submitted_image")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="reject_image",
            kind="aligned_translation",
            instruction="对照翻译",
            sources_only=True,
            settings=ModelSettings(
                model="deepseek-flash", thinking="high", max_output_tokens=4096, timeout_seconds=120
            ),
            sources=[{"kind": "selection", "snapshot_id": captured["snapshot_id"]}],
            target={"mode": "create", "title": "文字范围"},
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    view = TaskCoverage(repo).get(course, task.task_id)
    assert any(leaf.error_code == "SOURCE_TRANSLATION_TEXT_REQUIRED" for leaf in view.leaves)
    assert not TaskPlanner(repo).plan(course, task.task_id)
    assert TaskStore(repo).get(course, task.task_id).units == []
    repo.close()
