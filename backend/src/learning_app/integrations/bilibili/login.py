"""Explicit QR workflow; no browser-session import and no automatic polling."""

import hashlib
import json
import re
import sys
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from learning_app.errors import AppError

from ..access import Grant
from ..authentication import PENDING_ACCOUNT, superseded
from ..models import LoginView

ACTIVE_LOGIN = "bili_login_active"


class Login:
    def __init__(self, host):
        self.host = host

    def start(self, target_connection_id=None, *, identity=None):
        with self.host.access.lock:
            if identity and (existing := self.host.private.get(identity)):
                self._current(identity)
                if existing.get("result") or existing.get("authentication"):
                    return self.poll(identity)
                self._grant(identity, existing)
                if not existing.get("url") or not existing.get("key"):
                    raise AppError(
                        "BILI_LOGIN_RESULT_UNKNOWN",
                        "原扫码请求结果未知，未自动重发；请重新发起连接请求",
                        409,
                    )
                return LoginView(login_id=identity, url=existing["url"], state="waiting")
            old = self.host.private.get(ACTIVE_LOGIN).get("login_id")
            if old:
                state = self.host.private.get(old)
                temporary_id = state.get("connection_id") or state.get("temporary_connection_id")
                if temporary_id:
                    self._forget_temporary(temporary_id)
            scope = self.host.access.login_scope()
            if target_connection_id:
                if target_connection_id not in scope["connections"]:
                    superseded()
                scope["target_connection_id"] = target_connection_id
            temporary = self.host.access.connect(PENDING_ACCOUNT, "扫码等待", "guest")
            identity = identity or "login_" + uuid4().hex
            state = {"connection_id": temporary.connection_id, "scope": scope}
            self.host.private.put(identity, state)
            self.host.private.put(ACTIVE_LOGIN, {"login_id": identity})
        grant = self._grant(identity, state)
        data = self.host.provider.json(
            grant, "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        )
        from .provider import allowed_url

        allowed_url(data["url"])
        with self.host.access.lock:
            self.host.access.check(grant)
            self.host.private.put(
                identity, {**state, "key": data["qrcode_key"], "url": data["url"]}
            )
        return LoginView(login_id=identity, url=data["url"], state="waiting")

    def _current(self, identity):
        if self.host.private.get(ACTIVE_LOGIN).get("login_id") != identity:
            superseded()

    def _forget_temporary(self, connection_id):
        self.host.access.disconnect(
            connection_id, forget=lambda: self.host.private.remove(connection_id)
        )

    def _grant(self, identity, state):
        def check():
            self._current(identity)
            self.host.access.check_login_scope(state.get("scope"), state.get("authentication"))

        check()
        return replace(self.host.access.grant(state["connection_id"]), scope_check=check)

    def qr(self, identity):
        value = self.host.private.get(identity)
        if not value or "url" not in value:
            raise AppError("BILI_LOGIN_EXPIRED", "二维码已过期，请重新发起连接", 409)
        self._grant(identity, value)
        lock = json.loads(
            (self.host.project_root / "resources/integrations/qrcode.lock.json").read_text()
        )
        wheel = self.host.project_root / lock["path"]
        if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != lock["sha256"]:
            raise AppError("BILI_QR_NOT_PREPARED", "扫码组件尚未准备", 503)
        if str(wheel) not in sys.path:
            sys.path.insert(0, str(wheel))
        import qrcode
        from qrcode.image.svg import SvgPathImage

        image = qrcode.make(value["url"], image_factory=SvgPathImage)
        return image.to_string(encoding="unicode")

    def poll(self, identity):
        # Timer and manual checks may arrive together, including on separate route instances.
        # Serialize receipt publication in this host, leaving media playback independent.
        with self.host.lock:
            return self._poll(identity)

    def _poll(self, identity):
        state = self.host.private.get(identity)
        if not state:
            raise AppError("BILI_LOGIN_EXPIRED", "连接请求已过期，请重新发起", 409)
        self._current(identity)
        if state.get("result"):
            result = LoginView.model_validate(state["result"])
            if not state.get("published_grant"):
                raise AppError("BILI_LOGIN_EXPIRED", "原登录记录需重新核对，请重新发起连接", 409)
            self.host.access.check(Grant(**state["published_grant"]))
            if temporary := state.get("temporary_connection_id"):
                self._forget_temporary(temporary)
            return result
        grant = self._grant(identity, state)
        received = state.get("poll_receipt")
        if received is None:
            if state.get("poll_started"):
                raise AppError(
                    "BILI_LOGIN_RESULT_UNKNOWN", "扫码回执未确认，未更新原连接；请重新发起扫码", 409
                )
            state = {**state, "poll_started": True}
            self.host.private.put(identity, state)
            received = self.host.provider.credential_poll(grant, state["key"])
        data = received["data"]
        code = data["code"]
        if code != 0:
            state = {**state, "poll_started": False}
            self.host.private.put(identity, state)
            self.host.access.check(grant)
            return LoginView(
                login_id=identity, state={86038: "expired", 86090: "scanned"}.get(code, "waiting")
            )
        # Persist actual returned credentials before publishing connection identity.
        state = {**state, "poll_receipt": received, "poll_started": False}
        self.host.private.put(identity, state)
        self.host.access.check(grant)
        params = parse_qs(urlsplit(data.get("url") or "").query)
        allowed = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"]
        cookies = {k: params[k][0] for k in allowed if k in params}
        cookies.update(received.get("cookies", {}))
        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
            raise AppError("BILI_LOGIN_INCOMPLETE", "平台未返回完整连接信息，请重新连接", 409)
        if not isinstance(cookies["DedeUserID"], str) or not re.fullmatch(
            r"[1-9][0-9]{0,19}", cookies["DedeUserID"]
        ):
            raise AppError("BILI_LOGIN_INCOMPLETE", "平台未返回可核对的账号身份", 409)

        def stage(plan):
            nonlocal state
            state = {**state, "authentication": plan, "target_connection_id": plan["connection_id"]}
            self.host.private.put(identity, state)

        def install(connection_id):
            self.host.private.put(
                connection_id,
                {
                    "cookies": cookies,
                    "auth_receipt": identity,
                    "connection_epoch": state["authentication"]["epoch"],
                },
            )

        def installed(plan):
            saved = self.host.private.get(plan["connection_id"])
            return (
                saved.get("auth_receipt") == identity
                and saved.get("connection_epoch") == plan["epoch"]
                and saved.get("cookies", {}).get("DedeUserID") == plan["account_id"]
            )

        connection = self.host.access.authenticate(
            cookies["DedeUserID"],
            "B站账号 " + cookies["DedeUserID"],
            install,
            scope=state.get("scope"),
            plan=state.get("authentication"),
            stage=stage,
            installed=installed,
            permission=lambda: self.host.access.check(grant),
        )
        published = self.host.access.grant(connection.connection_id)
        result = LoginView(login_id=identity, state="connected", connection=connection)
        self.host.private.put(
            identity,
            {
                "result": result.model_dump(),
                "temporary_connection_id": state["connection_id"],
                "published_grant": {
                    "connection_id": published.connection_id,
                    "connection_epoch": published.connection_epoch,
                    "module_epoch": published.module_epoch,
                },
            },
        )
        self._forget_temporary(state["connection_id"])
        return result
