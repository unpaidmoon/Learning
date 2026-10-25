"""Integrity and archive scope checks for desktop preparation, without network."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from bootstrap import fetch_verified, unpack


class DesktopBootstrapTests(unittest.TestCase):
    def test_verified_cache_works_offline_and_corruption_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "runtime.zip"
            cached.write_bytes(b"small offline fixture")
            spec = {"url": "https://invalid.test/never-requested", "sha256": hashlib.sha256(cached.read_bytes()).hexdigest()}
            self.assertEqual(fetch_verified(spec, cached), cached)
            cached.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "DESKTOP_CACHE_HASH_MISMATCH"):
                fetch_verified(spec, cached)

    def test_archive_path_cannot_escape_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.txt", "must not be published")
            with self.assertRaisesRegex(RuntimeError, "DESKTOP_UNSAFE_ARCHIVE_PATH"):
                unpack(archive, root / "prepared")
            self.assertFalse((root / "escape.txt").exists())

    def test_source_archive_removes_only_declared_outer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("source-commit/apps/desktop/main.ts", "fixture source")
            unpack(archive, root / "prepared", strip_root=True)
            self.assertEqual((root / "prepared/apps/desktop/main.ts").read_text(), "fixture source")


if __name__ == "__main__":
    unittest.main()
