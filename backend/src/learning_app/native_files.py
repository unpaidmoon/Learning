"""Windows file actions invoked only by explicit local browser gestures.

No clipboard polling and no permanent-deletion fallback are provided. Recycle
uses IFileOperation's Windows 8+ FOFX_RECYCLEONDELETE contract.
"""

from __future__ import annotations

import ctypes
import json
import os
import uuid
from pathlib import Path

from learning_app.errors import AppError


def desktop_picker_owner(owner_path: Path | None = None) -> int | None:
    """Resolve only this launch's private Electron owner, never a renderer HWND.

    The desktop runner supplies the private file path to both owned processes.
    A browser-only launch has no such file and keeps its existing unowned flow.
    Stale handles, PID reuse, and another executable fail before opening a UI.
    """
    owner_file = owner_path or os.environ.get("LEARNING_DESKTOP_OWNER_FILE")
    if not owner_file:
        return None
    if os.name != "nt":
        raise AppError("PICKER_OWNER_UNAVAILABLE", "当前系统不支持桌面窗口关联", 503)
    from ctypes import wintypes

    try:
        value = json.loads(Path(owner_file).read_text("utf-8"))
        hwnd, pid = int(value["hwnd"]), int(value["pid"])
        registered_at_ms = int(value["registered_at_ms"])
        if hwnd <= 0 or pid <= 0 or registered_at_ms <= 0:
            raise ValueError("invalid desktop owner")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AppError(
            "PICKER_OWNER_UNAVAILABLE", "桌面窗口尚未就绪，请重新打开窗口后选择", 503
        ) from exc

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.BringWindowToTop.argtypes = [wintypes.HWND]
    user.BringWindowToTop.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.GetProcessTimes.restype = wintypes.BOOL

    def denied():
        return AppError("PICKER_OWNER_UNAVAILABLE", "桌面窗口已失效，请重新打开窗口后选择", 503)

    actual_pid = wintypes.DWORD()
    if not user.IsWindow(hwnd):
        raise denied()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(actual_pid))
    if actual_pid.value != pid:
        raise denied()
    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION.
    if not process:
        raise denied()
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            raise denied()
        expected = Path(__file__).resolve().parents[3] / ".runtime/desktop/electron/electron.exe"
        if Path(image.value).resolve() != expected.resolve():
            raise denied()
        if not kernel.GetProcessTimes(
            process, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(cpu_kernel), ctypes.byref(cpu_user),
        ):
            raise denied()
        created_ms = ((created.dwHighDateTime << 32) | created.dwLowDateTime) // 10_000 - 11_644_473_600_000
        if created_ms > registered_at_ms:
            raise denied()
    finally:
        kernel.CloseHandle(process)
    # The Electron main process focuses the owned window before admitting the
    # click's HTTP request. These calls also restore its minimize/hide state;
    # IFileDialog.Show(hwnd) then gives Windows actual modal-owner semantics.
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)  # SW_RESTORE.
    else:
        user.ShowWindow(hwnd, 5)  # SW_SHOW.
    user.BringWindowToTop(hwnd)
    user.SetForegroundWindow(hwnd)
    return hwnd


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _local_target(path: Path) -> Path:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise AppError("RECYCLE_PATH_INVALID", "只能将本机课程资料移入回收站", 422)
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise AppError("FILE_LINK_DENIED", "不能处理符号链接或目录联接", 422)
    resolved = path.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise AppError("RECYCLE_PATH_INVALID", "不能将磁盘根目录移入回收站", 422)
    return resolved


def pick_paths(*, directory: bool) -> list[str]:
    """Use Windows' Common Item Dialog on this worker's STA.

    Python distributions do not necessarily bundle Tcl/Tk's runtime data. Keep
    the native UI independent of it, and balance COM ownership on every exit.
    """
    if os.name != "nt":
        raise AppError("PICKER_UNAVAILABLE", "当前系统不支持 Windows 文件选择", 503)
    from ctypes import wintypes

    ole = ctypes.WinDLL("ole32")
    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    ole.CoInitializeEx.argtypes = [pointer, wintypes.DWORD]
    ole.CoInitializeEx.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(_Guid), pointer, wintypes.DWORD,
                                    ctypes.POINTER(_Guid), ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype = hresult
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None

    def call(obj, index, argtypes, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(hresult, pointer, *argtypes)(table[index])(obj, *args)

    def check(value: int) -> None:
        if value < 0:
            raise OSError(f"Windows file dialog HRESULT 0x{value & 0xFFFFFFFF:08X}")

    initialized = False
    dialog, results = pointer(), pointer()
    try:
        # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE; never share COM
        # objects between HTTP workers. Show supplies the modal message loop.
        check(ole.CoInitializeEx(None, 0x2 | 0x4))
        initialized = True
        clsid = _Guid.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid = _Guid.parse("D57C7288-D4AD-4768-BE02-9D969532D960")
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                   ctypes.byref(iid), ctypes.byref(dialog)))
        options = wintypes.DWORD()
        check(call(dialog, 10, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options)))
        # FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT.
        flags = options.value | 0x40 | 0x800 | 0x8 | 0x02000000
        flags |= 0x20 if directory else 0x1000 | 0x200  # PICKFOLDERS / multi-file.
        check(call(dialog, 9, [wintypes.DWORD], flags))
        title = "选择课程根文件夹" if directory else "选择复制到当前课程的文件"
        check(call(dialog, 17, [wintypes.LPCWSTR], title))
        shown = call(dialog, 3, [wintypes.HWND], desktop_picker_owner())
        if shown & 0xFFFFFFFF == 0x800704C7:  # HRESULT_FROM_WIN32(ERROR_CANCELLED).
            return []
        check(shown)
        check(call(dialog, 27, [ctypes.POINTER(pointer)], ctypes.byref(results)))
        count = wintypes.DWORD()
        check(call(results, 7, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(count)))
        if count.value > 1024:
            raise AppError("FILE_BATCH_TOO_LARGE", "一次最多选择 1024 个文件", 422)
        selected = []
        for index in range(count.value):
            item, text = pointer(), pointer()
            try:
                check(call(results, 8, [wintypes.DWORD, ctypes.POINTER(pointer)],
                           index, ctypes.byref(item)))
                check(call(item, 5, [wintypes.DWORD, ctypes.POINTER(pointer)],
                           0x80058000, ctypes.byref(text)))  # SIGDN_FILESYSPATH.
                selected.append(ctypes.wstring_at(text))
            finally:
                if text:
                    ole.CoTaskMemFree(text)
                if item:
                    call(item, 2, [])
        return selected
    finally:
        for obj in (results, dialog):
            if obj:
                call(obj, 2, [])
        if initialized:
            ole.CoUninitialize()


