"""Foreground local-processing priority, ordered across document/audio tabs."""

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError

from . import schema as s


class PreparationFocus:
    @staticmethod
    def row(conn, client):
        return (
            conn.execute(
                select(s.preparation_focus).where(s.preparation_focus.c.reader_client_id == client)
            )
            .mappings()
            .first()
        )

    @classmethod
    def latest(cls, conn, client):
        row = cls.row(conn, client)
        legacy = conn.execute(
            select(func.max(s.reading_open_fences.c.view_sequence)).where(
                s.reading_open_fences.c.reader_client_id == client
            )
        ).scalar_one_or_none()
        return max(row["view_sequence"] if row else -1, legacy if legacy is not None else -1)

    @staticmethod
    def clear(conn):
        conn.execute(
            update(s.preparation_units)
            .where(
                s.preparation_units.c.priority == 0,
                s.preparation_units.c.job_id.in_(
                    select(s.preparations.c.job_id).where(
                        s.preparations.c.parameters["native_read_run_id"].as_string().is_(None)
                    )
                ),
            )
            .values(priority=10)
        )

    @classmethod
    def claim(cls, conn, course_id, asset_id, version, client, epoch, sequence, request_id):
        row = cls.row(conn, client)
        owner = dict(
            course_id=course_id, asset_id=asset_id, source_version=version, reader_epoch=epoch
        )
        if cls.latest(conn, client) > sequence or (
            row
            and row["view_sequence"] == sequence
            and (
                not row["active"]
                or row["request_id"] != request_id
                or any(row[k] != v for k, v in owner.items())
            )
        ):
            raise AppError("READING_VIEW_STALE", "更新的资料视图已取得本地处理优先级", 409)
        payload = {**owner, "view_sequence": sequence, "request_id": request_id, "active": True}
        if row:
            conn.execute(
                update(s.preparation_focus)
                .where(s.preparation_focus.c.reader_client_id == client)
                .values(**payload)
            )
        else:
            conn.execute(insert(s.preparation_focus).values(reader_client_id=client, **payload))
        cls.clear(conn)

    @classmethod
    def require_owner(cls, conn, course_id, asset_id, version, client, epoch):
        row = cls.row(conn, client)
        owner = dict(
            course_id=course_id, asset_id=asset_id, source_version=version, reader_epoch=epoch
        )
        if not row or not row["active"] or any(row[k] != v for k, v in owner.items()):
            raise AppError("READING_VIEW_STALE", "当前资料激活身份已失效", 409)
        return row

    @classmethod
    def assert_current(cls, conn, course_id, asset_id, version, value):
        row = cls.row(conn, value.reader_client_id)
        expected = dict(
            course_id=course_id,
            asset_id=asset_id,
            source_version=version,
            reader_epoch=value.reader_epoch,
            view_sequence=value.focus_sequence,
            request_id=value.request_id,
        )
        if not row or not row["active"] or any(row[k] != v for k, v in expected.items()):
            raise AppError("READING_VIEW_STALE", "迟到结果未接管当前资料优先级", 409)

    @classmethod
    def release(cls, conn, course_id, asset_id, version, value):
        row = cls.row(conn, value.reader_client_id)
        owner = dict(
            course_id=course_id,
            asset_id=asset_id,
            source_version=version,
            reader_epoch=value.reader_epoch,
        )
        if (
            not row
            or not row["active"]
            or any(row[k] != v for k, v in owner.items())
            or cls.latest(conn, value.reader_client_id) > value.focus_sequence
            or (
                row["view_sequence"] == value.focus_sequence
                and row["request_id"] != value.request_id
            )
        ):
            return False
        conn.execute(
            update(s.preparation_focus)
            .where(s.preparation_focus.c.reader_client_id == value.reader_client_id)
            .values(view_sequence=value.focus_sequence, request_id=value.request_id, active=False)
        )
        cls.clear(conn)
        return True
