"""Actual Windows recycle proof with synthetic data; clipboard is never read."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from uuid import uuid4

import pytest

from learning_app.errors import AppError
from learning_app.native_files import _local_target, recycle_path


def test_recycle_target_refuses_disk_and_relative_paths(tmp_path):
    with pytest.raises(AppError) as relative:
        _local_target(Path("not-an-absolute-user-file.txt"))
    assert relative.value.code == "RECYCLE_PATH_INVALID"
    with pytest.raises(AppError) as root:
        _local_target(Path(tmp_path.anchor))
    assert root.value.code == "RECYCLE_PATH_INVALID"


@pytest.mark.skipif(os.name != "nt", reason="actual Windows IFileOperation Recycle Bin")
def test_real_synthetic_item_enters_recycle_bin(tmp_path):
    class RecycleInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("size", ctypes.c_int64), ("count", ctypes.c_int64)]

    def query(drive):
        shell = ctypes.OleDLL("shell32")
        shell.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(RecycleInfo)]
        shell.SHQueryRecycleBinW.restype = ctypes.c_long
        value = RecycleInfo()
        value.cbSize = ctypes.sizeof(value)
        assert shell.SHQueryRecycleBinW(drive, ctypes.byref(value)) >= 0
        return value.size, value.count

    source = tmp_path / f"codex-synthetic-recycle-{uuid4().hex}.txt"
    content = b"Explicit synthetic recycle proof; no user data."
    source.write_bytes(content)
    size_before, count_before = query(source.anchor)
    recycle_path(source)
    size_after, count_after = query(source.anchor)
    assert not source.exists()
    assert count_after >= count_before + 1
    assert size_after >= size_before + len(content)
    # The deliberate test item stays recoverable. Never empty the user's bin or
    # enumerate/read existing recycled filenames or original-path metadata.
