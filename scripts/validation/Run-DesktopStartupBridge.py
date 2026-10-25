"""Actual sandboxed preload bridge; only read and denied mutation are exercised."""

import argparse
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer

from websockets.sync.client import connect

from learning_app.config import PROJECT_ROOT
from learning_app.processes import OwnedProcessGroup

sys.path.insert(0, str(PROJECT_ROOT / "scripts/desktop"))
from check_shell import Debugger, PageFixture, debugger_targets, free_port  # noqa: E402
from bootstrap import verified_executable  # noqa: E402


def run(identity):
    if not identity or not all(c.isalnum() or c in "_-" for c in identity):
        raise ValueError("OWNED_RUN_ID_INVALID")
    output = PROJECT_ROOT / ".runtime/validation" / identity
    output.mkdir(exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    debug = free_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "LEARNING_APP_DATA_DIR"}
    }
    environment.update(
        LEARNING_DESKTOP_URL=f"http://127.0.0.1:{server.server_port}/desktop/",
        LEARNING_DESKTOP_OWNS_BACKEND="0",
        LEARNING_DESKTOP_USER_DATA=str(output / "browser"),
        LEARNING_DESKTOP_TEST_HIDDEN="1",
    )
    group = OwnedProcessGroup("startup-bridge-observer")
    result = {"status": "failed", "startup_mutations": 0, "model_calls": 0}
    try:
        process = group.spawn(
            [
                str(verified_executable()),
                f"--remote-debugging-port={debug}",
                str(PROJECT_ROOT / "desktop"),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
        )
        target = next(row for row in debugger_targets(debug) if row["type"] == "page")
        with connect(target["webSocketDebuggerUrl"]) as socket:
            page = Debugger(socket)
            import time

            deadline = time.monotonic() + 20
            while not page.evaluate(
                "Boolean(window.learningDesktopRuntime&&document.querySelector('h1'))"
            ):
                if time.monotonic() > deadline:
                    raise RuntimeError("PRELOAD_BRIDGE_NOT_READY")
                time.sleep(0.05)

            def evaluate_async(expression):
                value = page.command(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                if "exceptionDetails" in value:
                    raise RuntimeError("STARTUP_BRIDGE_PROBE_EXCEPTION")
                return value["result"]["value"]

            observed = evaluate_async(
                "(async()=>{const api=window.learningDesktopRuntime;const value=await api.startupStatus();let denied=false;try{await api.setStartup({enabled:true,operation_id:'startup_'+'a'.repeat(32),expected_revision:'unavailable'});}catch(e){denied=String(e).includes('STARTUP_USER_ACTION_REQUIRED');}return{status:value,withoutGestureDenied:denied,hasNode:typeof require!=='undefined',methods:Object.keys(api).sort()};})()"
            )
            assert observed["status"]["error_code"] == "STARTUP_PORTABLE_ENTRY_REQUIRED"
            assert observed["withoutGestureDenied"] and not observed["hasNode"]
            assert observed["methods"] == [
                "reconcileStartup",
                "setStartup",
                "startupStatus",
            ]
            result.update(status="passed", observed=observed, pid=process.pid)
    finally:
        result["cleanup"] = group.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
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
