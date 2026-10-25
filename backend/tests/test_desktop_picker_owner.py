"""The desktop picker accepts only its private, live Electron owner."""

import ctypes
import json
import os
import time
from ctypes import wintypes

import pytest

from learning_app.errors import AppError
from learning_app.native_files import desktop_picker_owner


def test_browser_only_launch_has_no_desktop_window_authority(monkeypatch):
    monkeypatch.delenv("LEARNING_DESKTOP_OWNER_FILE", raising=False)
    assert desktop_picker_owner() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows native window ownership")
@pytest.mark.parametrize("content", [None, "broken-json", '{"hwnd":0,"pid":1,"registered_at_ms":1}'])
def test_missing_or_malformed_private_owner_fails_without_a_dialog(tmp_path, monkeypatch, content):
    owner = tmp_path / "native-owner.json"
    if content is not None:
        owner.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LEARNING_DESKTOP_OWNER_FILE", str(owner))
    with pytest.raises(AppError) as failure:
        desktop_picker_owner()
    assert failure.value.code == "PICKER_OWNER_UNAVAILABLE"


@pytest.mark.skipif(os.name != "nt", reason="Windows native window ownership")
def test_a_live_non_electron_window_cannot_be_registered(tmp_path, monkeypatch):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.DestroyWindow.restype = wintypes.BOOL
    handle = user.CreateWindowExW(0, "STATIC", "Owned synthetic test", 0, 0, 0, 10, 10,
                                 None, None, None, None)
    assert handle
    try:
        owner = tmp_path / "native-owner.json"
        owner.write_text(json.dumps({
            "hwnd": str(handle), "pid": os.getpid(), "registered_at_ms": int(time.time() * 1000),
        }), encoding="utf-8")
        monkeypatch.setenv("LEARNING_DESKTOP_OWNER_FILE", str(owner))
        with pytest.raises(AppError) as failure:
            desktop_picker_owner()
        assert failure.value.code == "PICKER_OWNER_UNAVAILABLE"
        assert not user.IsWindowVisible(handle)
    finally:
        assert user.DestroyWindow(handle)
