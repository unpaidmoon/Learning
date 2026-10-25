"""Actual isolated Windows package, Electron carrier and owned-backend recovery."""

import argparse
import base64
import ctypes
import http.cookiejar
import json
import io
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from websockets.sync.client import connect

from learning_app.processes import OwnedProcessGroup

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def wait(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(0.1)
    raise TimeoutError(
        "PORTABLE_EXPECTED_STATE_NOT_OBSERVED"
        + (":" + last_error if last_error else "")
    )


def kill_owned_backend(group, pid, candidate):
    """Disk identity alone never authorizes termination: live private job first."""
    from ctypes import wintypes

    if pid not in group.pids():
        raise RuntimeError("BACKEND_NOT_IN_OWNED_JOB")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    handle = kernel.OpenProcess(0x1000 | 0x0001, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        text = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(text))
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        executable = Path(text.value).resolve()
        if pid not in group.pids() or executable not in {
            candidate / "runtime/python/python.exe",
            candidate / "runtime/python/pythonw.exe",
        }:
            raise RuntimeError("OWNED_BACKEND_EXECUTABLE_MISMATCH")
        if not kernel.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def run(candidate, output):
    sys.path.insert(0, str(candidate / "scripts/desktop"))
    from check_shell import Debugger, debugger_targets
    from portable import verify_release

    manifest = verify_release(candidate)
    result = {
        "status": "running",
        "assertions": [],
        "release_id": manifest["release_id"],
        "layer": "actual isolated portable Python + Rust entry + Electron + pinned Host + backend death",
        "model_requests": 0,
        "startup_os_writes": 0,
        "user_learning_experience": "not_run",
    }
    group = OwnedProcessGroup("portable-recovery-validation")
    port, debug_port = free_port(), free_port()
    profile = output / "owned-profile"
    result.update(owned_profile=str(profile), backend_port=port)
    origin = f"http://127.0.0.1:{port}"
    opener = build_opener(
        ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    environment = dict(os.environ)
    for key in list(environment):
        if key.upper().startswith(
            ("LEARNING_APP_", "LEARNING_DESKTOP_")
        ) or key.upper() in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "NODE_OPTIONS",
            "ELECTRON_RUN_AS_NODE",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_AUTH_TOKEN",
        }:
            environment.pop(key, None)
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")

    def check(condition, name):
        result["assertions"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def request(path, value=None):
        headers = {}
        if value is not None:
            with opener.open(origin + "/api/v1/session", timeout=5) as response:
                csrf = json.load(response)["csrf_token"]
            headers = {
                "Content-Type": "application/json",
                "Origin": origin,
                "X-Learning-CSRF": csrf,
            }
        data = json.dumps(value).encode() if value is not None else None
        with opener.open(
            Request(origin + "/api/v1" + path, data=data, headers=headers), timeout=10
        ) as response:
            return json.load(response)

    try:
        launcher = group.spawn(
            [
                str(candidate / "启动学习.exe"),
                "--profile-dir",
                str(profile),
                "--backend-port",
                str(port),
                "--debug-port",
                str(debug_port),
                "--hidden",
            ],
            cwd=output,
            env=environment,
        )
        launcher.wait(timeout=8)
        check(
            launcher.returncode == 0,
            "native_entry_runs_with_only_system32_in_child_path",
        )
        wait(lambda: request("/health")["ready"], timeout=150)
        first = request("/health")
        materials = output / "synthetic-course"
        materials.mkdir()
        (materials / "lesson.txt").write_text(
            "Self-created portable recovery fixture.", "utf-8"
        )
        video = materials / "示例视频.webm"
        subprocess.run(
            [
                str(candidate / ".tools/ffmpeg/ffmpeg.exe"),
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x008800:s=640x360:r=2:d=4",
                "-threads",
                "1",
                "-c:v",
                "libvpx",
                str(video),
            ],
            check=True,
            capture_output=True,
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=environment,
        )
        course = request(
            "/courses/folder", {"title": "独立运行恢复验证", "path": str(materials)}
        )
        mapping = request("/desktop/courses/" + course["course_id"] + "/open", {})
        draft_route = "/chats/" + mapping["chat_id"] + "/draft"
        check(True, "owned_synthetic_course_is_bound_before_composer_interaction")
        opened = request(
            "/courses/" + course["course_id"] + "/files/open", {"path": video.name}
        )
        asset = opened["video"]
        preview_route = (
            "/videos/"
            + asset["asset_id"]
            + "/previews?source_version="
            + asset["source_version"]
        )
        wait(lambda: request(preview_route)["status"] == "ready", timeout=60)
        previews = request(preview_route)
        from PIL import Image

        with opener.open(origin + previews["frames"][0]["url"], timeout=10) as response:
            raw_frame = response.read()
        with Image.open(io.BytesIO(raw_frame)) as frame:
            green = frame.convert("RGB").getpixel((120, 67))
            check(
                frame.size == (240, 135)
                and frame.format == "JPEG"
                and green[1] > green[0] + 50
                and green[1] > green[2] + 50,
                "packaged_fixed_ffmpeg_builds_the_actual_synthetic_video_preview",
            )
        wait(
            lambda: (profile / "launcher.log").is_file()
            and "LEARNING_DESKTOP_READY "
            in (profile / "launcher.log").read_text("utf-8")
        )
        targets = debugger_targets(debug_port)
        target = next(
            row
            for row in targets
            if row["type"] == "page" and row["url"].startswith(origin)
        )
        with connect(target["webSocketDebuggerUrl"]) as stream:
            page = Debugger(stream)
            page.command("Emulation.setFocusEmulationEnabled", {"enabled": True})
            page.command("Page.reload", {"ignoreCache": True})
            wait(
                lambda: page.evaluate(
                    "Boolean(document.querySelector('[data-composer-input]')?.isContentEditable)"
                )
            )
            check(
                page.evaluate(
                    "typeof require==='undefined'&&typeof process==='undefined'"
                ),
                "actual_renderer_has_no_node_api",
            )
            page.command("Page.bringToFront")
            page.evaluate(
                "window.__portableMounted='same-page';document.querySelector('[data-composer-input]').focus()"
            )
            (output / "composer-focus.json").write_text(
                json.dumps(
                    page.evaluate(
                        "({active:document.activeElement?.outerHTML,composer:document.querySelector('[data-composer-input]')?.outerHTML,focused:document.hasFocus(),visibility:document.visibilityState})"
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                "utf-8",
            )
            wait(
                lambda: page.evaluate(
                    "document.activeElement===document.querySelector('[data-composer-input]')"
                ),
                timeout=10,
            )
            page.command("Input.insertText", {"text": "UNSENT_PORTABLE_RECOVERY_DRAFT"})
            wait(
                lambda: page.evaluate(
                    "document.querySelector('[data-composer-input]').innerText.includes('UNSENT_PORTABLE_RECOVERY_DRAFT')"
                ),
                timeout=10,
            )
            check(
                page.evaluate(
                    "document.querySelector('[data-composer-input]').innerText.includes('UNSENT_PORTABLE_RECOVERY_DRAFT')"
                ),
                "native_composer_contains_unsent_owned_draft",
            )
            wait(
                lambda: request(draft_route)["text"] == "UNSENT_PORTABLE_RECOVERY_DRAFT"
            )
            saved_before = request(draft_route)
            initial_pids = group.pids()
            record = json.loads(
                (profile / "run/apps" / first["instance_id"] / "run.json").read_text(
                    "utf-8"
                )
            )
            kill_owned_backend(group, record["backend_pid"], candidate)
            page.command("Input.insertText", {"text": "_DURING_OUTAGE"})
            outage_text = "UNSENT_PORTABLE_RECOVERY_DRAFT_DURING_OUTAGE"
            wait(
                lambda: page.evaluate(
                    "document.querySelector('[data-composer-input]').innerText.trim()=== "
                    + json.dumps(outage_text)
                )
            )
            wait(
                lambda: (health := request("/health"))["ready"]
                and health["instance_id"] != first["instance_id"],
                timeout=150,
            )
            recovered = request("/health")
            request(
                "/session"
            )  # The independent HTTP observer also needs a new signed cookie.
            wait(
                lambda: json.loads(
                    (
                        profile / "run/apps" / recovered["instance_id"] / "run.json"
                    ).read_text("utf-8")
                )["status"]
                == "ready"
            )
            check(
                page.evaluate(
                    "window.__portableMounted==='same-page'&&document.querySelector('[data-composer-input]').innerText.trim()==="
                    + json.dumps(outage_text)
                ),
                "same_electron_document_and_unsent_draft_survive_backend_death",
            )
            try:
                wait(
                    lambda: page.evaluate(
                        "!Array.from(document.querySelectorAll('.learning-library-error,[role=alert]')).some(e=>/无法连接后台|DRAFT_NOT_READY|草稿尚未接回|草稿保存失败/.test(e.textContent))"
                    )
                )
            except TimeoutError:
                result["remaining_connection_errors"] = page.evaluate(
                    "Array.from(document.querySelectorAll('.learning-library-error,[role=alert]')).map(e=>({className:e.className,text:e.textContent}))"
                )
                raise
            check(True, "recovered_library_refresh_clears_obsolete_connection_error")
            check(
                request(draft_route) == saved_before,
                "reconnection_reads_original_draft_without_replaying_the_outage_save",
            )
            page.evaluate(
                "(()=>{const e=document.querySelector('[data-composer-input]');e.focus();const s=getSelection();s.selectAllChildren(e);s.collapseToEnd();})()"
            )
            page.command("Input.insertText", {"text": "_AFTER_RECOVERY"})
            wait(
                lambda: request(draft_route)["text"] == outage_text + "_AFTER_RECOVERY"
            )
            check(
                True,
                "new_native_edit_after_recovery_is_persisted_by_the_actual_package",
            )
            # Native settings perform their own browser write with the refreshed
            # application CSRF; the Python observer does not submit this change.
            page.evaluate(
                "Array.from(document.querySelectorAll('button')).find(e=>e.textContent.trim()==='设置').click()"
            )
            wait(
                lambda: page.evaluate(
                    "Array.from(document.querySelectorAll('[role=menuitem]')).some(e=>e.textContent.includes('运行设置'))"
                )
            )
            page.evaluate(
                "Array.from(document.querySelectorAll('[role=menuitem]')).find(e=>e.textContent.includes('运行设置')).click()"
            )
            wait(
                lambda: page.evaluate(
                    "Boolean(document.querySelector('select[aria-label=\"本地处理资源\"]'))"
                )
            )
            page.evaluate(
                "(()=>{const e=document.querySelector('select[aria-label=\"本地处理资源\"]');e.value='accelerated';e.dispatchEvent(new Event('change',{bubbles:true}));})()"
            )
            wait(
                lambda: request("/runtime/preferences")["effective"]["resource_mode"]
                == "accelerated"
            )
            check(
                True, "actual_native_settings_write_succeeds_after_instance_csrf_change"
            )
            screenshot = page.command("Page.captureScreenshot", {"format": "png"})
            (output / "portable-recovered-settings.png").write_bytes(
                base64.b64decode(screenshot["data"])
            )
            check(
                set(initial_pids) & set(group.pids()) != set(),
                "original_desktop_carrier_remains_alive",
            )
        request(
            "/runtime/exit",
            {"instance_id": recovered["instance_id"], "confirm_active": False},
        )
        wait(lambda: not group.pids(), timeout=50)
        check(
            True,
            "recovered_backend_and_original_carrier_exit_naturally_with_no_owned_process_left",
        )
        records = [
            json.loads(path.read_text("utf-8"))
            for path in (profile / "run/apps").glob("*/run.json")
        ]
        check(
            len(records) == 2
            and all(row["cleanup"]["remaining_pids"] == [] for row in records),
            "exactly_two_backend_records_and_both_old_groups_are_clean",
        )
        ledger = profile / "data/execution/boundary/boundary-ledger.jsonl"
        entries = (
            [json.loads(line) for line in ledger.read_text("utf-8").splitlines()]
            if ledger.exists()
            else []
        )
        check(
            not any(
                row["event"] in {"request_received", "dispatch_started"}
                for row in entries
            ),
            "no_model_submission_from_recovery_draft_or_settings",
        )
        check(
            verify_release(candidate) == manifest,
            "candidate_manifest_files_unchanged_after_actual_run",
        )
        result.update(
            status="passed",
            instance_records=[
                {
                    key: row.get(key)
                    for key in ["instance_id", "recovery_parent", "status", "cleanup"]
                }
                for row in records
            ],
        )
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        result["cleanup"] = group.close(grace_seconds=8)
        (output / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        raise RuntimeError("OWNED_RUN_ID_INVALID")
    directory = (args.output_root or ROOT / ".runtime/validation").resolve() / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    value = run(args.candidate.resolve(), directory)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "error": value.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
