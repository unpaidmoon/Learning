"""Real migrated Harness page: native picker, theme/caption, fullscreen.

Owns isolated fixture data and Electron processes; never opens user materials
or forwards model requests. Native picker interaction is confined to owned HWNDs.
"""

from __future__ import annotations

import argparse
import asyncio
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from websockets.sync.client import connect

from bootstrap import ROOT, verified_executable
from check_shell import Debugger, debugger_targets, free_port
from learning_app.processes import OwnedProcessGroup

sys.path.insert(0, str(ROOT / "tests/e2e"))
from test_desktop import serve_fixture  # noqa: E402
from test_native_course_picker import CoursePicker  # noqa: E402


def wait_until(operation, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = operation()
            if value:
                return value
        except (OSError, ConnectionError):
            pass
        time.sleep(.05)
    raise RuntimeError("MIGRATED_SHELL_CHECK_TIMEOUT")


def click_text(page, text):
    return page.evaluate("(()=>{const e=[...document.querySelectorAll('button,[role=menuitem]')]"
                         ".find(e=>e.textContent.trim()===" + json.dumps(text) + "&&e.getClientRects().length);"
                         "if(!e)return false;e.click();return true})()")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        return asyncio.run(serve_fixture(args.serve, args.port))
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        parser.error("safe run id required")
    directory = ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    folder = directory / "synthetic course 中文"
    folder.mkdir()
    (folder / "synthetic.txt").write_text("SYNTHETIC NATIVE PICKER ONLY", encoding="utf-8")
    port, chrome_port, inspector_port = free_port(), free_port(), free_port()
    env = {key: value for key, value in os.environ.items()
           if key not in {"DEEPSEEK_API_KEY", "ELECTRON_RUN_AS_NODE", "NODE_OPTIONS"}}
    env.update(
        LEARNING_DESKTOP_URL=f"http://127.0.0.1:{port}/desktop/", LEARNING_DESKTOP_OWNS_BACKEND="0",
        LEARNING_DESKTOP_TEST_HIDDEN="0", LEARNING_DESKTOP_USER_DATA=str(directory / "browser"),
        LEARNING_DESKTOP_OWNER_FILE=str(directory / "native-owner.json"), PYTHONUNBUFFERED="1",
    )
    group = OwnedProcessGroup("learning-migrated-shell-check")
    output = (directory / "process.log").open("w", encoding="utf-8")
    backend = group.spawn([
        sys.executable, str(Path(__file__).resolve()), "--run-id", args.run_id,
        "--serve", str(directory), "--port", str(port),
    ], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    opener = build_opener(ProxyHandler({}))
    def healthy():
        with opener.open(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as response:
            value = json.load(response)
        return value["ready"] and value["capabilities"]["harness"] == "ready"
    result = {"run_id": args.run_id, "status": "failed", "real_model_calls": 0,
              "scope": "actual Harness client in real Electron with isolated formal backend and fixture provider"}
    picker = None
    try:
        wait_until(healthy, 45)
        electron = group.spawn([
            str(verified_executable()), f"--inspect=127.0.0.1:{inspector_port}",
            f"--remote-debugging-port={chrome_port}", str(ROOT / "desktop"),
        ], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        native_target = debugger_targets(inspector_port)[0]
        page_target = next(row for row in debugger_targets(chrome_port) if row["type"] == "page")
        instance = type("OwnedBackend", (), {"record": {"backend_pid": backend.pid, "owned_pids": group.pids()}})()
        picker = CoursePicker(instance, directory)
        picker.user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        picker.user.GetWindow.restype = wintypes.HWND
        picker.user.GetForegroundWindow.restype = wintypes.HWND
        with connect(native_target["webSocketDebuggerUrl"], ping_interval=None) as native_socket, connect(page_target["webSocketDebuggerUrl"], ping_interval=None) as page_socket:
            native, page = Debugger(native_socket), Debugger(page_socket)
            wait_until(lambda: page.evaluate("Boolean(document.querySelector('[data-testid=learning-course-directory]'))"), 45)
            # Empty fixture opens the project course picker; its Create action
            # reveals the native-folder gesture and never sends a model prompt.
            if not click_text(page, "选择课程文件夹"):
                assert click_text(page, "创建课程")
                wait_until(lambda: page.evaluate("[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='选择课程文件夹')"))
            native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].minimize()")
            assert click_text(page, "选择课程文件夹")
            wait_until(picker.find)
            owner = json.loads((directory / "native-owner.json").read_text("utf-8"))
            assert picker.user.GetWindow(picker.dialog, 4) == int(owner["hwnd"])
            assert picker.user.GetForegroundWindow() == picker.dialog
            result["native_picker"] = {"owner_matches": True, "dialog_foreground": True,
                "restored": native.evaluate("!process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isMinimized()")}
            picker.finish(folder)
            wait_until(lambda: page.evaluate("document.body.innerText.includes(" + json.dumps(str(folder)) + ")"))
            # The course form was populated only by the real picker callback.
            assert click_text(page, "创建课程")
            wait_until(lambda: page.evaluate("!document.querySelector('[role=dialog]')"))
            result["native_picker"]["synthetic_course_created"] = True
            native.evaluate("(()=>{global.__overlay=[];const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0],f=w.setTitleBarOverlay.bind(w);w.setTitleBarOverlay=v=>{global.__overlay.push(v);return f(v)};return true})()")
            theme_results = []
            for label, is_dark in (("浅色", False), ("深色", True), ("浅色", False)):
                previous_count = native.evaluate("global.__overlay.length")
                assert page.evaluate("(()=>{const b=document.querySelector('button[aria-label=\"设置\"]');if(!b)return false;b.click();return true})()")
                wait_until(lambda: click_text(page, label))
                wait_until(lambda: page.evaluate("document.body.hasAttribute('data-ds-dark-theme')") == is_dark)
                style = page.evaluate("(()=>{const p=[...document.querySelectorAll('span')].find(e=>e.style.visibility==='hidden'&&e.style.pointerEvents==='none'),s=p&&getComputedStyle(p);return {dark:document.body.hasAttribute('data-ds-dark-theme'),background:getComputedStyle(document.body).backgroundColor,captionHeight:getComputedStyle(document.documentElement).getPropertyValue('--dsh-windows-titlebar-height').trim(),probe:s?{color:s.color,background:s.backgroundColor}:null}})()")
                result["theme_pending"] = style
                if theme_results:
                    wait_until(lambda: native.evaluate("global.__overlay.length") > previous_count, 3)
                style["native_caption"] = native.evaluate("global.__overlay.at(-1)")
                assert style["captionHeight"] == "40px"
                theme_results.append(style)
            assert theme_results[0]["background"] != theme_results[1]["background"]
            assert theme_results[1]["native_caption"]["color"] != theme_results[2]["native_caption"]["color"]
            result["theme"] = theme_results
            before = native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].getBounds()")
            page.command("Runtime.evaluate", {"expression": "window.__full={pending:true};document.querySelector('[data-testid=learning-materials]').requestFullscreen().then(()=>window.__full={entered:!!document.fullscreenElement}).catch(e=>window.__full={error:e.message});'started'", "userGesture": True})
            wait_until(lambda: page.evaluate("window.__full?.entered"))
            wait_until(lambda: native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isFullScreen()"))
            page.command("Runtime.evaluate", {"expression": "document.exitFullscreen()", "awaitPromise": True})
            wait_until(lambda: not native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isFullScreen()"))
            after = native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].getBounds()")
            assert before == after and page.evaluate("document.fullscreenElement===null")
            result["fullscreen"] = {"dom_entered": True, "native_entered": True, "restored_bounds": True}
            try:
                native.evaluate("process.mainModule.require('electron').app.quit()")
            except Exception:
                pass
        electron.wait(timeout=15)
        backend.stdin.write("shutdown\n")
        backend.stdin.flush()
        backend.wait(timeout=20)
        ledger = directory / "execution/boundary/boundary-ledger.jsonl"
        result["provider_ledger_rows"] = len(ledger.read_text("utf-8").splitlines()) if ledger.is_file() else 0
        assert result["provider_ledger_rows"] == 0
        result.update(status="passed", electron_exit_code=electron.returncode)
    except Exception as error:
        result["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if picker:
            if picker.dialog and picker.user.IsWindow(picker.dialog):
                picker.finish()
            picker.kernel.CloseHandle(picker.handle)
        result["cleanup"] = group.close()
        output.close()
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
