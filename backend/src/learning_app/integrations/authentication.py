"""Frozen login intent and durable credential publication, without supplier protocols."""

from uuid import uuid4

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError

from . import schema as s
from .models import ConnectionView

PENDING_ACCOUNT = "__login_pending__"


def superseded():
    raise AppError(
        "BILI_LOGIN_SUPERSEDED", "连接状态已变化，原扫码回执未重新授权；请重新发起连接", 409
    )


def state(access):
    with access.repository.engine.connect() as conn:
        module = (
            conn.execute(select(s.modules).where(s.modules.c.platform == "bilibili"))
            .mappings()
            .first()
        )
        rows = [
            dict(r)
            for r in conn.execute(
                select(s.connections).where(s.connections.c.platform == "bilibili")
            ).mappings()
        ]
    if not module or not module["enabled"]:
        raise AppError("PLATFORM_DISABLED", "B站接入已停用", 409)
    return dict(module), rows


def vector(rows):
    return {
        r["connection_id"]: {k: r[k] for k in ("account_id", "epoch", "state")}
        for r in rows
        if r["account_id"] != PENDING_ACCOUNT
    }


def capture(access):
    with access.lock:
        module, rows = state(access)
        return {"module_epoch": module["epoch"], "connections": vector(rows)}


def validate(access, scope, plan=None):
    with access.lock:
        module, rows = state(access)
        if not isinstance(scope, dict) or module["epoch"] != scope.get("module_epoch"):
            superseded()
        current = vector(rows)
        if plan is not None:
            expected = dict(plan["selection"])
            identity = plan["connection_id"]
            target = current.get(identity)
            if (
                target
                and target["account_id"] == plan["account_id"]
                and target["epoch"] == plan["epoch"]
                and target["state"] in {"expired", "connected"}
            ):
                expected[identity] = target
            if current != expected:
                superseded()
        else:
            expected = scope.get("connections")
            if not isinstance(expected, dict) or current.keys() != expected.keys():
                superseded()
            for identity, old in expected.items():
                if current[identity] != old and current[identity] != {
                    **old,
                    "state": "expired",
                    "epoch": old["epoch"] + 1,
                }:
                    superseded()
                if current[identity] != old and old["state"] != "connected":
                    superseded()
        return module, rows


def authenticate(
    access,
    account_id,
    label,
    install,
    *,
    scope=None,
    plan=None,
    stage=None,
    installed=None,
    permission=None,
):
    with access.lock:
        scope = scope if scope is not None else capture(access)
        module, rows = state(access)
        if module["epoch"] != scope.get("module_epoch"):
            superseded()
        if plan is not None:
            if plan["account_id"] != account_id or plan["module_epoch"] != module["epoch"]:
                superseded()
            saved = next((r for r in rows if r["connection_id"] == plan["connection_id"]), None)
            if (
                saved
                and saved["account_id"] == account_id
                and saved["state"] == "connected"
                and saved["epoch"] == plan["epoch"]
            ):
                if installed is None or not installed(plan):
                    superseded()
                return ConnectionView(
                    connection_id=saved["connection_id"],
                    account_label=saved["account_label"],
                    state="connected",
                )
        if permission is not None:
            permission()
        module, rows = validate(access, scope, plan)
        if plan is None:
            candidates = [
                r
                for r in rows
                if r["account_id"] == account_id
                and r["state"] in {"connected", "expired", "disconnected"}
            ]
            if target := scope.get("target_connection_id"):
                target_row = next((r for r in rows if r["connection_id"] == target), None)
                if target_row is None or target_row["account_id"] != account_id:
                    raise AppError(
                        "BILI_ACCOUNT_MISMATCH",
                        "扫码账号与原来源不同，请使用原账号；旧来源没有改绑",
                        409,
                    )
                candidates = [r for r in candidates if r["connection_id"] == target]
                if not candidates:
                    superseded()
            original = max(candidates, key=lambda r: r["epoch"]) if candidates else None
            with access.repository.engine.connect() as conn:
                epoch = (
                    conn.execute(select(func.max(s.connections.c.epoch))).scalar_one() or 0
                ) + 1
            plan = {
                "connection_id": original["connection_id"] if original else "bili_" + uuid4().hex,
                "account_id": account_id,
                "module_epoch": module["epoch"],
                "epoch": epoch,
                "selection": vector(rows),
            }
            if stage is not None:
                stage(plan)
            durable = False
        else:
            durable = bool(installed and installed(plan))
        original = next((r for r in rows if r["connection_id"] == plan["connection_id"]), None)
        if original and access.on_revoke:
            # Pause old jobs before publishing the new epoch; logging in does not resume them.
            access.on_revoke(original["connection_id"])
        try:
            if not durable:
                install(plan["connection_id"])
            with access.repository.engine.begin() as conn:
                values = dict(account_label=label, state="connected", epoch=plan["epoch"])
                if original:
                    changed = conn.execute(
                        update(s.connections)
                        .where(
                            s.connections.c.connection_id == original["connection_id"],
                            s.connections.c.epoch == original["epoch"],
                            s.connections.c.state == original["state"],
                        )
                        .values(**values)
                    ).rowcount
                    if changed != 1:
                        superseded()
                else:
                    conn.execute(
                        insert(s.connections).values(
                            connection_id=plan["connection_id"],
                            platform="bilibili",
                            account_id=account_id,
                            **values,
                        )
                    )
        except Exception:
            # A credential write may have succeeded before its acknowledgement failed.
            # Revoke the old usable epoch; the journaled plan can reconcile this exact effect.
            if original and original["state"] in {"connected", "expired"}:
                with access.repository.engine.begin() as conn:
                    conn.execute(
                        update(s.connections)
                        .where(
                            s.connections.c.connection_id == original["connection_id"],
                            s.connections.c.epoch == original["epoch"],
                        )
                        .values(state="expired", epoch=plan["epoch"])
                    )
            raise
        return ConnectionView(
            connection_id=plan["connection_id"], account_label=label, state="connected"
        )
