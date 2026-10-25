"""Conversation read workspaces, independent of browsing and the save owner."""

from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import schema as s

UNCLASSIFIED = "course_unclassified"
chat_workspaces = Table(
    "chat_workspaces",
    MetaData(),
    Column("chat_id", ForeignKey(s.chats.c.chat_id), primary_key=True),
    Column("course_ids", JSON, nullable=False),
    Column("revision", String, nullable=False),
)


def ensure_unclassified(repository):
    from learning_app.course_files import CourseFileStore

    with repository._lock:
        with repository.engine.connect() as conn:
            exists = conn.execute(
                select(s.courses).where(s.courses.c.course_id == UNCLASSIFIED)
            ).first()
        if not exists:
            repository.create_course("未分类", UNCLASSIFIED)
        files = CourseFileStore(repository)
        if files.folder(UNCLASSIFIED)["root_path"] is None:
            path = repository.root / "materials" / "unclassified"
            path.mkdir(parents=True, exist_ok=True)
            files.bind(UNCLASSIFIED, str(path), "未分类")
    return UNCLASSIFIED


def read_workspaces(conn, chat):
    row = conn.execute(
        select(chat_workspaces.c.course_ids).where(chat_workspaces.c.chat_id == chat["chat_id"])
    ).first()
    return (
        list(row[0]) if row else ([] if chat["course_id"] == UNCLASSIFIED else [chat["course_id"]])
    )


def set_workspaces(repository, chat_id, course_ids):
    import uuid

    if (
        not isinstance(course_ids, list)
        or len(course_ids) > 32
        or any(not isinstance(value, str) for value in course_ids)
        or len(set(course_ids)) != len(course_ids)
    ):
        raise AppError("WORKSPACE_SCOPE_INVALID", "工作区须为不重复的课程列表", 422)
    with repository._lock, repository.engine.begin() as conn:
        if not conn.execute(select(s.chats.c.chat_id).where(s.chats.c.chat_id == chat_id)).first():
            raise AppError("CHAT_NOT_FOUND", "对话不存在", 404)
        from sqlalchemy import or_

        from learning_app.course_files_schema import course_roots, course_visibility

        visible = set(
            conn.execute(
                select(s.courses.c.course_id)
                .outerjoin(course_roots, course_roots.c.course_id == s.courses.c.course_id)
                .outerjoin(
                    course_visibility, course_visibility.c.course_id == s.courses.c.course_id
                )
                .where(
                    or_(course_roots.c.state.is_(None), course_roots.c.state != "detached"),
                    or_(
                        course_visibility.c.hidden.is_(None), course_visibility.c.hidden.is_(False)
                    ),
                )
            ).scalars()
        )
        if not set(course_ids).issubset(visible):
            raise AppError("WORKSPACE_UNAVAILABLE", "指定工作区不可用", 403)
        values = {"course_ids": course_ids, "revision": uuid.uuid4().hex}
        exists = conn.execute(
            select(chat_workspaces.c.chat_id).where(chat_workspaces.c.chat_id == chat_id)
        ).first()
        if exists:
            conn.execute(
                update(chat_workspaces).where(chat_workspaces.c.chat_id == chat_id).values(**values)
            )
        else:
            conn.execute(insert(chat_workspaces).values(chat_id=chat_id, **values))
    return course_ids


def explicit_subtitles(text):
    import re

    value = text.strip()
    if re.match(r"^[/\$]subtitles\b", value):
        return True
    value = re.sub(
        r"^(?:(?:请|请你|帮我|请帮我|麻烦|现在|我想要|我想|我需要|我要|能否|可以)\s*)+", "", value
    )
    if re.search(r"(?:不要|禁止|无需|不需要|别).{0,12}(?:字幕|转写|转录)", value):
        return False
    return bool(
        re.match(
            r"^(?:(?:生成|获取|读取|转写|制作|提取|查看|显示|打开|准备|转录).{0,40}(?:字幕|转写)"
            r"|(?:把|将|给|为).{0,60}(?:(?:生成|获取|制作|提取).{0,16}字幕|转成(?:文字|字幕)))",
            value,
        )
    )
