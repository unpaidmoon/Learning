"""Explicit, hash-checked OCR resource import. No download, npm, scripts or deletion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[4]
RESOURCES = REPO / "resources/workbench"
DEFAULT_DESTINATION = REPO / ".runtime/workbench/ocr"
TEST_ROOT = REPO / ".local/cache/workbench/ocr-deployment-tests"
MAX_RUNTIME_BYTES = 128 * 1024 * 1024
RECORD_NAME = ".ocr-runtime-install.json"


class DeploymentError(Exception):
    def __init__(self, code: str, relative_path: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.relative_path = relative_path


@dataclass(frozen=True)
class FileEntry:
    install_path: str
    sha256: str
    bytes: int
    source_kind: str
    source_relative: str


@dataclass(frozen=True)
class RuntimeSpec:
    release_id: str
    manifest_sha256: str
    package_lock_sha256: str
    resource_root: Path
    files: tuple[FileEntry, ...]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise DeploymentError("OCR_LOCK_PATH_INVALID")
    parts = PurePosixPath(value)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value != parts.as_posix() or parts.is_absolute() or any(part in {".", ".."} for part in parts.parts) or "\\" in value or ":" in value or "\0" in value:
        raise DeploymentError("OCR_LOCK_PATH_INVALID")
    if any(part.rstrip(". ") != part or part.split(".")[0].upper() in reserved for part in parts.parts):
        raise DeploymentError("OCR_LOCK_PATH_INVALID")
    return parts.as_posix()


def within(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(safe_relative(relative)).parts)
    path = candidate.resolve()
    if path != candidate.absolute() or not path.is_relative_to(root.resolve()):
        raise DeploymentError("OCR_PATH_OUTSIDE_ROOT", relative)
    return path


def load_spec(resource_root: Path = RESOURCES) -> RuntimeSpec:
    raw = (resource_root / "ocr-runtime.lock.json").read_bytes()
    document = json.loads(raw)
    if document.get("manifest_version") != 1 or document.get("package_count") != 13:
        raise DeploymentError("OCR_LOCK_INVALID")
    package_lock = (resource_root / "ocr-package-lock.json").read_bytes()
    if digest(package_lock) != document["package_lock"]["sha256"]:
        raise DeploymentError("OCR_PACKAGE_LOCK_MISMATCH")
    package_document = json.loads(package_lock)
    locked = package_document["packages"]
    for package in document["packages"]:
        pinned = locked[package["install_path"]]
        if (pinned["version"], pinned["resolved"], pinned["integrity"]) != (package["version"], package["url"], package["integrity"]):
            raise DeploymentError("OCR_PACKAGE_LOCK_MISMATCH")
    entries = []
    seen = set()
    for item in document["files"]:
        install = safe_relative(item["install_path"])
        source = safe_relative(item["source_relative"])
        if install.casefold() in seen or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise DeploymentError("OCR_LOCK_INVALID")
        seen.add(install.casefold())
        if item["source_kind"] not in {"resources", "import_cache"} or not isinstance(item["bytes"], int) or item["bytes"] < 0:
            raise DeploymentError("OCR_LOCK_INVALID")
        entries.append(FileEntry(install, item["sha256"], item["bytes"], item["source_kind"], source))
    if len(entries) != document["file_count"] or sum(item.bytes for item in entries) > MAX_RUNTIME_BYTES:
        raise DeploymentError("OCR_LOCK_INVALID")
    return RuntimeSpec(document["release_id"], digest(raw), digest(package_lock), resource_root.resolve(), tuple(entries))


def checked_destination(destination: Path) -> Path:
    result = destination.resolve()
    if DEFAULT_DESTINATION.resolve() != DEFAULT_DESTINATION.absolute() or TEST_ROOT.resolve() != TEST_ROOT.absolute() or not result.is_relative_to(REPO.resolve()):
        raise DeploymentError("OCR_DESTINATION_OUTSIDE_OWNED_ROOT")
    if not (result.is_relative_to(DEFAULT_DESTINATION.resolve()) or result.is_relative_to(TEST_ROOT.resolve())):
        raise DeploymentError("OCR_DESTINATION_OUTSIDE_OWNED_ROOT")
    if result == TEST_ROOT.resolve():
        raise DeploymentError("OCR_DESTINATION_OUTSIDE_OWNED_ROOT")
    return result


def expected_record(spec: RuntimeSpec) -> bytes:
    record = {"manifest_version": 1, "release_id": spec.release_id, "runtime_lock_sha256": spec.manifest_sha256,
              "package_lock_sha256": spec.package_lock_sha256, "file_count": len(spec.files),
              "install_bytes": sum(item.bytes for item in spec.files), "complete": True,
              "runtime_uses_import_cache": False, "install_scripts_executed": False}
    return (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")


def preflight_destination(spec: RuntimeSpec, destination: Path) -> set[str]:
    known = {item.install_path: item for item in spec.files}
    existing = set()
    if destination.exists():
        for path in destination.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(destination).as_posix()
            if not path.resolve().is_relative_to(destination.resolve()):
                raise DeploymentError("OCR_PATH_OUTSIDE_ROOT", relative)
            if relative == RECORD_NAME:
                if path.read_bytes() != expected_record(spec):
                    raise DeploymentError("OCR_INSTALL_RECORD_MISMATCH")
                continue
            item = known.get(relative)
            if item is None:
                raise DeploymentError("OCR_UNMANAGED_DESTINATION_FILE", relative)
            content = path.read_bytes()
            if len(content) != item.bytes or digest(content) != item.sha256:
                raise DeploymentError("OCR_RUNTIME_FILE_MISMATCH", relative)
            existing.add(relative)
    return existing


def verify(spec: RuntimeSpec, destination: Path) -> dict[str, object]:
    destination = checked_destination(destination)
    existing = preflight_destination(spec, destination)
    missing = {item.install_path for item in spec.files} - existing
    if missing:
        raise DeploymentError("OCR_RUNTIME_FILE_MISSING", sorted(missing)[0])
    if not (destination / RECORD_NAME).is_file():
        raise DeploymentError("OCR_INSTALL_RECORD_MISSING")
    return {"status": "verified", "release_id": spec.release_id, "destination": str(destination),
            "file_count": len(spec.files), "runtime_lock_sha256": spec.manifest_sha256}


def import_runtime(spec: RuntimeSpec, import_from: Path, destination: Path) -> dict[str, object]:
    destination = checked_destination(destination)
    source_root = import_from.resolve()
    allowed_cache = (REPO / ".local/cache/workbench").resolve()
    if not source_root.is_dir() or not source_root.is_relative_to(allowed_cache) or source_root == allowed_cache:
        raise DeploymentError("OCR_IMPORT_SOURCE_INVALID")
    existing = preflight_destination(spec, destination)
    # 校验所有源后才写目标，固定总量上限，避免前半已写后才发现源被篡改。
    staged: dict[str, bytes] = {}
    for item in spec.files:
        source_base = source_root if item.source_kind == "import_cache" else spec.resource_root
        source = within(source_base, item.source_relative)
        if not source.is_file():
            raise DeploymentError("OCR_SOURCE_FILE_MISSING", item.install_path)
        content = source.read_bytes()
        if len(content) != item.bytes or digest(content) != item.sha256:
            raise DeploymentError("OCR_SOURCE_FILE_MISMATCH", item.install_path)
        if item.install_path not in existing:
            staged[item.install_path] = content
    destination.mkdir(parents=True, exist_ok=True)
    for relative, content in staged.items():
        target = within(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 独占创建：并发出现文件时拒绝，不覆盖原件；未知中断保持现场。
        try:
            with target.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as exc:
            raise DeploymentError("OCR_DESTINATION_CHANGED", relative) from exc
    record = destination / RECORD_NAME
    if not record.exists():
        with record.open("xb") as output:
            output.write(expected_record(spec))
            output.flush()
            os.fsync(output.fileno())
    result = verify(spec, destination)
    result["status"] = "imported" if staged else "already_verified"
    result["created_files"] = len(staged)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-from", type=Path)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        spec = load_spec()
        if args.verify_only:
            if args.import_from is not None:
                raise DeploymentError("OCR_IMPORT_ARGUMENTS_INVALID")
            result = verify(spec, args.destination)
        else:
            if args.import_from is None:
                raise DeploymentError("OCR_IMPORT_SOURCE_REQUIRED")
            result = import_runtime(spec, args.import_from, args.destination)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except DeploymentError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "relative_path": exc.relative_path}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "failed", "code": "OCR_IMPORT_IO_OR_LOCK_INVALID"}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
