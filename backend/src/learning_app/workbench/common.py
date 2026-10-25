"""Small transactional helpers shared only by learning business operations."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def run_input_hash(
    task_id: str,
    unit_id: str,
    scope_revision: int,
    snapshot_id: str | None,
    settings: dict,
    prompt: str,
    input_contract: dict | None = None,
) -> str:
    if input_contract is not None:
        return _hash(
            {
                "contract_version": 2,
                "task_id": task_id,
                "unit_id": unit_id,
                "scope_revision": scope_revision,
                "settings": settings,
                "prompt": prompt,
                "input_contract": input_contract,
            }
        )
    return _hash(
        {
            "task_id": task_id,
            "unit_id": unit_id,
            "scope_revision": scope_revision,
            "snapshot_id": snapshot_id,
            "settings": settings,
            "prompt": prompt,
        }
    )


def run_still_in_scope(run: dict, task: dict, unit: dict) -> bool:
    # Excluding a different unit need not discard an already-read result.
    return (
        unit["run_id"] == run["run_id"]
        and unit["excluded_at_scope_revision"] is None
        and (
            not run["input_contract"]
            or run["input_contract"]["work_spec_hash"] == _hash(unit["work_spec"])
        )
        and run["input_hash"]
        == run_input_hash(
            run["task_id"],
            run["unit_id"],
            run["scope_revision"],
            unit["source_snapshot_id"],
            task["settings"],
            run["prompt"],
            run["input_contract"],
        )
    )


def prior_operation(
    conn: Connection,
    course_id: str,
    kind: str,
    operation_id: str,
    payload: dict,
) -> dict | None:
    row = (
        conn.execute(select(s.operations).where(s.operations.c.operation_id == operation_id))
        .mappings()
        .first()
    )
    if row is None:
        return None
    if (
        row["course_id"] != course_id
        or row["kind"] != kind
        or row["payload_hash"] != _hash(payload)
    ):
        raise AppError("LEARNING_OPERATION_CONFLICT", "操作身份已用于其他内容，请核对原操作")
    return row["result"]


def save_operation(
    conn: Connection,
    course_id: str,
    kind: str,
    operation_id: str,
    payload: dict,
    result: BaseModel,
) -> None:
    conn.execute(
        insert(s.operations).values(
            operation_id=operation_id,
            course_id=course_id,
            kind=kind,
            payload_hash=_hash(payload),
            result=result.model_dump(mode="json"),
            created_at=now(),
        )
    )
