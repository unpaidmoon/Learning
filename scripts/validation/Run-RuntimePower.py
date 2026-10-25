"""A short owned-process power request; never changes a Windows power plan."""

import argparse
import hashlib
import json
import os
import subprocess
import sys

from learning_app.config import PROJECT_ROOT
from learning_app.processes import OwnedProcessGroup


def run(identity):
    if not identity or not all(c.isalnum() or c in "_-" for c in identity):
        raise ValueError("OWNED_RUN_ID_INVALID")
    output = PROJECT_ROOT / ".runtime/validation" / identity
    output.mkdir(exist_ok=False)
    if os.name != "nt":
        result = {"status": "not_run", "reason": "WINDOWS_REQUIRED"}
    else:
        program = """import ctypes,json,threading,time
from learning_app.runtime_preferences import WindowsExecutionPower
k=ctypes.WinDLL('kernel32',use_last_error=True);f=k.SetThreadExecutionState;f.argtypes=[ctypes.c_uint32];f.restype=ctypes.c_uint32
power=WindowsExecutionPower();tid=threading.get_ident()
try:
 power.set(True);time.sleep(0.15)
 held=f(0x80000000)
 power.set(False)
 released=f(0x80000000)
 print(json.dumps({'system_requested':bool(held&1),'display_not_requested':not bool(held&2),'own_thread_released':not bool(released&3),'same_thread':threading.get_ident()==tid}),flush=True)
finally:
 power.set(False)
"""
        group = OwnedProcessGroup("runtime-power-proof")
        try:
            process = group.spawn(
                [sys.executable, "-I", "-c", program],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate(timeout=15)
            if process.returncode:
                raise RuntimeError("OWNED_POWER_API_FAILED:" + stderr[-1000:])
            observations = json.loads(stdout)
            result = {
                "status": "passed" if all(observations.values()) else "failed",
                "observations": observations,
                "scope": "owned thread; 150ms system idle request; no display request, power plan, startup entry, manual sleep or lid test",
                "source_sha256": hashlib.sha256(
                    (
                        PROJECT_ROOT / "backend/src/learning_app/runtime_preferences.py"
                    ).read_bytes()
                ).hexdigest(),
            }
        finally:
            cleanup = group.close()
        result["cleanup"] = cleanup
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    result = run(parser.parse_args().run_id)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 1)
