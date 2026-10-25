"""Freeze a read-only mainline comparison and preview changes inside this worktree."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "68c9cbcf7e96ef390ba51bd9e143ae75113f46dd"
PUBLIC_ROOTS = {
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


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args])


def manifest(source):
    tracked = set(git(source, "ls-files", "-z").decode().split("\0")) - {""}
    extra = set(
        git(source, "ls-files", "--others", "--exclude-standard", "-z")
        .decode()
        .split("\0")
    ) - {""}
    extra = {p for p in extra if p.split("/")[0] in PUBLIC_ROOTS or p.endswith(".cmd")}
    extra = {
        p
        for p in extra
        if "/checks/" not in p and not p.startswith("research/reports/")
    }
    paths = sorted(tracked | extra)
    for name in paths:
        parts = Path(name).parts
        if any(
            p in {"..", ".local", ".runtime", ".tools", "node_modules", ".venv"}
            for p in parts
        ):
            raise RuntimeError("PRIVATE_OR_UNSAFE_MANIFEST_PATH")
        if not (source / name).resolve().is_relative_to(source):
            raise RuntimeError("MANIFEST_PATH_OUTSIDE_SOURCE")
    return paths


def sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read(source, name):
    path = source / name
    return path.read_bytes() if path.is_file() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("D:/Codex/Learning"))
    args = parser.parse_args()
    source = args.source.resolve()
    if source == ROOT:
        raise RuntimeError("SEPARATE_SOURCE_REQUIRED")
    previous = json.loads(
        (ROOT / "docs/development/platform-baseline-20261024.json").read_text("utf-8")
    )
    before = git(source, "ls-files", "--stage", "-z")
    head = git(source, "rev-parse", "HEAD").decode().strip()
    paths = manifest(source)
    blobs = {name: read(source, name) for name in paths}
    for name, data in blobs.items():
        if read(source, name) != data:
            raise RuntimeError("CONCURRENT_SOURCE_CHANGE: " + name)
    if manifest(source) != paths or before != git(source, "ls-files", "--stage", "-z"):
        raise RuntimeError("CONCURRENT_SOURCE_MANIFEST_OR_INDEX_CHANGE")
    directory = ROOT / ".runtime/platforms/mainline-review" / uuid4().hex
    directory.mkdir(parents=True)
    old = {item["path"]: item["sha256"] for item in previous["files"]}
    rows = []
    for name in sorted(set(paths) | set(old)):
        data = blobs.get(name)
        if sha(data) == old.get(name):
            continue
        ours = read(ROOT, name)
        row = {
            "path": name,
            "old_sha256": old.get(name),
            "source_sha256": sha(data),
            "platform_sha256": sha(ours),
            "platform_changed": sha(ours) != old.get(name),
        }
        for label, value in [("source", data), ("platform", ours)]:
            if value is not None:
                target = directory / label / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value)
        if data is not None and ours is not None and old.get(name):
            base = git(ROOT, "show", BASELINE + ":" + name)
            try:
                values = [
                    value.decode("utf-8").replace("\r\n", "\n").encode()
                    for value in (ours, base, data)
                ]
            except UnicodeError:
                row["merge"] = "binary-review"
            else:
                operands = []
                for label, value in zip(
                    ("ours", "base", "theirs"), values, strict=True
                ):
                    target = directory / "operands" / label / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(value)
                    operands.append(str(target))
                result = subprocess.run(
                    ["git", "merge-file", "--stdout", *operands], capture_output=True
                )
                # git merge-file reports the number of conflicts, capped at 127.
                if not 0 <= result.returncode <= 127:
                    raise RuntimeError("MERGE_PREVIEW_FAILED: " + name)
                target = directory / "preview" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(result.stdout)
                row["merge"] = "clean" if result.returncode == 0 else "conflict"
        else:
            row["merge"] = "addition-or-deletion-review"
        rows.append(row)
    evidence = {
        "source_head": head,
        "platform_head": git(ROOT, "rev-parse", "HEAD").decode().strip(),
        "source_index_sha256": sha(before),
        "index_matches_baseline": sha(before) == previous["index_sha256"],
        "source_manifest_files": len(paths),
        "changed": rows,
    }
    (directory / "audit.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run": directory.name,
                "source_manifest_files": len(paths),
                "changed": len(rows),
                "overlap": sum(r["platform_changed"] for r in rows),
                "conflicts": [r["path"] for r in rows if r["merge"] == "conflict"],
                "index_matches_baseline": evidence["index_matches_baseline"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
