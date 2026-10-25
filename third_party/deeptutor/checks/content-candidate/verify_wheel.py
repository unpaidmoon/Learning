"""Actual new wheel in a private venv; no changes to application environments."""

from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]
REPO = PACKAGE.parents[1]
CACHE = REPO / ".local/cache/workbench/deeptutor-c06-c08-c13"
ENV = CACHE / "wheel-env"
WHEEL = CACHE / "dist/learning_deeptutor_reuse-0.1.0-py3-none-any.whl"
UV = shutil.which("uv")
assert UV and WHEEL.is_file()
protected = [
    PACKAGE / "uv.lock",
    PACKAGE / "pyproject.toml",
    PACKAGE / "src/deeptutor_reuse/source_digest.py",
    REPO / "backend/uv.lock",
    REPO / "backend/pyproject.toml",
]
before = {
    str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in protected
}
environment = os.environ.copy()
environment["UV_PROJECT_ENVIRONMENT"] = str(ENV)
environment["UV_NO_PROGRESS"] = "1"
commands = []


def run(label, args):
    result = subprocess.run(
        args,
        cwd=REPO,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        timeout=120,
    )
    (CACHE / (label + ".stdout.txt")).write_text(result.stdout, "utf-8")
    (CACHE / (label + ".stderr.txt")).write_text(result.stderr, "utf-8")
    commands.append({"label": label, "argv": args, "returncode": result.returncode})
    if result.returncode:
        raise RuntimeError(label + ":" + result.stderr[-1500:])


try:
    run(
        "isolated-lock-sync",
        [
            UV,
            "sync",
            "--project",
            str(PACKAGE),
            "--locked",
            "--all-extras",
            "--no-install-project",
            "--offline",
        ],
    )
    python = ENV / "Scripts/python.exe"
    run(
        "isolated-wheel-install",
        [
            UV,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--offline",
            str(WHEEL),
        ],
    )
    run(
        "installed-content",
        [
            str(python),
            str(PACKAGE / "checks/content-candidate/verify_content.py"),
            "--installed",
            "--output",
            str(CACHE / "wheel-r1"),
        ],
    )
    run(
        "installed-original-regression",
        [str(python), str(PACKAGE / "tests/test_reuse.py"), "--installed"],
    )
    run(
        "installed-book-regression",
        [
            str(python),
            str(PACKAGE / "checks/integration-candidate/verify_integration.py"),
            "--installed",
            "--output",
            str(CACHE / "book-regression-r1"),
        ],
    )
    after = {
        str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in protected
    }
    assert before == after, "protected source/config/locks changed during test"
    result = {
        "status": "passed",
        "wheel_sha256": hashlib.sha256(WHEEL.read_bytes()).hexdigest(),
        "commands": commands,
        "protected_before": before,
        "protected_after": after,
    }
except Exception as error:
    result = {"status": "failed", "error": str(error), "commands": commands}
(CACHE / "wheel-verification.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
)
print(
    json.dumps(
        {
            "status": result["status"],
            "wheel_sha256": result.get("wheel_sha256"),
            "error": result.get("error"),
            "commands": len(commands),
        }
    )
)
raise SystemExit(0 if result["status"] == "passed" else 1)
