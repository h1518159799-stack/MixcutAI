"""Encrypted local OAuth token storage. Passwords are never accepted or stored."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect(value: str) -> str:
    raw = str(value or "").encode("utf-8")
    if not raw:
        return ""
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows DPAPI")
    source, keepalive = _blob(raw)
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "Mixcut OAuth", None, None, None, 0, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def unprotect(value: str) -> str:
    if not value:
        return ""
    encrypted = base64.b64decode(value)
    source, keepalive = _blob(encrypted)
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
