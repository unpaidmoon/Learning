"""Capture a reviewed source baseline without changing the source worktree/index."""

import hashlib
import json
import subprocess
from pathlib import Path

SOURCE = Path("D:/Codex/Learning")
TARGET = Path(__file__).resolve().parents[2]
ROOTS = {
    "backend",
    "frontend",
    "contracts",
    "desktop",
    "docs",
    "harness",
    "resources",
    "scripts",
    "tests",
    "third_party",
    "research",
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(SOURCE), *args])


def names():
    tracked = set(git("ls-files", "-z").decode().split("\0")) - {""}
    extra = set(
        git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    )
    extra = {p for p in extra if p and (p.split("/")[0] in ROOTS or p.endswith(".cmd"))}
    extra = {
        p
        for p in extra
        if "/checks/" not in p and not p.startswith("research/reports/")
    }
    return sorted(tracked | extra)


def main():
    if SOURCE.resolve() == TARGET.resolve():
        raise RuntimeError("Snapshot target must be a separate worktree")
    if (TARGET / "docs/development/platform-baseline-20261024.json").exists():
        raise RuntimeError(
            "Baseline already captured; use audit_delivery.py for read-only comparison"
        )
    files = names()
    blobs = {
        p: (SOURCE / p).read_bytes() if (SOURCE / p).is_file() else None for p in files
    }
    before_index = hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest()
    # Read twice before writing, fail on concurrent relevant changes.
    for p, data in blobs.items():
        current = (SOURCE / p).read_bytes() if (SOURCE / p).is_file() else None
        if current != data:
            raise RuntimeError(f"Concurrent source change: {p}")
    if names() != files:
        raise RuntimeError("Concurrent manifest change")
    rows = []
    for p, data in blobs.items():
        dst = TARGET / p
        if data is None:
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        rows.append(
            {
                "path": p,
                "sha256": hashlib.sha256(data).hexdigest()
                if data is not None
                else None,
            }
        )
    for p, data in blobs.items():
        current = (SOURCE / p).read_bytes() if (SOURCE / p).is_file() else None
        if current != data:
            raise RuntimeError(
                f"Source changed during capture: {p}; rerun before commit"
            )
        if data is not None and (TARGET / p).read_bytes() != data:
            raise RuntimeError(f"Target mismatch: {p}")
    if before_index != hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest():
        raise RuntimeError("Source index changed during capture")
    report = TARGET / "docs/development/platform-baseline-20261024.json"
    report.write_text(
        json.dumps(
            {
                "source": str(SOURCE),
                "branch": git("branch", "--show-current").decode().strip(),
                "head": git("rev-parse", "HEAD").decode().strip(),
                "index_sha256": before_index,
                "files": rows,
                "warning": "Development snapshot only; do not merge or cherry-pick this baseline.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Verified {len(rows)} source files; source index unchanged")


if __name__ == "__main__":
    main()
