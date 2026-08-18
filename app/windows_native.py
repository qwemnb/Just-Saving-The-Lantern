"""Stable, process-wide ctypes declarations for Helios Room's Windows APIs.

The DLL objects exposed by ``ctypes.windll`` cache function objects.  Their
``argtypes`` and ``restype`` attributes are therefore shared mutable state.
This module gives every native function one canonical signature and configures
each concrete function object at most once under a lock.  No DLL entry point is
resolved while importing this module, so non-Windows imports remain safe.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


class WindowsNativeBindingError(RuntimeError):
    """A Windows DLL binding could not be initialized or was later changed."""


class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    ]


class FILE_MODE_INFORMATION(ctypes.Structure):
    _fields_ = [("Mode", wintypes.ULONG)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class FILE_ID_EXTD_DIR_INFO(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ReparsePointTag", wintypes.DWORD),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


@dataclass(frozen=True)
class _Signature:
    dll_name: str
    function_name: str
    argtypes: tuple[Any, ...]
    restype: Any


@dataclass(frozen=True)
class _BoundFunction:
    function: Any
    argtypes_object: Any
    signature: _Signature


_SIGNATURES = {
    "get_file_information_by_handle_ex": _Signature(
        "kernel32",
        "GetFileInformationByHandleEx",
        (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD),
        wintypes.BOOL,
    ),
    "create_file": _Signature(
        "kernel32",
        "CreateFileW",
        (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ),
        wintypes.HANDLE,
    ),
    "close_handle": _Signature(
        "kernel32", "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL
    ),
    "get_last_error": _Signature(
        "kernel32", "GetLastError", (), wintypes.DWORD
    ),
    "flush_file_buffers": _Signature(
        "kernel32", "FlushFileBuffers", (wintypes.HANDLE,), wintypes.BOOL
    ),
    "set_file_information_by_handle": _Signature(
        "kernel32",
        "SetFileInformationByHandle",
        (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD),
        wintypes.BOOL,
    ),
    "lock_file_ex": _Signature(
        "kernel32",
        "LockFileEx",
        (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ),
        wintypes.BOOL,
    ),
    "unlock_file_ex": _Signature(
        "kernel32",
        "UnlockFileEx",
        (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ),
        wintypes.BOOL,
    ),
    "nt_create_file": _Signature(
        "ntdll",
        "NtCreateFile",
        (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(OBJECT_ATTRIBUTES),
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ),
        ctypes.c_long,
    ),
    "nt_query_directory_file": _Signature(
        "ntdll",
        "NtQueryDirectoryFile",
        (
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.BOOLEAN,
            ctypes.c_void_p,
            wintypes.BOOLEAN,
        ),
        ctypes.c_long,
    ),
    "nt_query_information_file": _Signature(
        "ntdll",
        "NtQueryInformationFile",
        (
            wintypes.HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
        ),
        ctypes.c_long,
    ),
    "nt_set_information_file": _Signature(
        "ntdll",
        "NtSetInformationFile",
        (
            wintypes.HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
        ),
        ctypes.c_long,
    ),
}

_BINDING_LOCK = threading.Lock()
_BOUND_FUNCTIONS: dict[tuple[str, str, int], _BoundFunction] = {}


def _bind(name: str) -> Any:
    if os.name != "nt":
        raise WindowsNativeBindingError(
            "Windows native bindings are unavailable on this platform"
        )
    signature = _SIGNATURES[name]
    try:
        library = getattr(ctypes.windll, signature.dll_name)
        function = getattr(library, signature.function_name)
    except (AttributeError, OSError) as error:
        raise WindowsNativeBindingError(
            f"Windows native binding is unavailable: {signature.function_name}"
        ) from error
    key = (signature.dll_name, signature.function_name, id(function))
    with _BINDING_LOCK:
        bound = _BOUND_FUNCTIONS.get(key)
        if bound is None:
            try:
                function.argtypes = list(signature.argtypes)
                function.restype = signature.restype
            except (AttributeError, TypeError, ValueError) as error:
                raise WindowsNativeBindingError(
                    f"Windows native binding could not be configured: "
                    f"{signature.function_name}"
                ) from error
            bound = _BoundFunction(function, function.argtypes, signature)
            _BOUND_FUNCTIONS[key] = bound
        if (
            bound.function is not function
            or function.argtypes is not bound.argtypes_object
            or tuple(function.argtypes or ()) != signature.argtypes
            or function.restype is not signature.restype
        ):
            raise WindowsNativeBindingError(
                f"Windows native binding changed after initialization: "
                f"{signature.function_name}"
            )
        return function


def get_file_information_by_handle_ex() -> Any:
    return _bind("get_file_information_by_handle_ex")


def create_file() -> Any:
    return _bind("create_file")


def close_handle() -> Any:
    return _bind("close_handle")


def get_last_error() -> Any:
    return _bind("get_last_error")


def flush_file_buffers() -> Any:
    return _bind("flush_file_buffers")


def set_file_information_by_handle() -> Any:
    return _bind("set_file_information_by_handle")


def lock_file_ex() -> Any:
    return _bind("lock_file_ex")


def unlock_file_ex() -> Any:
    return _bind("unlock_file_ex")


def nt_create_file() -> Any:
    return _bind("nt_create_file")


def nt_query_directory_file() -> Any:
    return _bind("nt_query_directory_file")


def nt_query_information_file() -> Any:
    return _bind("nt_query_information_file")


def nt_set_information_file() -> Any:
    return _bind("nt_set_information_file")
