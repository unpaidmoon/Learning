"""Bounded, credential-free Web Host contract probe. Never sends a prompt."""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
import websockets

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "unpacked/deepseek_harness_runtime/runtime/deepseek-harness-sdk-runtime-win-x64.exe"
WORKSPACE = ROOT / "workspace-web"
HOME = ROOT / "home-web"
RESULT = ROOT / "probe-results.json"
MAX_RSS = 1024 * 1024 * 1024
MAX_JOB_COMMIT = 1536 * 1024 * 1024


class BasicLimits(ctypes.Structure):
    _fields_ = [("user", ctypes.c_int64), ("jobuser", ctypes.c_int64), ("flags", wt.DWORD),
                ("minws", ctypes.c_size_t), ("maxws", ctypes.c_size_t), ("active", wt.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ro", "wo", "oo", "rt", "wt", "ot")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("peakws", "ws", "peakpaged", "paged", "peaknonpaged", "nonpaged", "pagefile", "peakpagefile", "private")]


class JobPids(ctypes.Structure):
    _fields_ = [("assigned", wt.DWORD), ("count", wt.DWORD), ("pids", ctypes.c_size_t * 64)]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel.CreateJobObjectW.restype = wt.HANDLE
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
kernel.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
kernel.CloseHandle.argtypes = [wt.HANDLE]
kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel.OpenProcess.restype = wt.HANDLE
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]


def redact(text: str) -> str:
    text = re.sub(r"(?i)(token=)[^&\s\"']+", r"\1<redacted>", text)
    return re.sub(r"(?i)(?:sk-[A-Za-z0-9_-]+)", "<redacted-key>", text)


def make_env(home: Path) -> dict[str, str]:
    keep = ["SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMDATA", "PATHEXT"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = str(Path(env.get("SystemRoot", r"C:\Windows")) / "System32")
    env.update({"DSH_HOME": str(home), "DSH_RESOURCE_CACHE": str(ROOT / "resources"),
                "DSH_PRIMARY_RUNTIME": "", "DSH_TELEMETRY_MODE": "DISABLED", "DSH_TELEMETRY_DISABLED": "1", "NO_COLOR": "1"})
    return env


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def job_pids(job: int) -> list[int]:
    value = JobPids()
    if not kernel.QueryInformationJobObject(job, 3, ctypes.byref(value), ctypes.sizeof(value), None):
        return []
    return [int(value.pids[i]) for i in range(min(value.count, 64))]


def rss_for(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        handle = kernel.OpenProcess(0x1000 | 0x0010, False, pid)
        if not handle:
            continue
        try:
            value = ProcessMemory()
            value.cb = ctypes.sizeof(value)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), ctypes.sizeof(value)):
                total += value.ws
        finally:
            kernel.CloseHandle(handle)
    return total


def run() -> dict:
    result = {"date": "2026-10-19", "artifactVersion": "0.2.1rc2", "modelRequestsSent": 0,
              "credentialsInherited": False, "steps": [], "limits": {"startupSeconds": 35, "contractSeconds": 30,
              "rssBytes": MAX_RSS, "jobCommitBytes": MAX_JOB_COMMIT}, "cleanup": {}}
    WORKSPACE.mkdir(exist_ok=True)
    if HOME.exists():
        raise RuntimeError("Refusing to reuse an existing probe home")
    HOME.mkdir()
    stub_hits = []

    class RejectModels(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            stub_hits.append({"method": "GET", "path": self.path.split("?", 1)[0]})
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"local probe rejects all model requests")
        do_POST = do_GET
        def log_message(self, *_args):
            return

    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RejectModels)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    env = make_env(HOME)
    env["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{stub.server_port}/v1"
    # Deliberately no DEEPSEEK_API_KEY: create/follow/cancel must not infer.
    port = free_port()
    result["port"] = port
    command = [str(EXE), "web", "--host", "127.0.0.1", "--port", str(port), "--no-open"]
    result["command"] = "<bundled-runtime> web --host 127.0.0.1 --port <local-port> --no-open"
    output = deque(maxlen=1000)
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000 | 0x0200  # Kill all descendants on close; job memory cap.
    limits.job_memory = MAX_JOB_COMMIT
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    proc = None
    stop_monitor = threading.Event()
    peak_rss = 0
    peak_count = 0
    killed_for_memory = False
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(command, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
            proc.terminate()
            proc.wait(timeout=5)
            raise ctypes.WinError(ctypes.get_last_error())
        result["jobObjectAssigned"] = True
        result["pid"] = proc.pid

        def drain(pipe, channel):
            for line in pipe:
                output.append((channel, line.rstrip()))

        threading.Thread(target=drain, args=(proc.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=drain, args=(proc.stderr, "stderr"), daemon=True).start()

        def monitor():
            nonlocal peak_rss, peak_count, killed_for_memory
            while not stop_monitor.wait(0.1):
                pids = job_pids(job)
                peak_count = max(peak_count, len(pids))
                usage = rss_for(pids)
                peak_rss = max(peak_rss, usage)
                if usage > MAX_RSS:
                    killed_for_memory = True
                    kernel.TerminateJobObject(job, 2)
                    return

        threading.Thread(target=monitor, daemon=True).start()
        launch_url = None
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline and proc.poll() is None:
            for _, line in tuple(output):
                for candidate in re.findall(r"https?://[^\s\x1b]+", line):
                    parts = urlsplit(candidate)
                    if parts.hostname == "127.0.0.1" and parts.port == port and parse_qs(parts.query).get("token"):
                        launch_url = candidate
                        break
            if launch_url:
                break
            time.sleep(0.05)
        result["startupSeconds"] = round(time.perf_counter() - started, 3)
        if launch_url is None:
            result["steps"].append({"step": "web_host_ready", "passed": False, "processExit": proc.poll(),
                                    "reason": "memory cap" if killed_for_memory else "no ready URL before exit/deadline"})
            return result
        result["steps"].append({"step": "web_host_ready", "passed": True})
        token = parse_qs(urlsplit(launch_url).query)["token"][0]
        base = f"http://127.0.0.1:{port}"

        async def contracts():
            with httpx.Client(base_url=base, timeout=5, follow_redirects=False, trust_env=False) as client:
                response = client.get("/connect", params={"token": token, "interactions": "none"})
                result["steps"].append({"step": "api_peer_connect", "status": response.status_code,
                                        "passed": response.status_code == 302, "cookieReceived": bool(client.cookies)})
                if response.status_code != 302:
                    return
                cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                location = response.headers.get("location", "")
                if "remote.mux" not in location:
                    raise RuntimeError("Unexpected API mux location")
                mux = f"ws://127.0.0.1:{port}/api/remote.mux"
                async with websockets.connect(mux, additional_headers={"Cookie": cookie}, proxy=None, open_timeout=5, close_timeout=2) as ws:
                    result["steps"].append({"step": "api_peer_websocket", "passed": True})

                    def rpc(method: str, request: dict):
                        response = client.post("/api/" + method, json={"type": "client-request", "rpcId": "probe-" + method.replace("/", "-"),
                            "method": method, "payload": {"args": {"request": request}}})
                        try:
                            body = response.json()
                        except ValueError:
                            body = {"parseError": True}
                        return response.status_code, body

                    status, body = rpc("session/create", {"cwd": str(WORKSPACE), "sessionId": "probe-empty-session"})
                    passed = status == 200 and body.get("result", {}).get("ok") is True
                    row = {"step": "empty_session_create", "status": status, "passed": passed}
                    if not passed:
                        row["response"] = body
                    result["steps"].append(row)
                    if not passed:
                        return
                    session_id = body["result"]["value"]["sessionId"]
                    await ws.send(json.dumps({"type": "open", "streamId": "probe-follow", "endpoint": "session/follow", "payload": {"args": {
                        "request": {"address": {"kind": "session", "sessionId": session_id}, "assistantStream": True}}}}))
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    value = frame.get("value", {})
                    result["steps"].append({"step": "empty_session_follow", "passed": frame.get("type") == "item" and value.get("type") == "snapshot",
                                            "carrierFrame": frame.get("type"), "valueType": value.get("type"), "snapshotHasAssistantStream": "assistantStream" in value})
                    status, body = rpc("session/cancel", {"sessionId": session_id})
                    result["steps"].append({"step": "idle_session_cancel_contract", "status": status,
                                            "passed": status == 200 and body.get("result", {}).get("value", {}).get("accepted") is True,
                                            "activeGenerationCancellationTested": False})
                    await ws.send(json.dumps({"type": "cancel", "streamId": "probe-follow"}))
                    result["steps"].append({"step": "follow_stream_cancel_sent", "passed": True, "isModelCancel": False})
                await asyncio.sleep(0.2)
                status, _body = rpc("session/cancel", {"sessionId": session_id})
                result["steps"].append({"step": "closed_peer_cookie_rejected", "status": status, "passed": status == 401})
                response = client.get("/connect", params={"token": token, "interactions": "none"})
                result["steps"].append({"step": "api_peer_reconnect", "status": response.status_code, "passed": response.status_code == 302})
                if response.status_code == 302:
                    new_cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                    async with websockets.connect(mux, additional_headers={"Cookie": new_cookie}, proxy=None, open_timeout=5, close_timeout=2):
                        pass

        asyncio.run(asyncio.wait_for(contracts(), timeout=30))
        return result
    except Exception as exc:
        result["error"] = redact(type(exc).__name__ + ": " + str(exc))
        return result
    finally:
        stop_monitor.set()
        before = job_pids(job)
        result["cleanup"]["ownedPidsBeforeTermination"] = before
        kernel.TerminateJobObject(job, 0)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        result["cleanup"]["method"] = "TerminateJobObject (forced idle-probe cleanup; not graceful-shutdown proof)"
        result["cleanup"]["remainingJobPids"] = job_pids(job)
        kernel.CloseHandle(job)
        stub.shutdown()
        stub.server_close()
        with socket.socket() as sock:
            sock.settimeout(0.5)
            result["cleanup"]["hostPortClosed"] = sock.connect_ex(("127.0.0.1", port)) != 0
        result["peakAggregateRssBytes"] = peak_rss
        result["peakOwnedProcessCount"] = peak_count
        result["memoryCapTriggered"] = killed_for_memory
        result["localRejectStubHits"] = stub_hits
        result["elapsedSeconds"] = round(time.perf_counter() - started, 3)
        result["sanitizedDiagnosticTail"] = [{"channel": ch, "text": redact(line)} for ch, line in list(output)[-30:]]
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
