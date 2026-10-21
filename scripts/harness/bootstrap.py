"""Install only the verified, pinned Windows Harness carrier into this project.

This module has no import side effects and no dependency on research probes.
The optional wheel argument also supports an independently verified offline cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.request
import uuid
import zipfile


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected: str) -> None:
    if not path.is_file() or _digest(path) != expected:
        raise ValueError(f"HARNESS_ARTIFACT_INTEGRITY: {path.name}")


def _publish_cache(temporary: Path, cached: Path, expected: str) -> None:
    """Accept a concurrent valid publication without overwriting open files."""
    if cached.exists():
        _verify(cached, expected)
        return
    try:
        os.replace(temporary, cached)
    except (PermissionError, FileExistsError):
        # Windows can refuse replacement while another bootstrap is reading the
        # wheel. A verified winner is sufficient; an invalid winner still fails.
        _verify(cached, expected)


def ensure_runtime(
    repo_root: Path | str | None = None,
    wheel_path: Path | str | None = None,
    *,
    allow_download: bool = True,
) -> Path:
    """Return a verified formal executable, extracting a wheel only if needed."""
    root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
    lock = json.loads((root / "harness/runtime.lock.json").read_text(encoding="utf-8"))
    artifact = lock["artifact"]
    install_dir = root / ".runtime/harness/install"
    executable = install_dir / artifact["executable"]["filename"]
    if executable.exists():
        for item in (artifact["executable"], artifact["ripgrep"]):
            _verify(install_dir / item["filename"], item["sha256"])
        for item in lock["licenses"]:
            _verify(install_dir / "licenses" / item["filename"], item["sha256"])
        return executable

    cache_dir = root / ".local/cache/harness"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / artifact["wheel"]["filename"]
    if wheel_path is not None:
        source = Path(wheel_path).resolve()
        _verify(source, artifact["wheel"]["sha256"])
        if source != cached:
            temporary = cache_dir / f"{cached.name}.{uuid.uuid4().hex}.partial"
            try:
                shutil.copyfile(source, temporary)
                _verify(temporary, artifact["wheel"]["sha256"])
                _publish_cache(temporary, cached, artifact["wheel"]["sha256"])
            finally:
                temporary.unlink(missing_ok=True)
    if not cached.is_file():
        if not allow_download:
            raise FileNotFoundError("HARNESS_WHEEL_NOT_CACHED: supply --wheel or allow download")
        temporary = cache_dir / f"{cached.name}.{uuid.uuid4().hex}.partial"
        try:
            request = urllib.request.Request(artifact["wheel"]["url"], headers={"User-Agent": "learning-harness-bootstrap/1"})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as target:
                shutil.copyfileobj(response, target)
            _verify(temporary, artifact["wheel"]["sha256"])
            _publish_cache(temporary, cached, artifact["wheel"]["sha256"])
        finally:
            temporary.unlink(missing_ok=True)
    _verify(cached, artifact["wheel"]["sha256"])

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=install_dir.parent) as directory:
        staging = Path(directory) / "payload"
        staging.mkdir()
        prefix = "deepseek_harness_runtime/runtime/"
        license_prefix = "deepseek_harness_runtime_bin-0.2.1rc2.dist-info/licenses/"
        with zipfile.ZipFile(cached) as archive:
            for member in archive.infolist():
                name = member.filename
                if name.startswith(prefix):
                    relative = name[len(prefix):]
                elif name.startswith(license_prefix):
                    relative = "licenses/" + name[len(license_prefix):]
                elif name == "deepseek_harness_runtime/deepseek-harness-runtime.json":
                    relative = "deepseek-harness-runtime.json"
                else:
                    continue
                parts = PurePosixPath(relative)
                if parts.is_absolute() or any(part in {"..", "."} or ":" in part for part in parts.parts):
                    raise ValueError("HARNESS_UNSAFE_ARCHIVE_PATH")
                destination = staging.joinpath(*parts.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
        for item in (artifact["executable"], artifact["ripgrep"]):
            _verify(staging / item["filename"], item["sha256"])
        for item in lock["licenses"]:
            _verify(staging / "licenses" / item["filename"], item["sha256"])
        (staging / "installation.json").write_text(json.dumps({
            "version": artifact["version"], "wheel_sha256": artifact["wheel"]["sha256"],
            "executable_sha256": artifact["executable"]["sha256"],
            "source": "verified wheel cache", "downloaded_optional_resources": False,
        }, indent=2) + "\n", encoding="utf-8")
        try:
            os.replace(staging, install_dir)
        except (PermissionError, FileExistsError):
            # Another installer may have published while this one extracted.
            # Never delete its directory or any unknown existing contents.
            for item in (artifact["executable"], artifact["ripgrep"]):
                _verify(install_dir / item["filename"], item["sha256"])
            for item in lock["licenses"]:
                _verify(install_dir / "licenses" / item["filename"], item["sha256"])
    return executable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    executable = ensure_runtime(args.repo_root, args.wheel, allow_download=not args.offline)
    print(json.dumps({"executable": str(executable), "verified": True}))


if __name__ == "__main__":
    main()
