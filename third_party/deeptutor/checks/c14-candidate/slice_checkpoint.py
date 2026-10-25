"""Original SliceCheckpoint excerpt; job/root/source authority is external.
Only a candidate, not part of the frozen runtime wheel. Valid hash != valid index.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
_MAX_ARCHIVE_BYTES=500*1024*1024

class SliceCheckpoint:
    def __init__(self, job: Path, start: int, end: int):
        self.directory = job / f"{start}-{end}"
        self.pointer = self.directory / "ready.json"

    def load(self) -> bytes | None:
        try:
            if self.pointer.is_symlink() or self.directory.is_symlink():
                return None
            info = json.loads(self.pointer.read_text(encoding="utf-8"))
            digest = info["sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                return None
            archive = self.directory / f"{digest}.zip"
            if archive.is_symlink() or archive.stat().st_size > _MAX_ARCHIVE_BYTES:
                return None
            data = archive.read_bytes()
            if hashlib.sha256(data).hexdigest() == digest:
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def save(self, archive: bytes) -> None:
        if len(archive) > _MAX_ARCHIVE_BYTES:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(archive).hexdigest()
        self._write(self.directory / f"{digest}.zip", archive)
        self._write(self.pointer, json.dumps({"sha256": digest}).encode())

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
