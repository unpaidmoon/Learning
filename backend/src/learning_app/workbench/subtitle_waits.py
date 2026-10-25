"""Finite local audio work may run only while its original QA tool is waiting."""

from sqlalchemy import Column, ForeignKey, String, Table, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .common import now

waits = Table(
    "learning_subtitle_read_waits",
    s.metadata,
    Column("wait_id", String, primary_key=True),
    Column("run_id", ForeignKey(s.native_scopes.c.run_id), nullable=False),
    Column("job_id", ForeignKey(s.preparations.c.job_id), nullable=False),
    Column("anchor_id", String, nullable=False),
    Column("deadline", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)


class SubtitleWaits:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def active_ids():
        return (
            select(waits.c.job_id)
            .join(s.preparations, s.preparations.c.job_id == waits.c.job_id)
            .join(s.native_scopes, s.native_scopes.c.run_id == waits.c.run_id)
            .join(business.generations, business.generations.c.generation_id == waits.c.run_id)
            .where(
                waits.c.state == "waiting",
                waits.c.deadline > now(),
                s.native_scopes.c.state == "active",
                s.native_scopes.c.owner_kind == "chat",
                s.native_scopes.c.purpose == "qa",
                s.preparations.c.kind == "asr",
                s.preparations.c.scope == "unit",
                business.generations.c.state.in_(["dispatching", "accepted", "running"]),
                business.generations.c.cancel_requested.is_(False),
            )
            .correlate(None)
        )

    def register(self, run_id, anchor_id, job_id, deadline):
        identity = "subtitle_wait_" + _hash({"run": run_id, "anchor": anchor_id, "job": job_id})
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = (
                conn.execute(select(s.native_scopes).where(s.native_scopes.c.run_id == run_id))
                .mappings()
                .one()
            )
            job = (
                conn.execute(select(s.preparations).where(s.preparations.c.job_id == job_id))
                .mappings()
                .one()
            )
            anchor = next(
                (
                    item
                    for item in scope["grants"].get("subtitle_anchors", [])
                    if item["anchor_id"] == anchor_id
                ),
                None,
            )
            if (
                anchor is None
                or scope["owner_kind"] != "chat"
                or scope["purpose"] != "qa"
                or job["kind"] != "asr"
                or job["scope"] != "unit"
                or job["course_id"] != scope["course_id"]
                or job["parameters"].get("native_read_run_id") != run_id
                or job["parameters"].get("native_anchor_id") != anchor_id
                or any(
                    job[key] != anchor[key]
                    for key in ("asset_id", "source_version", "source_digest")
                )
                or type(job["parameters"].get("requested_unit_index")) is not int
                or job["parameters"]["requested_unit_index"]
                not in range(max(0, anchor["time_ms"] // 30000 - 1), anchor["time_ms"] // 30000 + 2)
            ):
                raise AppError(
                    "NATIVE_LOCAL_READ_SCOPE_DENIED", "局部转写不属于本次问题的有限音轨范围", 403
                )
            if (
                conn.execute(select(waits.c.wait_id).where(waits.c.wait_id == identity)).first()
                is None
            ):
                conn.execute(
                    insert(waits).values(
                        wait_id=identity,
                        run_id=run_id,
                        anchor_id=anchor_id,
                        job_id=job_id,
                        deadline=deadline,
                        state="waiting",
                        created_at=now(),
                        updated_at=now(),
                    )
                )
        return identity

    def has_active(self):
        with self.repository.engine.connect() as conn:
            return (
                conn.execute(
                    select(s.preparations.c.job_id)
                    .join(s.preparation_units)
                    .where(
                        s.preparations.c.job_id.in_(self.active_ids()),
                        s.preparations.c.state.in_(["queued", "running"]),
                        s.preparation_units.c.state.in_(["pending", "running"]),
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def job_is_active(self, job_id):
        with self.repository.engine.connect() as conn:
            return (
                conn.execute(
                    select(waits.c.job_id)
                    .where(waits.c.job_id == job_id, waits.c.job_id.in_(self.active_ids()))
                    .limit(1)
                ).first()
                is not None
            )

    def close(self, run_id, anchor_id):
        from .preparations import PreparationStore

        with self.repository._lock, self.repository.engine.begin() as conn:
            jobs = list(
                conn.execute(
                    select(waits.c.job_id).where(
                        waits.c.run_id == run_id,
                        waits.c.anchor_id == anchor_id,
                        waits.c.state == "waiting",
                    )
                ).scalars()
            )
            conn.execute(
                update(waits)
                .where(waits.c.run_id == run_id, waits.c.anchor_id == anchor_id)
                .values(state="closed", updated_at=now())
            )
            # These are separate question-owned unit jobs. A user's paused full
            # transcription is never selected or resumed by this operation.
            rows = (
                conn.execute(
                    select(s.preparations).where(
                        s.preparations.c.job_id.in_(jobs),
                        s.preparations.c.state.in_(["queued", "running"]),
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                PreparationStore._event(
                    conn,
                    row,
                    "native_read_wait_closed",
                    {"run_id": run_id, "anchor_id": anchor_id},
                    state="cancelled",
                    epoch=row["epoch"] + 1,
                )
                conn.execute(
                    update(s.preparation_units)
                    .where(
                        s.preparation_units.c.job_id == row["job_id"],
                        s.preparation_units.c.state.in_(["pending", "running"]),
                    )
                    .values(state="pending", lease_id=None, lease_epoch=None, progress=None)
                )

    @classmethod
    def schedulable(cls):
        return s.preparations.c.parameters["native_read_run_id"].as_string().is_(
            None
        ) | s.preparations.c.job_id.in_(cls.active_ids())
