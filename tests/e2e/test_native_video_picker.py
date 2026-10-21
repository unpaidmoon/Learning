"""Actual Windows file dialogs, restricted to this runner's owned backend.

Only generated synthetic media is selected. No global keyboard, mouse, focus,
clipboard, desktop capture or directory-content read is used. Each Win32 message
rechecks the original live backend process, dialog ownership and ancestry.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

from test_browser import ROOT, Browser, Instance, port_closed, wait_until, write_json


class OwnedPicker:
    TITLE = "选择本地视频（原地引用，不复制）"

    def __init__(self, instance: Instance):
        self.pid = instance.record["backend_pid"]
        if self.pid not in instance.record["owned_pids"]:
            raise AssertionError("BACKEND_OWNERSHIP_MISSING")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.EnumChildWindows.argtypes = [
            wintypes.HWND,
            self.callback_type,
            wintypes.LPARAM,
        ]
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        self.user.IsChild.restype = wintypes.BOOL
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.GetDlgCtrlID.argtypes = [wintypes.HWND]
        self.user.GetDlgCtrlID.restype = ctypes.c_int
        self.user.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetDlgItem.restype = wintypes.HWND
        self.user.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user.SendMessageTimeoutW.restype = wintypes.LPARAM
        self.user.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user.PostMessageW.restype = wintypes.BOOL
        self.dialog: int | None = None
        self.evidence: list[dict] = []

    def _owned(self, hwnd: int, *, child: bool = False) -> None:
        if self.kernel.WaitForSingleObject(self.handle, 0) != 258:
            raise AssertionError("ORIGINAL_BACKEND_EXITED")
        pid = wintypes.DWORD()
        if not self.user.IsWindow(hwnd):
            raise AssertionError("OWNED_WINDOW_CLOSED")
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != self.pid:
            raise AssertionError("WINDOW_OWNERSHIP_MISMATCH")
        if child and (self.dialog is None or not self.user.IsChild(self.dialog, hwnd)):
            raise AssertionError("CONTROL_ANCESTRY_MISMATCH")

    def _class(self, hwnd: int) -> str:
        self._owned(hwnd)
        buffer = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def find(self) -> int | None:
        matches = []

        @self.callback_type
        def inspect(hwnd, _):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != self.pid:
                return True  # No title, text or controls of unrelated windows.
            if self._class(hwnd) == "#32770" and self.user.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(256)
                self.user.GetWindowTextW(hwnd, title, len(title))
                if title.value == self.TITLE:
                    matches.append(hwnd)
            return True

        self.user.EnumWindows(inspect, 0)
        if len(matches) > 1:
            raise AssertionError("PICKER_IDENTITY_AMBIGUOUS")
        if matches:
            self.dialog = matches[0]
        return matches[0] if matches else None

    def _filename_edit(self) -> int:
        self._owned(self.dialog)
        candidates = []
        inventory = []

        @self.callback_type
        def inspect(hwnd, _):
            self._owned(hwnd, child=True)
            control_id = self.user.GetDlgCtrlID(hwnd)
            class_name = self._class(hwnd)
            inventory.append({"class": class_name, "control_id": control_id})
            if class_name != "Edit":
                return True
            if control_id == 0x480:
                candidates.append(hwnd)
                return True
            parent = self.user.GetParent(hwnd)
            while parent and parent != self.dialog:
                self._owned(parent, child=True)
                if self.user.GetDlgCtrlID(parent) == 0x47C:
                    candidates.append(hwnd)
                    break
                parent = self.user.GetParent(parent)
            return True

        self.user.EnumChildWindows(self.dialog, inspect, 0)
        if len(candidates) != 1:
            self.evidence.append({"filename_control_inventory": inventory})
            raise AssertionError("UNIQUE_FILENAME_EDIT_NOT_FOUND")
        return candidates[0]

    def _send(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self._owned(self.dialog)
        self._owned(hwnd, child=hwnd != self.dialog)
        result = ctypes.c_size_t()
        if not self.user.SendMessageTimeoutW(
            hwnd, message, wparam, lparam, 0x1 | 0x2, 2000, ctypes.byref(result)
        ):
            raise AssertionError("OWNED_DIALOG_MESSAGE_TIMEOUT")
        return result.value

    def finish(self, selected: Path | None = None) -> None:
        wait_until(self.find, timeout=15)
        self._owned(self.dialog)
        event = {
            "operation": "cancel" if selected is None else "select",
            "owned_pid": self.pid,
        }
        if selected is not None:
            source = selected.resolve(strict=True)
            if not source.is_relative_to(
                ROOT / "output/playwright"
            ) or source.name not in {
                "synthetic-course.webm",
                "synthetic-relocated.webm",
            }:
                raise AssertionError("SYNTHETIC_SELECTION_SCOPE_DENIED")
            edit = self._filename_edit()
            value = ctypes.create_unicode_buffer(str(source))
            self._send(edit, 0x000C, 0, ctypes.cast(value, ctypes.c_void_p).value)
            # Read back only the field we have just overwritten with our own path.
            actual = ctypes.create_unicode_buffer(32768)
            self._send(
                edit, 0x000D, len(actual), ctypes.cast(actual, ctypes.c_void_p).value
            )
            if actual.value != str(source):
                raise AssertionError("FILENAME_FIELD_NOT_SYNTHETIC_PATH")
            event.update(
                filename_verified=True, control_class="Edit", filename=source.name
            )
        command = 2 if selected is None else 1
        button = self.user.GetDlgItem(self.dialog, command)
        if not button:
            raise AssertionError("OWNED_DIALOG_BUTTON_NOT_FOUND")
        self._owned(button, child=True)
        self._owned(self.dialog)
        if not self.user.PostMessageW(self.dialog, 0x0111, command, button):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = self.dialog
        wait_until(lambda: not self.user.IsWindow(hwnd), timeout=15)
        self.dialog = None
        self.evidence.append(event)

    def cancel_remaining(self) -> None:
        if self.kernel.WaitForSingleObject(self.handle, 0) == 258 and self.find():
            self.finish()

    def release(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def api(browser: Browser, path: str, body=None) -> dict:
    return browser.evaluate(
        "async()=>{const s=await(await fetch('/api/v1/session')).json();"
        "const r=await fetch('/api/v1'+" + json.dumps(path) + ","
        "{method:" + json.dumps("GET" if body is None else "POST") + ","
        "headers:{'Content-Type':'application/json','X-Learning-CSRF':s.csrf_token},body:"
        + ("undefined" if body is None else "JSON.stringify(" + json.dumps(body) + ")")
        + "});return {status:r.status,body:await r.json()};}"
    )


def click(browser: Browser, name: str) -> None:
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',"
        "{name:" + json.dumps(name) + ",exact:true}).click();}",
    )


def button_ready(browser: Browser, name: str) -> None:
    browser.command(
        "run-code",
        "async(page)=>{await page.waitForFunction(name=>"
        "Array.from(document.querySelectorAll('button')).some(b=>"
        "b.textContent.trim()===name&&!b.disabled)," + json.dumps(name) + ");}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default="native-picker-" + time.strftime("%Y%m%d-%H%M%S")
    )
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18870)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("该验证只适用于 Windows")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    if not port_closed(args.backend_port):
        parser.error("端口已占用，不接管其他进程")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        parser.error("固定 Chromium 缺失；不自动下载")
    spec = importlib.util.spec_from_file_location(
        "native_picker_synthetic_generator", ROOT / "tests/fixtures/mvp_a/generate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    manifest = fixture.generate(directory / "synthetic", ffmpeg=args.ffmpeg_path)
    source = Path(manifest["video"])
    app_paths = [
        "backend/src/learning_app/media.py",
        "frontend/src/media/VideoWorkspace.tsx",
    ]
    before = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in app_paths
    }
    instance = Instance(
        directory / "app", "start", args.backend_port, args.backend_port
    )
    browser = Browser(directory / "browser", executable)
    native = None
    result = {
        "run_id": args.run_id,
        "status": "failed",
        "real_provider_attempts": 0,
        "scope": "actual Windows native picker; synthetic media; own backend HWND only",
        "checks": {},
        "source_digests_before": before,
    }
    checks = result["checks"]
    try:
        instance.start()
        native = OwnedPicker(instance)
        browser.command("open", instance.url, "--config", str(browser.config))
        browser.wait_text("后台已连接")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('textbox',"
            "{name:'新课程名称'}).fill('合成原生选择验证');"
            "await page.getByRole('button',{name:'新建',exact:true}).click();}",
        )
        button_ready(browser, "选择本地视频")
        course = api(browser, "/courses")["body"][0]
        list_path = f"/courses/{course['course_id']}/videos"

        click(browser, "选择本地视频")
        wait_until(native.find, timeout=15)
        duplicate = api(browser, f"/courses/{course['course_id']}/videos/pick", {})
        checks["real_dialog_concurrent_pick_is_rejected"] = (
            duplicate["status"] == 409 and duplicate["body"]["code"] == "PICKER_ACTIVE"
        )
        native.finish()
        button_ready(browser, "选择本地视频")
        checks["native_cancel_adds_no_asset"] = api(browser, list_path)["body"] == []

        click(browser, "选择本地视频")
        native.finish(source)
        button_ready(browser, "选择本地视频")
        browser.command(
            "run-code",
            "async(page)=>{await page.waitForFunction(()=>"
            "document.querySelector('video')?.videoWidth===640);}",
        )
        assets = api(browser, list_path)["body"]
        asset = assets[0]
        checks["native_select_registers_one_available_synthetic_video"] = (
            len(assets) == 1
            and asset["availability"] == "available"
            and asset["source_version"] == manifest["files"][source.name]
        )

        # Navigation unloads the source before moving this runner's own fixture.
        browser.command("goto", instance.url)
        browser.wait_text("后台已连接")
        moved = source.with_name("synthetic-relocated.webm")
        source.rename(moved)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',"
            "{name:'选择课程'}).selectOption("
            + json.dumps(course["course_id"])
            + ");}",
        )
        relocation_button = "重新定位 " + asset["display_name"]
        button_ready(browser, relocation_button)
        click(browser, relocation_button)
        native.finish()
        button_ready(browser, relocation_button)
        missing = api(browser, list_path)["body"][0]
        checks["native_relocation_cancel_preserves_missing_source"] = (
            missing["asset_id"] == asset["asset_id"]
            and missing["availability"] == "missing"
        )
        click(browser, relocation_button)
        native.finish(moved)
        button_ready(browser, "选择本地视频")
        browser.command(
            "run-code",
            "async(page)=>{await page.waitForFunction(()=>"
            "document.querySelector('video')?.videoWidth===640);}",
        )
        relocated = api(browser, list_path)["body"][0]
        checks["native_relocation_keeps_identity_version_and_restores_playback"] = (
            relocated["asset_id"] == asset["asset_id"]
            and relocated["source_version"] == asset["source_version"]
            and relocated["availability"] == "available"
        )
        checks["all_window_actions_target_original_owned_backend"] = len(
            native.evidence
        ) == 4 and all(
            event.get("owned_pid") == native.pid for event in native.evidence
        )
        result["asset"] = {
            key: relocated[key] for key in ("asset_id", "source_version", "size_bytes")
        }
        if not all(checks.values()):
            raise AssertionError("原生选择断言存在失败")
        result["status"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if native:
            try:
                native.cancel_remaining()
            except Exception as exc:
                result["dialog_cleanup_error"] = f"{type(exc).__name__}: {exc}"
            result["native_actions"] = native.evidence
        result["browser_cleanup"] = browser.close()
        result["application_cleanup"] = instance.stop()
        if native:
            native.release()
        after = {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in app_paths
        }
        result["source_digests_after"] = after
        checks["application_sources_unchanged"] = before == after
        checks["owned_processes_and_port_closed"] = (
            result["application_cleanup"].get("backend_port_closed", False)
            and not result["application_cleanup"]
            .get("record", {})
            .get("cleanup", {})
            .get("remaining_pids")
            and not result["browser_cleanup"].get("remaining_pids")
        )
        if not all(checks.values()):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": checks,
                "error": result.get("error"),
                "evidence": str(directory / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
