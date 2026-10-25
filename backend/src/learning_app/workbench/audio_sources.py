"""Audio-only preparation authority; document tools keep their document allowlist."""

from pathlib import Path

from sqlalchemy import select

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import schema as business

AUDIO_CONTAINERS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
}


class AudioSources:
    def __init__(self, repository):
        self.repository = repository

    def associated(self, course_id: str, asset_id: str, version: str) -> dict:
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(business.source_versions)
                    .join(
                        business.course_assets,
                        business.source_versions.c.asset_id == business.course_assets.c.asset_id,
                    )
                    .where(
                        business.course_assets.c.course_id == course_id,
                        business.source_versions.c.asset_id == asset_id,
                        business.source_versions.c.source_version == version,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("SOURCE_COURSE_DENIED", "音频来源不属于指定课程和版本", 403)
        if Path(row["local_path"]).suffix.lower() not in AUDIO_CONTAINERS:
            raise AppError("AUDIO_KIND_DENIED", "该来源不属于本地音频处理范围", 403)
        return dict(row)

    def current(self, course_id: str, asset_id: str, version: str) -> dict:
        self.associated(course_id, asset_id, version)
        source = self.repository.resolve_source(asset_id, version)
        if source["status"] != "available":
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "音频原版本缺失或改变，已有字幕仍保留")
        files = CourseFileStore(self.repository)
        root = files._root(course_id)
        path = Path(source["local_path"])
        if not path.is_relative_to(root):
            raise AppError("SOURCE_COURSE_DENIED", "音频来源不在指定课程目录", 403)
        checked = files._path(root, path.relative_to(root).as_posix())
        return {**source, "path": checked}
