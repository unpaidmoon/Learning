"""Only synthetic local course text; no model, video or user assets."""

from learning_app.course_files import CourseFileStore
from learning_app.storage import Repository
from learning_app.workbench.models import ArtifactBody, ArtifactCreate, MarkdownBlock, ModelSettings
from learning_app.workbench.sources import SourceStore


def learning_course(tmp_path):
    repository = Repository(tmp_path / "data")
    root = tmp_path / "course-materials"
    root.mkdir()
    path = root / "lesson.txt"
    path.write_text("Only this selected sentence. 不应补发前后文。", encoding="utf-8")
    files = CourseFileStore(repository)
    course = files.bind(None, str(root))
    opened = files.open(course["course_id"], "lesson.txt")
    snapshot = SourceStore(repository).capture_text(
        course["course_id"],
        opened["asset_id"],
        opened["source_version"],
        0,
        27,
    )
    return repository, course["course_id"], root, snapshot


def note(operation_id="create_note", text="学习内容", *, snapshot_id=None):
    return ArtifactCreate(
        operation_id=operation_id,
        kind="note",
        title="课程笔记",
        body=ArtifactBody(
            blocks=[
                MarkdownBlock(
                    block_id="paragraph_1",
                    markdown=text,
                    attribution="source_paraphrase" if snapshot_id else "user_authored",
                    source_snapshot_ids=[snapshot_id] if snapshot_id else [],
                )
            ]
        ),
    )


def settings():
    return ModelSettings(
        model="deepseek-flash",
        thinking="high",
        max_output_tokens=8192,
        timeout_seconds=180,
    )
