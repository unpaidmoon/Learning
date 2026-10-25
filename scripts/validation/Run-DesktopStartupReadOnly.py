"""Observe the pinned Electron login-item API without creating a startup entry."""

import argparse
import hashlib
import json
import os
import subprocess

from learning_app.config import PROJECT_ROOT
from learning_app.processes import OwnedProcessGroup


def run(identity):
    if not identity or not all(c.isalnum() or c in "_-" for c in identity):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / identity
    directory.mkdir(exist_ok=False)
    application = directory / "application"
    application.mkdir()
    (application / "package.json").write_text(
        json.dumps(
            {
                "name": "learning-startup-readonly-proof",
                "version": "1.0.0",
                "main": "main.cjs",
            }
        ),
        "utf-8",
    )
    script = """const{app}=require('electron');
app.setPath('userData',process.env.LEARNING_STARTUP_PROBE_PROFILE);
app.disableHardwareAcceleration();
app.whenReady().then(()=>{try{
const result=app.getLoginItemSettings({path:process.env.LEARNING_STARTUP_PROBE_LAUNCHER,args:['--profile-dir',process.env.LEARNING_STARTUP_PROBE_PROFILE]});
console.log('LEARNING_STARTUP_READONLY '+JSON.stringify({electron:process.versions.electron,hasOpenAtLogin:typeof result.openAtLogin==='boolean',hasWillLaunch:typeof result.executableWillLaunchAtLogin==='boolean',hasLaunchItems:Array.isArray(result.launchItems),matchingEntries:result.launchItems?.length}));
}finally{app.quit();}});
"""
    (application / "main.cjs").write_text(script, "utf-8")
    executable = PROJECT_ROOT / ".runtime/desktop/electron/electron.exe"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS"}
    }
    env.update(
        LEARNING_STARTUP_PROBE_PROFILE=str(directory / "profile"),
        LEARNING_STARTUP_PROBE_LAUNCHER=str(
            PROJECT_ROOT / ".runtime/dist/可搬迁 学习包 r5/启动学习.exe"
        ),
    )
    group = OwnedProcessGroup("desktop-startup-readonly-proof")
    result = {
        "status": "failed",
        "registry_mutations": 0,
        "layer": "actual pinned Electron getLoginItemSettings; no set call and no logon test",
    }
    try:
        process = group.spawn(
            [str(executable), str(application)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=30)
        (directory / "stderr.log").write_text(stderr, "utf-8")
        line = next(
            (
                line
                for line in stdout.splitlines()
                if line.startswith("LEARNING_STARTUP_READONLY ")
            ),
            None,
        )
        if process.returncode or not line:
            raise RuntimeError("READ_ONLY_API_PROBE_FAILED")
        observed = json.loads(line.removeprefix("LEARNING_STARTUP_READONLY "))
        result.update(
            observed=observed,
            status="passed"
            if observed["hasOpenAtLogin"]
            and observed["hasWillLaunch"]
            and observed["hasLaunchItems"]
            else "failed",
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
    finally:
        result["cleanup"] = group.close()
        (directory / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    result = run(parser.parse_args().run_id)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 1)
