"""Verified portable entry. No dependency resolution, building, or model call."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]


def verify_release(root: Path = ROOT) -> dict:
    manifest = json.loads((root / "desktop-release.json").read_text("utf-8"))
    if manifest.get("format") != 1 or manifest.get("platform") != "win32-x64":
        raise RuntimeError("DESKTOP_RELEASE_VERSION_INVALID")
    files = manifest.get("files", {})
    required = {
        "runtime/python/python.exe",
        "runtime/python/pythonw.exe",
        "runtime/python/python312._pth",
        "scripts/app.py",
        "scripts/desktop/run.py",
        "desktop/main.cjs",
        "启动学习.exe",
    }
    if not required.issubset(files):
        raise RuntimeError("DESKTOP_RELEASE_INCOMPLETE")
    for name, spec in files.items():
        parts = PurePosixPath(name).parts
        if (
            not parts
            or PurePosixPath(name).is_absolute()
            or ".." in parts
            or "\\" in name
            or ":" in name
            or "\x00" in name
        ):
            raise RuntimeError("DESKTOP_RELEASE_PATH_DENIED")
        target = root.joinpath(*parts)
        if not target.resolve().is_relative_to(root.resolve()) or any(
            p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
            for p in [target, *target.parents]
        ):
            raise RuntimeError("DESKTOP_RELEASE_PATH_DENIED")
        if not target.is_file() or target.stat().st_size != spec["bytes"]:
            raise RuntimeError("DESKTOP_RELEASE_FILE_MISSING: " + name)
        with target.open("rb") as incoming:
            digest = hashlib.file_digest(incoming, "sha256").hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError("DESKTOP_RELEASE_HASH_MISMATCH: " + name)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="启动已校验的 Windows 学习运行包")
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--backend-port", type=int, default=8766)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--hidden", action="store_true", help="仅验收：不显示窗口")
    parser.add_argument("--debug-port", type=int, help="仅验收：本地 Chromium 调试端口")
    args = parser.parse_args()
    manifest = verify_release()
    if args.verify_only:
        print(
            json.dumps(
                {
                    "status": "passed",
                    "release_id": manifest["release_id"],
                    "files": len(manifest["files"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    profile = (
        args.profile_dir or Path(os.environ["LOCALAPPDATA"]) / "LearningWorkbench"
    ).resolve()
    if profile.is_relative_to(ROOT):
        raise RuntimeError("DESKTOP_PROFILE_INSIDE_RELEASE")
    profile.mkdir(parents=True, exist_ok=True)
    # An inherited development environment must not reroute a portable release.
    for key in list(os.environ):
        if key.upper().startswith(
            ("LEARNING_APP_", "LEARNING_DESKTOP_")
        ) or key.upper() in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "NODE_OPTIONS",
            "ELECTRON_RUN_AS_NODE",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_AUTH_TOKEN",
        }:
            os.environ.pop(key, None)
    os.environ["PATH"] = str(
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    )
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
    from run import main as run_desktop

    sys.argv = [
        str(ROOT / "scripts/desktop/run.py"),
        "--no-prepare",
        "--data-dir",
        str(profile / "data"),
        "--runtime-dir",
        str(profile / "run"),
        "--user-data-dir",
        str(profile / "browser"),
        "--backend-port",
        str(args.backend_port),
    ]
    if args.hidden:
        sys.argv.append("--hidden")
    if args.debug_port is not None:
        sys.argv.extend(["--debug-port", str(args.debug_port)])
    with (profile / "launcher.log").open("a", encoding="utf-8", buffering=1) as log:
        previous = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            return run_desktop()
        except Exception:
            traceback.print_exc(file=log)
            raise
        finally:
            sys.stdout, sys.stderr = previous


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = f"{getattr(error, 'code', 'DESKTOP_START_FAILED')}：{error}"
        if sys.stderr is not None:
            traceback.print_exc()
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "AI 辅助学习", 0x10)
        raise SystemExit(1) from None
