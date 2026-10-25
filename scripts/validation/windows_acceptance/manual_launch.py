"""Open only the profile created by this acceptance kit; no credentials or installs."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", type=Path, required=True)
    args = parser.parse_args()
    root = args.kit.resolve()
    latest = json.loads((root / "验收结果/latest.json").read_text("utf-8-sig"))
    candidate = root / latest["release_id"]
    profile = Path(latest["profile"])
    if not profile.resolve().is_relative_to((root / "验收结果").resolve()):
        raise RuntimeError("ACCEPTANCE_PROFILE_OUTSIDE_RESULT_DIRECTORY")
    if not (profile / "data/business.sqlite3").is_file():
        raise RuntimeError("AUTOMATIC_CHECK_HAS_NOT_CREATED_A_REVIEWABLE_PROFILE")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    completed = subprocess.run(
        [
            str(candidate / "启动学习.exe"),
            "--profile-dir",
            str(profile),
            "--backend-port",
            str(latest["port"]),
        ],
        cwd=root,
        check=False,
        **options,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
