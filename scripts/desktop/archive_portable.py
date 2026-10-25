"""Archive only a verified release manifest, excluding caches created by use."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from portable import verify_release


def main():
    parser = argparse.ArgumentParser(description="将已校验运行目录封装为固定 ZIP64 包")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    manifest = verify_release(root)
    if args.output.exists() or args.output.resolve().is_relative_to(root):
        raise RuntimeError("PORTABLE_ARCHIVE_OUTPUT_DENIED")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        args.output, "x", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as packed:
        for name in [*manifest["files"], "desktop-release.json"]:
            packed.write(root / name, manifest["release_id"] + "/" + name)
    checked = 0
    with zipfile.ZipFile(args.output) as packed:
        expected = {
            manifest["release_id"] + "/" + name
            for name in [*manifest["files"], "desktop-release.json"]
        }
        if set(packed.namelist()) != expected or len(packed.namelist()) != len(
            expected
        ):
            raise RuntimeError("PORTABLE_ARCHIVE_MEMBERS_INVALID")
        for name, spec in manifest["files"].items():
            with packed.open(manifest["release_id"] + "/" + name) as incoming:
                actual = hashlib.file_digest(incoming, "sha256").hexdigest()
            if actual != spec["sha256"]:
                raise RuntimeError("PORTABLE_ARCHIVE_HASH_MISMATCH: " + name)
            checked += 1
    with args.output.open("rb") as incoming:
        sha256 = hashlib.file_digest(incoming, "sha256").hexdigest()
    record = {
        "status": "passed",
        "archive": str(args.output.resolve()),
        "bytes": args.output.stat().st_size,
        "sha256": sha256,
        "checked_files": checked,
        "release_id": manifest["release_id"],
    }
    args.output.with_suffix(args.output.suffix + ".json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
