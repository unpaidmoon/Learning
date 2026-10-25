"""A retained package stage is never an application execution root until active."""

import json
from pathlib import Path

from learning_app.errors import AppError

STAGE_MARKER = ".learning-data-stage.json"


def read_stage(directory, states):
    try:
        value = json.loads((Path(directory) / STAGE_MARKER).read_text("utf-8"))
        if value["format"] != 1 or value["state"] not in states:
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise AppError("DATA_STAGE_NOT_READY", "数据暂存就绪记录无法核对") from None


def stage_identity(directory, states):
    return {
        key: value
        for key, value in read_stage(directory, states).items()
        if key not in {"state", "activation_operation_id"}
    }


def require_active_data(data_directory):
    parent = Path(data_directory).resolve().parent
    marker = parent / STAGE_MARKER
    activation_file = parent / "activation.json"
    if (
        not marker.exists()
        and not activation_file.exists()
        and not parent.name.startswith(".learning-stage-")
    ):
        return
    try:
        value = read_stage(parent, {"active"})
        activation = json.loads(activation_file.read_text("utf-8"))
        if (
            not activation["operation_id"]
            or value["activation_operation_id"] != activation["operation_id"]
        ):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AppError):
        raise AppError(
            "DATA_STAGE_NOT_ACTIVE", "这是尚未激活的导入暂存，未启动旧任务或模型", 503
        ) from None


def activate_stage(directory):
    from learning_app.data_operations import atomic_record

    marker = Path(directory) / STAGE_MARKER
    value = read_stage(directory, {"ready", "active"})
    activation = json.loads((Path(directory) / "activation.json").read_text("utf-8"))
    identity = activation["operation_id"]
    if not identity or (
        value["state"] == "active" and value.get("activation_operation_id") != identity
    ):
        raise AppError("DATA_STAGE_NOT_READY", "数据暂存激活身份无法核对")
    atomic_record(marker, {**value, "state": "active", "activation_operation_id": identity})
