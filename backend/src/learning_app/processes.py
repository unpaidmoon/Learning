"""Ownership of child process trees; never discover or terminate by executable name.

Windows children start suspended, join this instance's Job Object, then resume.
This closes the usual spawn/assignment window in which grandchildren can escape.
The CPython process handle is contained here, rather than leaking into callers.
"""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any


class ProcessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def require_free_port(host: str, port: int) -> None:
    """Fail closed on an occupied port; never attach to its current listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((host, port))
        except OSError as exc:
            raise ProcessError(
                "PORT_IN_USE", f"端口 {host}:{port} 不可用；未结束占用进程。"
            ) from exc


if os.name == "nt":
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process_time", ctypes.c_int64),
            ("per_job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_operations",
                "write_operations",
                "other_operations",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", _BasicLimits),
            ("io", _IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _kernel.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class OwnedProcessGroup:
    """One private job owns all processes started through this object.

    close() is application/Host shutdown, not model generation cancellation.
    Callers may request graceful child shutdown first; close always bounds cleanup.
    """

    def __init__(self, label: str = "learning-app") -> None:
        self.label = label
        self.processes: list[subprocess.Popen[str]] = []
        self._closed = False
        self._job: Any = None
        if os.name == "nt":
            self._job = _kernel.CreateJobObjectW(None, None)
            if not self._job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                _kernel.CloseHandle(self._job)
                self._job = None
                raise error

    def spawn(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        stdout: int | IO[str] | None = subprocess.PIPE,
        stderr: int | IO[str] | None = subprocess.PIPE,
        stdin: int | IO[str] | None = subprocess.PIPE,
    ) -> subprocess.Popen[str]:
        if self._closed:
            raise ProcessError("PROCESS_GROUP_CLOSED", "所属进程组已关闭。")
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
        self.processes.append(process)
        if os.name == "nt":
            # Popen's owned process handle is intentionally used only in this adapter.
            handle = int(process._handle)  # type: ignore[attr-defined]
            if not _kernel.AssignProcessToJobObject(self._job, handle):
                error = ctypes.WinError(ctypes.get_last_error())
                process.kill()
                process.wait(timeout=5)
                raise ProcessError("PROCESS_OWNERSHIP_FAILED", str(error)) from error
            if _ntdll.NtResumeProcess(handle) < 0:
                process.kill()
                process.wait(timeout=5)
                raise ProcessError("PROCESS_RESUME_FAILED", "所属子进程无法恢复执行。")
        return process

    def pids(self) -> list[int]:
        if self._closed:
            return []
        if os.name != "nt":
            return [process.pid for process in self.processes if process.poll() is None]
        capacity = 32
        while capacity <= 65536:

            class _PidList(ctypes.Structure):
                _fields_ = [
                    ("assigned", wintypes.DWORD),
                    ("count", wintypes.DWORD),
                    ("ids", ctypes.c_size_t * capacity),
                ]

            result = _PidList()
            if _kernel.QueryInformationJobObject(
                self._job, 3, ctypes.byref(result), ctypes.sizeof(result), None
            ):
                return sorted(int(pid) for pid in result.ids[: result.count])
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            capacity *= 2
        raise ProcessError("PROCESS_LIST_TOO_LARGE", "所属进程数量超出可检查范围。")

    def close(self, grace_seconds: float = 3.0) -> dict[str, object]:
        if self._closed:
            return {"owned_pids_before": [], "remaining_pids": [], "method": "already_closed"}
        before = self.pids()
        if os.name == "nt":
            if before and not _kernel.TerminateJobObject(self._job, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            for process in self.processes:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + grace_seconds
        for process in self.processes:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)
        # Job membership can lag the root process signal briefly; observe the job itself.
        remaining = self.pids()
        while remaining and time.monotonic() < deadline:
            time.sleep(0.01)
            remaining = self.pids()
        if os.name == "nt":
            _kernel.CloseHandle(self._job)
            self._job = None
        for process in self.processes:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        self._closed = True
        return {
            "owned_pids_before": before,
            "remaining_pids": remaining,
            "method": "windows_job" if os.name == "nt" else "owned_process_groups",
        }

    def __enter__(self) -> OwnedProcessGroup:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
