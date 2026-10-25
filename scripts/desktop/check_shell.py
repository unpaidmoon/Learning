"""Check the actual Electron carrier with a synthetic page or local UI fixture.

The temporary inspector endpoints bind loopback and disappear with this owned
test process. Normal desktop launches never enable them. No provider is called.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid

from websockets.sync.client import connect

from bootstrap import ROOT, verified_executable
from learning_app.processes import OwnedProcessGroup


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PageFixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        content = (
            b"<html><title>Synthetic fixture</title><style>:root{"
            b"--dsw-specific-sidebar-fill:#f9fafb;--dsw-alias-label-primary:#0f1115}"
            b"</style><body><h1>Desktop carrier fixture</h1></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(content)


def debugger_targets(port: int) -> list:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                targets = json.load(response)
            if targets:
                return targets
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("DESKTOP_TEST_DEBUGGER_TIMEOUT")


class Debugger:
    def __init__(self, socket) -> None:
        self.socket = socket
        self.sequence = 0

    def command(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        while True:
            value = json.loads(self.socket.recv(timeout=15))
            if value.get("id") == self.sequence:
                if "error" in value:
                    raise RuntimeError(value["error"])
                return value.get("result", {})

    def evaluate(self, expression: str):
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")


def main() -> int:
    parser = argparse.ArgumentParser(description="使用合成页面或已运行的本地fixture核验真实Electron宿主")
    parser.add_argument("--url", help="只连接合成测试应用的 /desktop/，不管理该后台")
    parser.add_argument("--screenshot", type=Path, help="保存本次合成页面截图")
    parser.add_argument("--visible", action="store_true", help="验收时显示本次所属窗口")
    parser.add_argument("--open-synthetic-video", action="store_true", help="点击合成fixture目录中的 synthetic-course.webm，禁止用于用户课程")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/desktop/shell-verification.json")
    args = parser.parse_args()
    server = None
    if args.url:
        target = urlsplit(args.url)
        if target.scheme != "http" or target.hostname != "127.0.0.1" or target.path != "/desktop/" or target.query or target.fragment:
            parser.error("--url 仅接受本地合成fixture /desktop/ 地址")
        url = args.url
    else:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PageFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/desktop/"
    chrome_port, inspector_port = free_port(), free_port()
    environment = {key: value for key, value in os.environ.items() if key not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "DEEPSEEK_API_KEY"}}
    environment.update(
        LEARNING_DESKTOP_URL=url,
        LEARNING_DESKTOP_OWNS_BACKEND="0",
        LEARNING_DESKTOP_TEST_HIDDEN="0" if args.visible else "1",
        LEARNING_DESKTOP_USER_DATA=str(ROOT / ".runtime/desktop/shell-checks" / uuid.uuid4().hex),
    )
    group = OwnedProcessGroup("learning-desktop-shell-check")
    process = group.spawn(
        [str(verified_executable()), f"--inspect=127.0.0.1:{inspector_port}", f"--remote-debugging-port={chrome_port}", str(ROOT / "desktop")],
        cwd=ROOT, env=environment,
    )
    result = {}
    try:
        native_target = debugger_targets(inspector_port)[0]
        page_target = next(row for row in debugger_targets(chrome_port) if row["type"] == "page")
        with connect(native_target["webSocketDebuggerUrl"]) as native_socket, connect(page_target["webSocketDebuggerUrl"]) as page_socket:
            native, page = Debugger(native_socket), Debugger(page_socket)
            ready_selector = '[contenteditable="true"]' if args.url else "h1"
            deadline = time.monotonic() + 45
            while not page.evaluate(f"Boolean(document.querySelector({json.dumps(ready_selector)}))"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("DESKTOP_TEST_PAGE_NOT_READY: " + str(page.evaluate("document.body.innerText.slice(0,600)")))
                time.sleep(0.05)
            geometry = native.evaluate("(()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0],p=w.webContents.getLastWebPreferences();return {bounds:w.getBounds(),content_bounds:w.getContentBounds(),title:w.getTitle(),contextIsolation:p.contextIsolation,nodeIntegration:p.nodeIntegration,sandbox:p.sandbox,webSecurity:p.webSecurity,visible:w.isVisible(),id:w.id}})()")
            security = page.evaluate("({node_absent:typeof require==='undefined'&&typeof process==='undefined',ipc_absent:typeof window.ipcRenderer==='undefined',caption:document.documentElement.hasAttribute('data-windows-titlebar'),caption_height:getComputedStyle(document.documentElement).getPropertyValue('--dsh-windows-titlebar-height').trim()})")
            learning = None
            if args.open_synthetic_video:
                deadline = time.monotonic() + 20
                while not page.evaluate("Boolean(document.querySelector('[data-testid=\"learning-course-directory\"] button[aria-label=\"synthetic-course.webm\"]'))"):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("DESKTOP_SYNTHETIC_VIDEO_NOT_FOUND: " + str(page.evaluate("document.body.innerText.slice(0,800)")))
                    time.sleep(0.05)
                page.evaluate("document.querySelector('[data-testid=\"learning-course-directory\"] button[aria-label=\"synthetic-course.webm\"]').click()")
                deadline = time.monotonic() + 20
                while not page.evaluate("Boolean(document.querySelector('video')?.readyState>=2)"):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("DESKTOP_SYNTHETIC_VIDEO_NOT_READY")
                    time.sleep(0.05)
                page.evaluate("document.querySelector('video').pause()")
                learning = page.evaluate("(()=>{const rect=s=>{const e=document.querySelector(s),r=e?.getBoundingClientRect();return r?{x:r.x,y:r.y,width:r.width,height:r.height}:null};const v=document.querySelector('video');return {courses:rect('[data-learning-region=courses]'),materials:rect('[data-testid=learning-materials]'),ai:rect('[data-learning-region=ai]'),video:{width:v.videoWidth,height:v.videoHeight,readyState:v.readyState},native_composer:Boolean(document.querySelector('[data-learning-region=ai] [contenteditable=true]')),native_slots:document.querySelectorAll('[data-slot]').length}})()")
                assert learning["native_composer"] and learning["native_slots"] > 0, learning
                assert learning["courses"]["x"] < learning["materials"]["x"] < learning["ai"]["x"], learning
            if args.screenshot:
                screenshot = page.command("Page.captureScreenshot", {"format": "png"})
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                args.screenshot.write_bytes(base64.b64decode(screenshot["data"]))
            closed = native.evaluate("(()=>{const E=process.mainModule.require('electron'),w=E.BrowserWindow.getAllWindows()[0],id=w.id;w.close();return {same_window:E.BrowserWindow.getAllWindows()[0]?.id===id,destroyed:w.isDestroyed(),visible:w.isVisible()}})()")
            result = {"kind": "official-client-in-electron" if args.url else "synthetic-carrier-only", "native": geometry, "security": security, "close": closed, "learning": learning}
            # Windows caption/DPI rounding can add a few DIP to requested bounds.
            assert 1280 <= geometry["bounds"]["width"] <= 1290 and 820 <= geometry["bounds"]["height"] <= 830, result
            assert geometry["title"] == "AI 辅助学习", result
            assert geometry["contextIsolation"] and not geometry["nodeIntegration"] and geometry["sandbox"] and geometry["webSecurity"], result
            assert all(security[key] for key in ("node_absent", "ipc_absent", "caption")) and security["caption_height"] == "40px", result
            assert closed["same_window"] and not closed["destroyed"] and not closed["visible"], result
            try:
                native.evaluate("process.mainModule.require('electron').app.quit()")
            except Exception:
                # A successful app quit closes the inspector before its reply.
                pass
        process.wait(timeout=15)
        result["electron_exit_code"] = process.returncode
        assert process.returncode == 0, result
    finally:
        result["cleanup"] = group.close()
        if server is not None:
            server.shutdown()
            server.server_close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
