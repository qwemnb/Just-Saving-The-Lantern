"""Shared/exclusive coordination for every Helios Room database open."""

from __future__ import annotations

import contextlib
import ctypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import windows_native


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_DATABASE_PATH = Path(os.path.abspath(PROJECT_ROOT / "data" / "helios.db"))
ACTIVE_LOCK_PATH = PROJECT_ROOT / "data" / ".helios-room-database.lock"
RESET_STATE_NAME = ".helios-room-reset-state.json"
RESET_STATE_NEXT_NAME = ".helios-room-reset-state.json.next"
RESET_STATE_PREFIX = ".helios-room-reset-state."


class MaintenanceLockError(OSError):
    pass


class ResetRecoveryRequiredError(MaintenanceLockError):
    code = "database_reset_recovery_required"
    message = "The Helios Room database requires reset recovery before it can be opened."

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass
class MaintenanceLease:
    path: Path
    file: object
    shared: bool
    _overlapped: object | None = None

    def close(self) -> None:
        if self.file is None:
            return
        try:
            _unlock(self)
        finally:
            self.file.close()  # type: ignore[union-attr]
            self.file = None

    def __enter__(self) -> "MaintenanceLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def coordination_paths(database_path: Path | str) -> tuple[Path, Path, Path]:
    # Lock/control placement is lexical and must never follow an untrusted
    # database symlink or reparse target outside its configured directory.
    database = Path(os.path.abspath(database_path))
    if database == ACTIVE_DATABASE_PATH:
        directory = ACTIVE_LOCK_PATH.parent
        return (
            ACTIVE_LOCK_PATH,
            directory / RESET_STATE_NAME,
            directory / RESET_STATE_NEXT_NAME,
        )
    directory = database.parent
    return (
        directory / ".helios-room-database.lock",
        directory / RESET_STATE_NAME,
        directory / RESET_STATE_NEXT_NAME,
    )


def acquire_database_lease(
    database_path: Path | str,
    *,
    shared: bool,
    allow_recovery_state: bool = False,
) -> MaintenanceLease:
    lock_path, state_path, next_path = coordination_paths(database_path)
    if not lock_path.parent.is_dir():
        raise MaintenanceLockError("database maintenance directory is unavailable")
    if not allow_recovery_state:
        _require_no_reset_state(lock_path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    file = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MaintenanceLockError("maintenance lock path is unsafe")
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse:
            raise MaintenanceLockError("maintenance lock path is unsafe")
        lease = MaintenanceLease(lock_path, file, shared)
        _lock(lease)
        if not allow_recovery_state:
            try:
                _require_no_reset_state(lock_path.parent)
            except Exception:
                lease.close()
                raise
        return lease
    except Exception:
        file.close()
        raise


def _require_no_reset_state(directory: Path) -> None:
    try:
        if os.name == "nt":
            # Import lazily to keep normal lock-module import order acyclic.
            from . import reset_database

            names = reset_database._windows_enumerate_directory_direct(directory)
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(directory, flags)
            try:
                with os.scandir(descriptor) as entries:
                    names = [entry.name for entry in entries]
            finally:
                os.close(descriptor)
        for name in names:
            if (
                name in {RESET_STATE_NAME, RESET_STATE_NEXT_NAME}
                or name.startswith(RESET_STATE_PREFIX)
            ):
                raise ResetRecoveryRequiredError()
    except ResetRecoveryRequiredError:
        raise
    except (OSError, RuntimeError) as error:
        raise ResetRecoveryRequiredError() from error


@contextlib.contextmanager
def database_lease(
    database_path: Path | str,
    *,
    shared: bool = True,
    allow_recovery_state: bool = False,
) -> Iterator[MaintenanceLease]:
    lease = acquire_database_lease(
        database_path,
        shared=shared,
        allow_recovery_state=allow_recovery_state,
    )
    try:
        yield lease
    finally:
        lease.close()


def _lock(lease: MaintenanceLease) -> None:
    if os.name != "nt":
        import fcntl

        mode = fcntl.LOCK_SH if lease.shared else fcntl.LOCK_EX
        try:
            fcntl.flock(lease.file.fileno(), mode | fcntl.LOCK_NB)  # type: ignore[union-attr]
        except OSError as error:
            raise MaintenanceLockError("database maintenance lock unavailable") from error
        return

    from ctypes import wintypes
    import msvcrt

    try:
        lock_file_ex = windows_native.lock_file_ex()
    except windows_native.WindowsNativeBindingError as error:
        raise MaintenanceLockError(
            "database maintenance lock unavailable"
        ) from error
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(lease.file.fileno()))  # type: ignore[union-attr]
    flags = 0x00000001 | (0 if lease.shared else 0x00000002)
    overlapped = windows_native.OVERLAPPED()
    if not lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        raise MaintenanceLockError("database maintenance lock unavailable")
    lease._overlapped = overlapped


def _unlock(lease: MaintenanceLease) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(lease.file.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        return
    from ctypes import wintypes
    import msvcrt

    try:
        unlock_file_ex = windows_native.unlock_file_ex()
    except windows_native.WindowsNativeBindingError as error:
        raise MaintenanceLockError(
            "database maintenance lock unavailable"
        ) from error
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(lease.file.fileno()))  # type: ignore[union-attr]
    unlock_file_ex(handle, 0, 1, 0, ctypes.byref(lease._overlapped))
