"""Revocable connection grants checked at creation and every outbound request."""

from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from threading import Event, RLock
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError

from . import schema as s
from .authentication import PENDING_ACCOUNT
from .models import ConnectionView, ModuleView

PLATFORMS = [
    "bilibili",
    "chaoxing",
    "maimemo",
    "zotero",
    "icourse",
    "rainclassroom",
    "xuetang",
    "zhihuishu",
]


@dataclass(frozen=True)
class Grant:
    connection_id: str
    module_epoch: int
    connection_epoch: int
    cancelled: Event | None = field(default=None, compare=False, repr=False)
    scope_check: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    operation_id: str | None = field(default=None, compare=False, repr=False)


class Access:
    def __init__(self, repository, on_revoke=None):
        self.repository = repository
        self.lock = RLock()
        self.on_revoke = on_revoke

    def modules(self, *, connection=None):
        with (
            self.repository.engine.connect() if connection is None else nullcontext(connection)
        ) as conn:
            rows = {r["platform"]: r for r in conn.execute(select(s.modules)).mappings()}
            connections = list(
                conn.execute(
                    select(s.connections).order_by(
                        s.connections.c.epoch, s.connections.c.connection_id
                    )
                ).mappings()
            )
        result = []
        for platform in PLATFORMS:
            active = next(
                (
                    r
                    for r in reversed(connections)
                    if r["platform"] == platform
                    and r["state"] != "disconnected"
                    and r["account_id"] != PENDING_ACCOUNT
                ),
                None,
            )
            result.append(
                ModuleView(
                    platform=platform,
                    enabled=bool(rows.get(platform, {}).get("enabled")),
                    implemented=platform == "bilibili",
                    connection_id=active["connection_id"] if active else None,
                    account_label=active["account_label"] if active else None,
                    connection_state=active["state"] if active else "disconnected",
                )
            )
        return result

    def change(self, platform, enabled):
        if platform != "bilibili":
            raise AppError("PLATFORM_NOT_IMPLEMENTED", "该平台尚未接入", 409)
        with self.lock:
            with self.repository.engine.begin() as conn:
                row = (
                    conn.execute(select(s.modules).where(s.modules.c.platform == platform))
                    .mappings()
                    .first()
                )
                if row:
                    if row["enabled"] != enabled:
                        conn.execute(
                            update(s.modules)
                            .where(s.modules.c.platform == platform)
                            .values(enabled=enabled, epoch=row["epoch"] + 1)
                        )
                else:
                    conn.execute(
                        insert(s.modules).values(platform=platform, enabled=enabled, epoch=1)
                    )
            if not enabled and self.on_revoke:
                self.on_revoke(None)
        return next(v for v in self.modules() if v.platform == platform)

    def require_enabled(self):
        if not next(v for v in self.modules() if v.platform == "bilibili").enabled:
            raise AppError("PLATFORM_DISABLED", "请先启用B站接入；已保存资料仍可使用", 409)

    def connect(self, account_id="guest", label="匿名访问", state="guest"):
        with self.lock:
            self.require_enabled()
            identity = "bili_" + uuid4().hex
            with self.repository.engine.begin() as conn:
                epoch = (
                    conn.execute(select(func.max(s.connections.c.epoch))).scalar_one() or 0
                ) + 1
                conn.execute(
                    insert(s.connections).values(
                        connection_id=identity,
                        platform="bilibili",
                        account_id=account_id,
                        account_label=label,
                        state=state,
                        epoch=epoch,
                    )
                )
            return ConnectionView(connection_id=identity, account_label=label, state=state)

    def grant(self, connection_id, *, connection=None):
        # A caller-owned transaction already serializes the snapshot. Taking
        # Access.lock here would invert it with a concurrent module change.
        with (
            self.lock if connection is None else nullcontext(),
            (
                self.repository.engine.connect() if connection is None else nullcontext(connection)
            ) as conn,
        ):
            module = (
                conn.execute(select(s.modules).where(s.modules.c.platform == "bilibili"))
                .mappings()
                .first()
            )
            connection = (
                conn.execute(
                    select(s.connections).where(s.connections.c.connection_id == connection_id)
                )
                .mappings()
                .first()
            )
            if not module or not module["enabled"]:
                raise AppError("PLATFORM_DISABLED", "B站接入已停用", 409)
            if (
                not connection
                or connection["platform"] != "bilibili"
                or connection["state"] not in {"guest", "connected"}
            ):
                raise AppError("CONNECTION_UNAVAILABLE", "原账号连接不可用，请重新连接", 409)
            return Grant(connection_id, module["epoch"], connection["epoch"])

    def check(self, grant, *, connection=None):
        if grant.scope_check is not None:
            grant.scope_check()
        if grant.cancelled is not None and grant.cancelled.is_set():
            raise AppError("PREPARATION_INTERRUPTED", "原任务已暂停，未继续平台访问", 409)
        if self.grant(grant.connection_id, connection=connection) != grant:
            raise AppError("PLATFORM_GRANT_REVOKED", "原调用授权已失效，请核对后恢复", 409)

    def scoped(self, grant, cancelled):
        return replace(grant, cancelled=cancelled)

    def disconnect(self, connection_id, *, forget=None):
        with self.lock:
            with self.repository.engine.begin() as conn:
                changed = conn.execute(
                    update(s.connections)
                    .where(
                        s.connections.c.connection_id == connection_id,
                        s.connections.c.state != "disconnected",
                    )
                    .values(state="disconnected", epoch=s.connections.c.epoch + 1)
                ).rowcount
            if changed and self.on_revoke:
                self.on_revoke(connection_id)
            if forget is not None:
                forget()

    def expire(self, grant):
        """Apply definitive authentication failure only to the dispatched credential epoch."""
        changed = False
        with self.lock:
            with self.repository.engine.begin() as conn:
                module = (
                    conn.execute(select(s.modules).where(s.modules.c.platform == "bilibili"))
                    .mappings()
                    .first()
                )
                if module and module["enabled"] and module["epoch"] == grant.module_epoch:
                    changed = bool(
                        conn.execute(
                            update(s.connections)
                            .where(
                                s.connections.c.connection_id == grant.connection_id,
                                s.connections.c.epoch == grant.connection_epoch,
                                s.connections.c.state == "connected",
                            )
                            .values(state="expired", epoch=s.connections.c.epoch + 1)
                        ).rowcount
                    )
            # Commit the revoked epoch before pausing. Keep authentication serialized
            # until old jobs are paused, so logging in cannot implicitly resume them.
            if changed and self.on_revoke:
                self.on_revoke(grant.connection_id)
        return changed

    def login_scope(self):
        from .authentication import capture

        return capture(self)

    def check_login_scope(self, scope, plan=None):
        from .authentication import validate

        validate(self, scope, plan)

    def authenticate(self, account_id, label, install, **kwargs):
        from .authentication import authenticate

        return authenticate(self, account_id, label, install, **kwargs)

    def request(self, grant, purpose):
        # Linearize disable against the decision to dispatch; network work holds no DB lock.
        if grant.scope_check is not None:
            grant.scope_check()
        with self.lock:
            self.check(replace(grant, scope_check=None))
            identity = "out_" + uuid4().hex
            with self.repository.engine.begin() as conn:
                conn.execute(
                    insert(s.requests).values(
                        request_id=identity,
                        connection_id=grant.connection_id,
                        purpose=purpose + (":" + grant.operation_id if grant.operation_id else ""),
                        state="dispatched",
                    )
                )
            return identity

    def receipt(self, identity, status=None):
        with self.repository.engine.begin() as conn:
            conn.execute(
                update(s.requests)
                .where(s.requests.c.request_id == identity)
                .values(state="received" if status is not None else "unknown", status_code=status)
            )
