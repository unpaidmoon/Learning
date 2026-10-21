"""Local browser capability, Host/Origin and CSRF enforcement; no Harness credentials."""

import secrets

from starlette.responses import JSONResponse

from learning_app.config import AppConfig

COOKIE_NAME = "learning_session"


class BrowserSessions:
    def __init__(self):
        self.sessions: dict[str, str] = {}

    def create(self, existing: str | None = None) -> tuple[str, str]:
        if existing in self.sessions:
            return existing, self.sessions[existing]
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[token] = csrf
        return token, csrf

    def valid(self, token: str | None, csrf: str | None = None) -> bool:
        expected = self.sessions.get(token or "")
        return expected is not None and (csrf is None or secrets.compare_digest(expected, csrf))


class LocalBoundaryMiddleware:
    def __init__(self, app, config: AppConfig, sessions: BrowserSessions):
        self.app = app
        self.config = config
        self.sessions = sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        names = [key.decode().lower() for key, _value in scope["headers"]]
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        host = headers.get("host", "")
        origin = headers.get("origin")
        code = None
        if names.count("host") != 1 or names.count("origin") > 1:
            code = "AMBIGUOUS_ORIGIN"
        elif host not in self.config.allowed_hosts:
            code = "HOST_DENIED"
        elif origin is not None and origin not in self.config.allowed_origins:
            code = "ORIGIN_DENIED"
        elif headers.get("sec-fetch-site") == "cross-site":
            code = "ORIGIN_DENIED"
        if code:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                response = JSONResponse({"code": code, "message": "请求来源未获授权"}, 403)
                await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
