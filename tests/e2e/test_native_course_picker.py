"""Real Windows folder dialog and formal course API, synthetic folders only.

Uses messages confined to the live, owned backend HWND. No global input,
clipboard, screenshots or directory-list inspection. No Harness/model requests.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from test_browser import ROOT, Instance, port_closed, wait_until, write_json
from test_native_video_picker import OwnedPicker


class CoursePicker(OwnedPicker):
    TITLE = "选择课程根文件夹"

    def __init__(self, instance, scope: Path):
        super().__init__(instance)
        self.scope = scope.resolve(strict=True)

    def _filename_edit(self) -> int:
        candidates = []
        inventory = []

        @self.callback_type
        def inspect(hwnd, _):
            self._owned(hwnd, child=True)
            control_id = self.user.GetDlgCtrlID(hwnd)
            class_name = self._class(hwnd)
            inventory.append({"class": class_name, "id": control_id})
            if class_name == "Edit" and control_id in {1001, 0x480}:
                candidates.append(hwnd)
            return True

        self.user.EnumChildWindows(self.dialog, inspect, 0)
        if len(candidates) != 1:
            self.evidence.append({"owned_control_inventory": inventory})
            raise AssertionError("UNIQUE_FOLDER_EDIT_NOT_FOUND")
        return candidates[0]

    def finish(self, selected: Path | None = None) -> None:
        self._owned(self.dialog)
        if selected is not None:
            source = selected.resolve(strict=True)
            if not source.is_dir() or not source.is_relative_to(self.scope):
                raise AssertionError("SYNTHETIC_FOLDER_SCOPE_DENIED")
            edit = self._filename_edit()
            text = ctypes.create_unicode_buffer(str(source))
            self._send(edit, 0x000C, 0, ctypes.cast(text, ctypes.c_void_p).value)
            actual = ctypes.create_unicode_buffer(32768)
            self._send(
                edit, 0x000D, len(actual), ctypes.cast(actual, ctypes.c_void_p).value
            )
            if actual.value != str(source):
                raise AssertionError("FOLDER_EDIT_DID_NOT_ACCEPT_SYNTHETIC_PATH")
        command = 2 if selected is None else 1
        hwnd = self.dialog
        for _ in range(3):
            self._owned(hwnd)
            button = self.user.GetDlgItem(hwnd, command)
            if not button:
                raise AssertionError("OWNED_FOLDER_DIALOG_BUTTON_NOT_FOUND")
            self._owned(button, child=True)
            if not self.user.PostMessageW(hwnd, 0x0111, command, button):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 2
            while self.user.IsWindow(hwnd) and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.user.IsWindow(hwnd):
                break
        if self.user.IsWindow(hwnd):
            raise AssertionError("OWNED_FOLDER_DIALOG_DID_NOT_CLOSE")
        self.evidence.append(
            {
                "operation": "cancel" if selected is None else "select",
                "owned_pid": self.pid,
                "synthetic_only": True,
            }
        )
        self.dialog = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19176)
    args = parser.parse_args()
    if os.name != "nt" or not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("Windows and a safe run-id are required")
    if not port_closed(args.backend_port):
        parser.error("Port occupied; no process will be taken over")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    synthetic = directory / "synthetic"
    synthetic.mkdir()
    folder = synthetic / "原生选目录 中文 空格"
    folder.mkdir()
    (folder / "synthetic.txt").write_text("SYNTHETIC ONLY", encoding="utf-8")
    implementation = [
        ROOT / "backend/src/learning_app" / name
        for name in ["course_files.py", "native_files.py"]
    ]

    def digests():
        return {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in implementation
        }

    before = digests()
    instance = Instance(
        directory / "app", "start", args.backend_port, args.backend_port
    )
    pool = ThreadPoolExecutor(max_workers=1)
    native = None
    checks = {}
    result = {
        "run_id": args.run_id,
        "date": "2026-10-22",
        "status": "failed",
        "scope": "real Windows dialog + formal HTTP worker; no browser or model",
        "real_model_attempts": 0,
        "checks": checks,
        "source_before": before,
    }
    try:
        instance.start()
        native = CoursePicker(instance, synthetic)
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

        def api(path, body=None):
            request = Request(
                instance.url + "/api/v1" + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", "X-Learning-CSRF": csrf}
                if body is not None
                else {},
            )
            try:
                with opener.open(request, timeout=25) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)

        csrf = api("/session")[1]["csrf_token"]

        def open_dialog():
            pending = pool.submit(api, "/courses/folder/pick", {})

            def visible():
                if native.find():
                    return True
                if pending.done():
                    reply = pending.result()
                    result["premature_picker_reply"] = reply
                    raise AssertionError(f"Expected real folder window; got {reply}")
                return False

            wait_until(visible, timeout=10)
            return pending

        pending = open_dialog()
        checks["real_folder_dialog_visible"] = True
        duplicate = api("/courses/folder/pick", {})
        checks["concurrent_pick_is_rejected"] = (
            duplicate[0] == 409 and duplicate[1]["code"] == "PICKER_ACTIVE"
        )
        native.finish()
        checks["cancel_returns_null_and_creates_no_course"] = (
            pending.result() == (200, None) and api("/courses")[1] == []
        )
        pending = open_dialog()
        native.finish(folder)
        status, selected = pending.result()
        checks["actual_unicode_folder_selected"] = (
            status == 200
            and Path(selected["path"]) == folder
            and selected["name"] == folder.name
        )
        checks["selection_alone_creates_no_course"] = api("/courses")[1] == []
        status, course = api(
            "/courses/folder", {"path": selected["path"], "title": "原生选择后创建"}
        )
        checks["confirm_creates_course_without_renaming_folder"] = (
            status == 200
            and course["title"] == "原生选择后创建"
            and folder.is_dir()
            and len(api("/courses")[1]) == 1
        )
        pending = open_dialog()
        native.finish()
        checks["reopen_and_cancel_after_selection_works"] = pending.result() == (
            200,
            None,
        )
        checks["only_original_owned_windows_controlled"] = len(
            native.evidence
        ) == 3 and all(
            event.get("owned_pid") == native.pid for event in native.evidence
        )
        result["status"] = "passed" if all(checks.values()) else "failed"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if native:
            try:
                native.cancel_remaining()
            except Exception as exc:
                result["dialog_cleanup_error"] = type(exc).__name__
            result["native_actions"] = native.evidence
            native.release()
        result["application_cleanup"] = instance.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        result["source_after"] = digests()
        checks["application_sources_unchanged"] = before == result["source_after"]
        cleanup = result["application_cleanup"]
        checks["owned_processes_and_port_closed"] = cleanup.get(
            "backend_port_closed", False
        ) and not cleanup.get("record", {}).get("cleanup", {}).get("remaining_pids")
        if not all(checks.values()):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": checks,
                "error": result.get("error"),
                "result": str(directory / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
