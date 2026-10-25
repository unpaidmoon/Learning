"""Run the fixed, sandboxed offline check with this release's own Electron."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from learning_app.processes import OwnedProcessGroup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application-db", type=Path)
    args = parser.parse_args()
    root, output = args.kit.resolve(), args.output.resolve()
    if not output.is_relative_to((root / "验收结果").resolve()):
        raise RuntimeError("OFFLINE_ACCEPTANCE_OUTPUT_OUTSIDE_KIT")
    config = json.loads((root / "验收工具/kit.json").read_text("utf-8"))
    database = args.application_db.resolve() if args.application_db else None
    if database and not database.is_relative_to((root / "验收结果").resolve()):
        raise RuntimeError("OFFLINE_ACCEPTANCE_DATABASE_OUTSIDE_KIT")
    database_before = (
        hashlib.sha256(database.read_bytes()).hexdigest() if database else None
    )
    candidate = root / config["release_id"]
    samples = root / "验收工具/离线示例"
    before = {
        path.relative_to(samples).as_posix(): path.read_bytes()
        for path in samples.rglob("*")
        if path.is_file()
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "NODE_PATH"}
    }
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    group = OwnedProcessGroup("offline-acceptance")
    try:
        process = group.spawn(
            [
                str(candidate / ".runtime/desktop/electron/electron.exe"),
                str(root / "验收工具/offline_check.cjs"),
                "--samples",
                str(samples),
                "--output",
                str(output),
            ],
            cwd=root,
            env=environment,
        )
        stdout, stderr = process.communicate(timeout=130)
        status = process.returncode
        if status:
            (output / "offline-error.txt").write_text(
                (stdout + stderr)[-8000:], "utf-8"
            )
    finally:
        cleanup = group.close(grace_seconds=5)
    summary_path = output / "offline-summary.json"
    summary = (
        json.loads(summary_path.read_text("utf-8"))
        if summary_path.is_file()
        else {"status": "failed", "error": "OFFLINE_CARRIER_RESULT_MISSING"}
    )
    after = {
        path.relative_to(samples).as_posix(): path.read_bytes()
        for path in samples.rglob("*")
        if path.is_file()
    }
    summary.update(sample_files_unchanged=before == after, cleanup=cleanup)
    if database:
        summary["application_database_unchanged"] = (
            hashlib.sha256(database.read_bytes()).hexdigest() == database_before
        )
    if (
        before != after
        or cleanup["remaining_pids"]
        or summary.get("application_database_unchanged") is False
    ):
        summary["status"] = "failed"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    print(
        json.dumps(
            {
                "status": summary["status"],
                "checks": len(summary.get("assertions", [])),
                "error": summary.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
