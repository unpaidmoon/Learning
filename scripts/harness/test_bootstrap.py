"""Installer integrity boundaries with temporary synthetic artifacts only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from bootstrap import ensure_runtime


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class BootstrapIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="learning-bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "harness").mkdir()
        self.wheel = self.root / "synthetic.whl"
        self.exe = b"synthetic-not-executable"
        self.rg = b"synthetic-rg"
        self.license = b"synthetic-license"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("deepseek_harness_runtime/runtime/host.exe", self.exe)
            archive.writestr("deepseek_harness_runtime/runtime/rg.exe", self.rg)
            archive.writestr("deepseek_harness_runtime_bin-0.2.1rc2.dist-info/licenses/LICENSE", self.license)
        self.lock = {
            "artifact": {
                "version": "synthetic-test",
                "wheel": {"filename": self.wheel.name, "sha256": digest(self.wheel.read_bytes())},
                "executable": {"filename": "host.exe", "sha256": digest(self.exe)},
                "ripgrep": {"filename": "rg.exe", "sha256": digest(self.rg)},
            },
            "licenses": [{"filename": "LICENSE", "sha256": digest(self.license)}],
        }
        self.save_lock()

    def save_lock(self) -> None:
        (self.root / "harness/runtime.lock.json").write_text(json.dumps(self.lock), encoding="utf-8")

    def test_verified_archive_installs_then_reuses_offline_without_source(self) -> None:
        installed = ensure_runtime(self.root, self.wheel, allow_download=False)
        self.assertEqual(installed.read_bytes(), self.exe)
        self.wheel.unlink()
        self.assertEqual(ensure_runtime(self.root, allow_download=False), installed)

    def test_archive_hash_mismatch_never_publishes_runtime(self) -> None:
        self.wheel.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "HARNESS_ARTIFACT_INTEGRITY"):
            ensure_runtime(self.root, self.wheel, allow_download=False)
        self.assertFalse((self.root / ".runtime/harness/install").exists())

    def test_existing_tampered_runtime_is_preserved_and_rejected(self) -> None:
        installed = ensure_runtime(self.root, self.wheel, allow_download=False)
        installed.write_bytes(b"corrupt-runtime")
        with self.assertRaisesRegex(ValueError, "HARNESS_ARTIFACT_INTEGRITY"):
            ensure_runtime(self.root, allow_download=False)
        self.assertEqual(installed.read_bytes(), b"corrupt-runtime")

    def test_verified_zip_still_rejects_parent_path_before_publication(self) -> None:
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("deepseek_harness_runtime/runtime/../escape.txt", b"forbidden")
        self.lock["artifact"]["wheel"]["sha256"] = digest(self.wheel.read_bytes())
        self.save_lock()
        with self.assertRaisesRegex(ValueError, "HARNESS_UNSAFE_ARCHIVE_PATH"):
            ensure_runtime(self.root, self.wheel, allow_download=False)
        self.assertFalse((self.root / ".runtime/harness/install").exists())
        self.assertFalse((self.root / ".runtime/harness/escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
