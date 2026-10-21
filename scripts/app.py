"""Use the project's own interpreter, independent of the invoking shell/Python."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    python = (
        root
        / "backend/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not python.is_file():
        print(
            "PROJECT_ENVIRONMENT_MISSING: 请先运行 scripts/bootstrap.ps1。",
            file=sys.stderr,
        )
        return 1
    if Path(sys.executable).resolve() != python.resolve():
        # Replace on POSIX; Windows exec waits on the new process, so delegate via
        # subprocess while forwarding Ctrl+C to the actual owned launcher.
        command = [str(python), str(Path(__file__).resolve()), *sys.argv[1:]]
        try:
            return subprocess.call(command, cwd=root)
        except KeyboardInterrupt:
            return 130
    from learning_app.dev import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
