"""Remember local queue intent across an explicitly requested data move."""

import json

from sqlalchemy import select

from learning_app.data_operations import atomic_record
from learning_app.imported_executions import imported_executions
from learning_app.workbench import schema as s
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.tasks import TaskStore


class DataMaintenance:
    def __init__(self, repository):
        self.repository = repository
        self.path = repository.root / "maintenance-state.json"

    def freeze_intent(self, operation_id):
        old = None
        if self.path.exists():
            old = json.loads(self.path.read_text("utf-8"))
            if old["operation_id"] != operation_id or old["state"] == "restored":
                old = None
        fence = imported_executions(self.repository)
        with self.repository._lock, self.repository.engine.begin() as connection:
            jobs = (
                old["local_jobs"]
                if old
                else [
                    dict(row)
                    for row in connection.execute(
                        select(
                            s.preparations.c.job_id, s.preparations.c.epoch, s.preparations.c.state
                        ).where(s.preparations.c.state.in_(["queued", "running"]))
                    ).mappings()
                    if not fence.contains("preparation", row["job_id"])
                ]
            )
            tasks = TaskStore(self.repository)
            paused = list(old["paused_tasks"]) if old else []
            for row in (
                connection.execute(
                    select(s.tasks).where(s.tasks.c.state.in_(["queued", "running", "unknown"]))
                )
                .mappings()
                .all()
            ):
                if not fence.contains("task", row["task_id"]):
                    tasks._event(
                        connection,
                        row,
                        "data_maintenance",
                        {"operation_id": operation_id},
                        state="paused",
                    )
                    paused.append({"task_id": row["task_id"], "state": row["state"]})
            record = {
                "format": 1,
                "operation_id": operation_id,
                "state": "pending",
                "local_jobs": jobs,
                "paused_tasks": paused,
            }
            # Intent precedes the transaction commit and subsequent epoch fence.
            atomic_record(self.path, record)
        return record

    def restore_local_jobs(self):
        if not self.path.exists():
            return
        record = json.loads(self.path.read_text("utf-8"))
        if record["state"] == "restored":
            return
        fence = imported_executions(self.repository)
        store = PreparationStore(self.repository)
        resumed = []
        with self.repository._lock, self.repository.engine.begin() as connection:
            for old in record["local_jobs"]:
                row = (
                    connection.execute(
                        select(s.preparations).where(s.preparations.c.job_id == old["job_id"])
                    )
                    .mappings()
                    .first()
                )
                if (
                    row
                    and not fence.contains("preparation", row["job_id"])
                    and row["state"] == "paused"
                    and row["epoch"] == old["epoch"] + 1
                ):
                    store._event(
                        connection,
                        row,
                        "data_maintenance_resume",
                        {"operation_id": record["operation_id"]},
                        state="queued",
                        epoch=row["epoch"] + 1,
                    )
                    resumed.append(row["job_id"])
        record.update(state="restored", resumed_local_jobs=resumed)
        atomic_record(self.path, record)
