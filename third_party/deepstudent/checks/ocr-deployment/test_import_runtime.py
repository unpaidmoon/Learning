"""Owned synthetic deployment fixtures; intentionally preserve test directories."""

import unittest
import uuid

from import_runtime import (
    RECORD_NAME,
    TEST_ROOT,
    DeploymentError,
    FileEntry,
    RuntimeSpec,
    digest,
    import_runtime,
    safe_relative,
    verify,
)


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / ("unit-" + uuid.uuid4().hex)
        self.source = self.root / "source"
        self.resources = self.root / "resources"
        self.destination = self.root / "runtime"
        self.source.mkdir(parents=True)
        self.resources.mkdir()
        (self.source / "input.js").write_bytes(b"synthetic code fixture\n")
        (self.resources / "LICENSE.txt").write_bytes(b"synthetic notice fixture\n")
        self.entries = (
            FileEntry("node_modules/fixture/index.js", digest(b"synthetic code fixture\n"), len(b"synthetic code fixture\n"), "import_cache", "input.js"),
            FileEntry("notices/LICENSE.txt", digest(b"synthetic notice fixture\n"), len(b"synthetic notice fixture\n"), "resources", "LICENSE.txt"),
        )
        self.spec = RuntimeSpec("fixture-v1", "0" * 64, "1" * 64, self.resources, self.entries)

    def assert_error(self, code: str, operation) -> None:
        with self.assertRaises(DeploymentError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_valid_import_then_verify_without_import_cache(self) -> None:
        result = import_runtime(self.spec, self.source, self.destination)
        self.assertEqual(result["created_files"], 2)
        # verify不接收import路径，数据源不存在也仍能验证已安装闭包。
        (self.source / "input.js").rename(self.source / "input-moved.js")
        self.assertEqual(verify(self.spec, self.destination)["status"], "verified")

    def test_bad_source_is_rejected_before_any_destination_write(self) -> None:
        (self.source / "input.js").write_bytes(b"tampered")
        self.assert_error("OCR_SOURCE_FILE_MISMATCH", lambda: import_runtime(self.spec, self.source, self.destination))
        self.assertFalse(self.destination.exists())

    def test_mismatched_destination_is_not_overwritten_or_deleted(self) -> None:
        target = self.destination / "node_modules/fixture/index.js"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"preserve-existing-content")
        self.assert_error("OCR_RUNTIME_FILE_MISMATCH", lambda: import_runtime(self.spec, self.source, self.destination))
        self.assertEqual(target.read_bytes(), b"preserve-existing-content")
        self.assertFalse((self.destination / "notices").exists())

    def test_unmanaged_files_preserved_and_reported(self) -> None:
        self.destination.mkdir()
        user_file = self.destination / "preserve-me.txt"
        user_file.write_bytes(b"preserve")
        self.assert_error("OCR_UNMANAGED_DESTINATION_FILE", lambda: import_runtime(self.spec, self.source, self.destination))
        self.assertEqual(user_file.read_bytes(), b"preserve")

    def test_matching_partial_import_is_completed_and_repeat_is_readonly(self) -> None:
        target = self.destination / "node_modules/fixture/index.js"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"synthetic code fixture\n")
        initial_mtime = target.stat().st_mtime_ns
        self.assertEqual(import_runtime(self.spec, self.source, self.destination)["created_files"], 1)
        self.assertEqual(target.stat().st_mtime_ns, initial_mtime)
        self.assertEqual(import_runtime(self.spec, self.source, self.destination)["created_files"], 0)
        self.assertEqual(target.stat().st_mtime_ns, initial_mtime)

    def test_changed_install_record_is_not_overwritten(self) -> None:
        import_runtime(self.spec, self.source, self.destination)
        record = self.destination / RECORD_NAME
        record.write_bytes(b"other-installation")
        self.assert_error("OCR_INSTALL_RECORD_MISMATCH", lambda: import_runtime(self.spec, self.source, self.destination))
        self.assertEqual(record.read_bytes(), b"other-installation")

    def test_missing_installed_file_is_not_silently_recovered_by_verify(self) -> None:
        self.destination.mkdir()
        self.assert_error("OCR_RUNTIME_FILE_MISSING", lambda: verify(self.spec, self.destination))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_unsafe_paths_and_unowned_destination_are_rejected(self) -> None:
        for relative in ["../outside", "/outside", "C:/outside", "node_modules\\x", "a/./b", "a//b", "CON.txt", "name."]:
            with self.subTest(relative=relative):
                self.assert_error("OCR_LOCK_PATH_INVALID", lambda: safe_relative(relative))
        self.assert_error("OCR_DESTINATION_OUTSIDE_OWNED_ROOT", lambda: import_runtime(self.spec, self.source, self.resources.parent.parent.parent / "unowned-runtime"))


if __name__ == "__main__":
    unittest.main()