def read_clipboard_paths() -> list[str]:
    """Read CF_HDROP once, after the user explicitly chooses Paste."""
    if os.name != "nt":
        raise AppError("CLIPBOARD_FILES_UNAVAILABLE", "当前系统不支持读取文件剪贴板", 503)
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    user.CloseClipboard.argtypes = []
    user.CloseClipboard.restype = wintypes.BOOL
    shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell.DragQueryFileW.restype = wintypes.UINT
    if not user.OpenClipboard(None):
        raise AppError("CLIPBOARD_BUSY", "剪贴板暂不可用，请稍后重新粘贴", 409)
    try:
        handle = user.GetClipboardData(15)  # CF_HDROP; never read text or credentials.
        if not handle:
            raise AppError("CLIPBOARD_FILES_EMPTY", "剪贴板中没有可复制的文件", 422)
        count = shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        if not count:
            raise AppError("CLIPBOARD_FILES_EMPTY", "剪贴板中没有可复制的文件", 422)
        if count > 1024:
            raise AppError("FILE_BATCH_TOO_LARGE", "一次最多粘贴 1024 个文件或文件夹", 422)
        values = []
        for index in range(count):
            size = shell.DragQueryFileW(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(size + 1)
            shell.DragQueryFileW(handle, index, buffer, size + 1)
            values.append(buffer.value)
        return values
    finally:
        user.CloseClipboard()


def recycle_path(path: Path) -> None:
    """Send one checked local file/folder to Windows Recycle Bin, or fail."""
    path = _local_target(path)
    if os.name != "nt":
        raise AppError("RECYCLE_UNAVAILABLE", "当前系统不支持 Windows 回收站", 503)
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor) != 3:  # DRIVE_FIXED only; no network/removable fallback.
        raise AppError("RECYCLE_VOLUME_UNSUPPORTED", "此磁盘不支持安全回收，未执行永久删除", 422)
    ole = ctypes.OleDLL("ole32")
    shell = ctypes.OleDLL("shell32")
    hresult = ctypes.c_long
    pointer = ctypes.c_void_p
    ole.CoInitializeEx.argtypes = [pointer, wintypes.DWORD]
    ole.CoInitializeEx.restype = hresult
    ole.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(pointer),
    ]
    ole.CoCreateInstance.restype = hresult
    shell.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR,
        pointer,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(pointer),
    ]
    shell.SHCreateItemFromParsingName.restype = hresult

    def check(value: int) -> None:
        if value < 0:
            raise AppError("FILE_RECYCLE_FAILED", "未能移入回收站，原资料不会改为永久删除", 409)

    def call(obj, index, argtypes, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(hresult, pointer, *argtypes)(table[index])(obj, *args)

    operation, item = pointer(), pointer()
    initialized = False
    try:
        check(ole.CoInitializeEx(None, 2))  # STA, confined to this calling worker.
        initialized = True
        clsid = _Guid.parse("3ad05575-8857-4850-9277-11b85bdb8e09")
        iid_operation = _Guid.parse("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8")
        iid_item = _Guid.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
        check(
            ole.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid_operation), ctypes.byref(operation)
            )
        )
        check(
            shell.SHCreateItemFromParsingName(
                str(path), None, ctypes.byref(iid_item), ctypes.byref(item)
            )
        )
        # Silent, no confirmation/error UI; recycle is explicit, and early
        # failure prevents a skipped action from masquerading as completion.
        flags = 0x0004 | 0x0010 | 0x0040 | 0x0400 | 0x00080000 | 0x00100000 | 0x20000000
        check(call(operation, 5, [wintypes.DWORD], flags))
        check(call(operation, 18, [pointer, pointer], item, None))
        check(call(operation, 21, []))
        aborted = wintypes.BOOL()
        check(call(operation, 22, [ctypes.POINTER(wintypes.BOOL)], ctypes.byref(aborted)))
        if aborted.value or path.exists():
            raise AppError("FILE_RECYCLE_FAILED", "资料尚未移入回收站，请核对后重新操作", 409)
    except OSError as exc:
        raise AppError(
            "FILE_RECYCLE_FAILED", "Windows 回收站操作不可用，未执行永久删除", 503
        ) from exc
    finally:
        for value in (item, operation):
            if value:
                call(value, 2, [])
        if initialized:
            ole.CoUninitialize()
