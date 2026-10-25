"""Known final-write rejections cannot silently publish an earlier draft."""

from dataclasses import asdict

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table, insert, select

from learning_app.storage.repository import _hash

from . import schema as s
from .common import now

failures = Table(
    "learning_model_stage_failures",
    s.metadata,
    Column("effect_id", String, primary_key=True),
    Column("run_id", ForeignKey(s.task_runs.c.run_id), nullable=False),
    Column("native_call", JSON, nullable=False),
    Column("code", String, nullable=False),
    Column("status", Integer, nullable=False),
    Column("details", JSON),
    Column("created_at", String, nullable=False),
)


def rejected(conn, call):
    row = (
        conn.execute(select(failures).where(failures.c.effect_id == _hash(asdict(call))))
        .mappings()
        .first()
    )
    return dict(row) if row else None


def record(conn, call, error):
    if rejected(conn, call) is None:
        conn.execute(
            insert(failures).values(
                effect_id=_hash(asdict(call)),
                run_id=call.run_id,
                native_call=asdict(call),
                code=error.code,
                status=error.status,
                details=error.details,
                created_at=now(),
            )
        )


def order(call):
    return tuple(
        call.get(key, -1)
        for key in ("native_turn", "native_step", "native_call_seq", "tool_ordinal")
    )


def final_stage(conn, run_id):
    accepted = list(
        conn.execute(select(s.native_effects).where(s.native_effects.c.run_id == run_id)).mappings()
    )
    denied = list(conn.execute(select(failures).where(failures.c.run_id == run_id)).mappings())
    latest = max(accepted, key=lambda row: order(row["native_call"]), default=None)
    failure = max(denied, key=lambda row: order(row["native_call"]), default=None)
    if failure and (latest is None or order(failure["native_call"]) > order(latest["native_call"])):
        from learning_app.errors import AppError

        raise AppError(
            "NATIVE_FINAL_STAGE_REJECTED", "最后一次成果写入未通过校验；旧草稿保留，未发布"
        )
    return latest["stage_id"] if latest else None
