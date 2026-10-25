"""Exercise HTML fullscreen in a separate real Electron synthetic window."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import time
import uuid

from websockets.sync.client import connect

from bootstrap import ROOT, verified_executable
from check_shell import Debugger, PageFixture, debugger_targets, free_port
from learning_app.processes import OwnedProcessGroup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        parser.error("safe run id required")
    directory = ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PageFixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome_port, inspector_port = free_port(), free_port()
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "DEEPSEEK_API_KEY",
                                  "LEARNING_DESKTOP_OWNER_FILE"}}
    environment.update(
        LEARNING_DESKTOP_URL=f"http://127.0.0.1:{server.server_port}/desktop/",
        LEARNING_DESKTOP_OWNS_BACKEND="0", LEARNING_DESKTOP_TEST_HIDDEN="0",
        LEARNING_DESKTOP_USER_DATA=str(directory / ("browser-" + uuid.uuid4().hex)),
    )
    group = OwnedProcessGroup("learning-fullscreen-check")
    result = {"run_id": args.run_id, "status": "failed", "model_calls": 0,
              "scope": "real Electron synthetic main frame fullscreen permission and exit"}
    output = (directory / "electron.log").open("w", encoding="utf-8")
    process = group.spawn([
        str(verified_executable()), f"--inspect=127.0.0.1:{inspector_port}",
        f"--remote-debugging-port={chrome_port}", str(ROOT / "desktop"),
    ], cwd=ROOT, env=environment, stdout=output, stderr=output)
    try:
        native_target = debugger_targets(inspector_port)[0]
        page_target = next(row for row in debugger_targets(chrome_port) if row["type"] == "page")
        with connect(native_target["webSocketDebuggerUrl"]) as native_socket, connect(page_target["webSocketDebuggerUrl"]) as page_socket:
            native, page = Debugger(native_socket), Debugger(page_socket)
            deadline = time.monotonic() + 15
            while not page.evaluate("Boolean(document.querySelector('h1'))"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("FULLSCREEN_SYNTHETIC_PAGE_NOT_READY")
                time.sleep(.05)
            before = native.evaluate("(()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];return {bounds:w.getBounds(),fullscreen:w.isFullScreen(),visible:w.isVisible()}})()")
            native.evaluate("(()=>{global.__fullscreenEvents=[];const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];w.webContents.on('enter-html-full-screen',()=>global.__fullscreenEvents.push('enter'));w.webContents.on('leave-html-full-screen',()=>global.__fullscreenEvents.push('leave'));return true})()")
            page.command("Runtime.evaluate", {
                "expression": "window.__fullscreenResult={pending:true};document.documentElement.requestFullscreen().then(()=>{window.__fullscreenResult={allowed:true,element:document.fullscreenElement===document.documentElement}}).catch(error=>{window.__fullscreenResult={allowed:false,name:error.name,message:error.message}});'dispatched'",
                "userGesture": True, "returnByValue": True,
            })
            deadline = time.monotonic() + 3
            admission = page.evaluate("window.__fullscreenResult")
            while admission.get("pending") and time.monotonic() < deadline:
                time.sleep(.05)
                admission = page.evaluate("window.__fullscreenResult")
            result["before"] = before
            result["request"] = admission
            deadline = time.monotonic() + 5
            entered = False
            while time.monotonic() < deadline:
                entered = native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isFullScreen()")
                if entered or not admission.get("allowed"):
                    break
                time.sleep(.05)
            result["native_entered_fullscreen"] = entered
            if admission.get("allowed"):
                page.command("Runtime.evaluate", {"expression": "document.exitFullscreen()", "awaitPromise": True})
                deadline = time.monotonic() + 5
                while native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isFullScreen()"):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("FULLSCREEN_EXIT_TIMEOUT")
                    time.sleep(.05)
                result["after"] = native.evaluate("(()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];return {bounds:w.getBounds(),fullscreen:w.isFullScreen(),visible:w.isVisible()}})()")
                result["dom_exited_fullscreen"] = page.evaluate("document.fullscreenElement===null")
                result["native_fullscreen_events"] = native.evaluate("global.__fullscreenEvents")
                page.evaluate("(()=>{const f=document.createElement('iframe');f.id='synthetic-frame';f.allowFullscreen=true;f.srcdoc='<p>Synthetic same-origin frame</p>';document.body.append(f);return true})()")
                deadline = time.monotonic() + 5
                while not page.evaluate("Boolean(document.getElementById('synthetic-frame').contentDocument?.querySelector('p'))"):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("FULLSCREEN_SYNTHETIC_FRAME_NOT_READY")
                    time.sleep(.05)
                page.command("Runtime.evaluate", {
                    "expression": "window.__frameResult={pending:true};document.getElementById('synthetic-frame').contentDocument.documentElement.requestFullscreen().then(()=>{window.__frameResult={allowed:true}}).catch(e=>{window.__frameResult={allowed:false,message:e.message}});'dispatched'",
                    "userGesture": True, "returnByValue": True,
                })
                time.sleep(.3)
                result["iframe_request"] = page.evaluate("window.__frameResult")
                result["iframe_fullscreen_denied"] = (
                    not result["iframe_request"].get("allowed")
                    and page.evaluate("document.fullscreenElement===null")
                    and not native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isFullScreen()")
                )
            if (admission.get("allowed") and admission.get("element") and entered
                    and result.get("dom_exited_fullscreen")
                    and result.get("iframe_fullscreen_denied")
                    and result["after"]["bounds"] == before["bounds"]):
                result["status"] = "passed"
            try:
                native.evaluate("process.mainModule.require('electron').app.quit()")
            except Exception:
                pass
        process.wait(timeout=15)
        result["electron_exit_code"] = process.returncode
    finally:
        result["cleanup"] = group.close()
        server.shutdown()
        server.server_close()
        output.close()
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
