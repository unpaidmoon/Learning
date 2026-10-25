"""Durable native appearance fields, without Host configuration authority."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from uuid import uuid4

from learning_app.errors import AppError

_PREFERENCES = frozenset({"light", "dark", "system"})
_BASE = {
    "preference": "system", "fontSize": 14, "codeFontSize": 11, "terminalFontSize": 13,
    "textFontFamily": "", "codeFontFamily": "", "terminalFontFamily": "",
}
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


def _schema() -> dict:
    """Pinned Schemastery toJSON reference envelope used by ConfigForms.

    The full section retains native typography defaults and validates writes.
    """
    refs = {
        "0": {"type": "object", "dict": {
            "preference": 1, "fontSize": 5, "codeFontSize": 6, "terminalFontSize": 7,
            "textFontFamily": 8, "codeFontFamily": 8, "terminalFontFamily": 8,
        }, "meta": {}},
        "1": {"type": "union", "list": [2, 3, 4], "meta": {"default": "system"}},
        "2": {"type": "const", "value": "light", "meta": {"required": True}},
        "3": {"type": "const", "value": "dark", "meta": {"required": True}},
        "4": {"type": "const", "value": "system", "meta": {"required": True}},
        "5": {"type": "number", "meta": {"min": 10, "max": 22, "step": 1, "default": 14}},
        "6": {"type": "number", "meta": {"min": 10, "max": 16, "step": 1, "default": 11}},
        "7": {"type": "number", "meta": {"min": 10, "max": 20, "step": 1, "default": 13}},
        "8": {"type": "string", "meta": {"default": ""}},
    }
    return {"uid": 0, "refs": refs}


class DesktopThemePreferences:
    def __init__(self, data_dir: Path):
        self.path = data_dir.resolve() / "desktop-ui" / "theme.json"
        with _LOCKS_LOCK:
            self.lock = _LOCKS.setdefault(self.path, threading.RLock())

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"version": 1, "preference": "system", "revision": 0}
        try:
            if self.path.stat().st_size > 4096:
                raise ValueError("oversized theme document")
            value = json.loads(self.path.read_text("utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or value.get("preference") not in _PREFERENCES
                    or type(value.get("revision")) is not int or value["revision"] < 0):
                raise ValueError("invalid theme document")
            return {"version": 1, "revision": value["revision"], **self._values(value)}
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise AppError("DESKTOP_THEME_STORAGE_INVALID", "外观设置记录无效，请检查本地数据", 503) from exc

    @staticmethod
    def _values(record: dict) -> dict:
        values = {key: record.get(key, default) for key, default in _BASE.items()}
        valid = isinstance(values["preference"], str) and values["preference"] in _PREFERENCES
        for key, upper in (("fontSize", 22), ("codeFontSize", 16), ("terminalFontSize", 20)):
            valid = valid and type(values[key]) in (int, float) and 10 <= values[key] <= upper
        for key in ("textFontFamily", "codeFontFamily", "terminalFontFamily"):
            valid = valid and isinstance(values[key], str) and len(values[key]) <= 256
        if not valid:
            raise AppError("DESKTOP_THEME_INVALID", "外观设置值无效", 422)
        return values

    def _view(self, record: dict) -> dict:
        return {
            "ns": "ui-theme", "autoGenerate": False, "schema": _schema(),
            "value": self._values(record), "base": dict(_BASE),
            "user": self._values(record) if record["revision"] else {},
            "applies": "live", "secrets": [], "revision": record["revision"],
        }

    def handle(self, method: str, request: dict) -> dict:
        if method not in {"settings/describe", "settings/query", "settings/mutate"}:
            raise AppError("DESKTOP_METHOD_DENIED", "此设置操作不在桌面范围内", 403)
        allowed = {"ns", "namespace"} | ({"ops", "expectedRevision"} if method == "settings/mutate" else set())
        if set(request) - allowed:
            raise AppError("DESKTOP_THEME_INVALID", "外观设置请求无效", 422)
        namespace = request.get("ns", request.get("namespace"))
        if (namespace is not None and namespace != "ui-theme") or (
            "ns" in request and "namespace" in request and request["ns"] != request["namespace"]
        ):
            raise AppError("DESKTOP_METHOD_DENIED", "只允许更改应用外观设置", 403)
        if method != "settings/describe" and namespace != "ui-theme":
            raise AppError("DESKTOP_THEME_INVALID", "外观设置命名空间无效", 422)
        with self.lock:
            record = self._read()
            if method == "settings/describe":
                # There is no editable Host config document in this adapter.
                return {"writable": True, "hasDocument": False, "namespaces": [self._view(record)]}
            if method == "settings/query":
                return self._view(record)
            ops = request.get("ops")
            if not isinstance(ops, list) or not 1 <= len(ops) <= len(_BASE):
                raise AppError("DESKTOP_THEME_INVALID", "外观设置请求无效", 422)
            values = self._values(record)
            for op in ops:
                if (not isinstance(op, dict) or set(op) - {"op", "path", "value"}
                        or op.get("op") not in {"set", "unset"}
                        or not isinstance(op.get("path"), list) or len(op["path"]) != 1
                        or not isinstance(op["path"][0], str) or op["path"][0] not in _BASE):
                    raise AppError("DESKTOP_METHOD_DENIED", "只允许修改原生外观选项", 403)
                key = op["path"][0]
                values[key] = op.get("value") if op["op"] == "set" else _BASE[key]
            values = self._values(values)
            if "expectedRevision" in request:
                expected = request["expectedRevision"]
                if type(expected) is not int or expected < 0:
                    raise AppError("DESKTOP_THEME_INVALID", "外观设置版本无效", 422)
                if expected != record["revision"]:
                    raise AppError("settings/conflict", "外观设置已有较新版本，请刷新后再试", 409)
            if values != self._values(record):
                record = {"version": 1, **values, "revision": record["revision"] + 1}
                self._write(record)
            return self._view(record)

    def _write(self, record: dict) -> None:
        temporary = self.path.with_name(".theme-" + uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise AppError("DESKTOP_THEME_SAVE_FAILED", "外观设置未能保存，请稍后重试", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def project_index(self, html: str) -> str:
        """Project the same durable preference into the native pre-plugin boot."""
        with self.lock:
            preference = self._read()["preference"]
        light = ":root{color-scheme:light}body{background-color:#fff;--dsh-boot-bg:#fff}"
        dark = ":root{color-scheme:dark}body{background-color:#151517;--dsh-boot-bg:#151517}"
        css = light if preference == "light" else dark if preference == "dark" else (
            light + "@media(prefers-color-scheme:dark){" + dark + "}"
        )
        html = re.sub(r"<style>[^<]*--dsh-boot-bg:[^<]*</style>",
                      lambda _match: "<style>" + css + "</style>", html, count=1)
        return re.sub(r'(const preference = )"(?:light|dark|system)"(?=\s*\n\s*const systemDark = preference)',
                      lambda match: match[1] + json.dumps(preference), html, count=1)
