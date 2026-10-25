"""Isolated visible platform desktop, with the ordinary owned-process launcher."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(
        description="启动独立B站开发桌面，不连接原开发实例的数据"
    )
    parser.add_argument("--backend-port", type=int, default=8876)
    parser.add_argument("--debug-port", type=int)
    parser.add_argument("--model-secret-file", type=Path)
    parser.add_argument("--api-key-route", action="store_true")
    args = parser.parse_args()
    environment = {**os.environ, "LEARNING_APP_HARNESS_ENABLED": "true"}
    if args.model_secret_file:
        environment["LEARNING_APP_SECRET_FILE"] = str(args.model_secret_file.resolve())
    if args.api_key_route:
        environment["LEARNING_APP_CREDENTIAL_ROUTE"] = "api_key"
    command = [
        sys.executable,
        str(ROOT / "scripts/desktop/run.py"),
        "--backend-port",
        str(args.backend_port),
        "--data-dir",
        str(ROOT / ".runtime/platforms/desktop/data"),
        "--user-data-dir",
        str(ROOT / ".runtime/platforms/desktop/electron-profile"),
    ]
    if args.debug_port:
        command += ["--debug-port", str(args.debug_port)]
    return subprocess.call(
        command,
        cwd=ROOT,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
