"""User-scoped DPAPI storage. No credentials in business DTOs, logs or source files."""

import ctypes
import json
import os
from ctypes import wintypes

from learning_app.errors import AppError


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != "nt":
        raise AppError("PRIVATE_STORE_UNAVAILABLE", "账号凭据需要Windows用户私有存储", 503)
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = library.CryptUnprotectData if decrypt else library.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AppError("PRIVATE_STORE_FAILED", "私有凭据无法读取或保存，请重新连接", 503)
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.WinDLL("kernel32").LocalFree(target.data)


class PrivateStore:
    def __init__(self, root):
        self.root = root / "platform-private"

    def path(self, identity):
        if not identity.replace("_", "").isalnum():
            raise AppError("CONNECTION_INVALID", "连接身份无效", 422)
        return self.root / (identity + ".dpapi")

    def put(self, identity, value):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(identity)
        temp = target.with_suffix(".tmp")
        with temp.open("wb") as stream:
            stream.write(crypt(json.dumps(value).encode()))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)

    def get(self, identity):
        path = self.path(identity)
        return json.loads(crypt(path.read_bytes(), True)) if path.exists() else {}

    def remove(self, identity):
        self.path(identity).unlink(missing_ok=True)
