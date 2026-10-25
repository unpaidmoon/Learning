"""Reconnect one frozen source; never rewrite its account or resource identity."""

from sqlalchemy import func, select, update

from learning_app.errors import AppError

from . import schema as s
from .access import Grant
from .bilibili.login import Login
from .common import content_hash
from .models import AccountSourceRecovery, AnonymousSourceRecovery, ConnectionView


class SourceRecovery:
    def __init__(self, host):
        self.host = host

    def begin(self, course_id, asset_id, value):
        with self.host.lock:
            source = self.host.source(asset_id, value.source_version, course_id)
            if source is None:
                raise AppError("SOURCE_NOT_FOUND", "原B站来源不存在，未创建新连接", 404)
            self.host.access.require_enabled()
            context = {
                "course_id": course_id,
                "asset_id": asset_id,
                "source_version": value.source_version,
                "connection_id": source["connection_id"],
            }
            record_id = "recover_" + content_hash({"operation": value.operation_id})[:40]
            saved = self.host.private.get(record_id)
            if saved and saved.get("context") != context:
                raise AppError(
                    "BILI_RECOVERY_OPERATION_CONFLICT", "原恢复操作不属于此资料，请重新发起", 409
                )
            with self.host.repository.engine.connect() as conn:
                account = (
                    conn.execute(
                        select(s.connections).where(
                            s.connections.c.connection_id == source["connection_id"]
                        )
                    )
                    .mappings()
                    .first()
                )
            if account is None or account["platform"] != "bilibili":
                raise AppError("CONNECTION_UNAVAILABLE", "原来源连接不存在", 409)
            if account["account_id"] == "guest":
                return self._anonymous(record_id, context, dict(account), saved)
            identity = (
                saved.get("login_id")
                if saved
                else "login_" + content_hash(context | {"operation": value.operation_id})[:32]
            )
            if not saved:
                self.host.private.put(record_id, {"context": context, "login_id": identity})
            login = Login(self.host).start(source["connection_id"], identity=identity)
            return AccountSourceRecovery(login=login, account_label=account["account_label"])

    def _anonymous(self, record_id, context, account, saved):
        access = self.host.access
        with access.lock:
            scope = access.login_scope()
            current = scope["connections"].get(account["connection_id"])
            if current is None or current["account_id"] != "guest":
                raise AppError("CONNECTION_UNAVAILABLE", "原匿名身份不可用", 409)
            if saved and saved.get("result"):
                access.check(Grant(**saved["grant"]))
                return AnonymousSourceRecovery.model_validate(saved["result"])
            plan = saved.get("plan") if saved else None
            if plan is None:
                with self.host.repository.engine.connect() as conn:
                    epoch = (
                        conn.execute(select(func.max(s.connections.c.epoch))).scalar_one() or 0
                    ) + 1
                plan = {"module_epoch": scope["module_epoch"], "before": current, "epoch": epoch}
                self.host.private.put(record_id, {"context": context, "plan": plan})
            if plan["module_epoch"] != scope["module_epoch"]:
                raise AppError("BILI_RECOVERY_STALE", "原恢复操作已失效，请重新发起", 409)
            recovered = current["state"] == "guest" and current["epoch"] == plan["epoch"]
            if not recovered:
                if current != plan["before"] or current["state"] not in {"guest", "disconnected"}:
                    raise AppError("BILI_RECOVERY_STALE", "原匿名连接状态已变化，请重新发起", 409)
                if current["state"] == "disconnected":
                    if access.on_revoke:
                        access.on_revoke(account["connection_id"])
                    self.host.private.remove(account["connection_id"])
                    with self.host.repository.engine.begin() as conn:
                        changed = conn.execute(
                            update(s.connections)
                            .where(
                                s.connections.c.connection_id == account["connection_id"],
                                s.connections.c.epoch == current["epoch"],
                                s.connections.c.state == "disconnected",
                            )
                            .values(state="guest", epoch=plan["epoch"])
                        ).rowcount
                        if changed != 1:
                            raise AppError("BILI_RECOVERY_STALE", "原匿名连接已变化", 409)
            grant = access.grant(account["connection_id"])
            result = AnonymousSourceRecovery(
                connection=ConnectionView(
                    connection_id=account["connection_id"],
                    account_label=account["account_label"],
                    state="guest",
                )
            )
            self.host.private.put(
                record_id,
                {
                    "context": context,
                    "plan": plan,
                    "result": result.model_dump(),
                    "grant": {
                        "connection_id": grant.connection_id,
                        "module_epoch": grant.module_epoch,
                        "connection_epoch": grant.connection_epoch,
                    },
                },
            )
            return result
