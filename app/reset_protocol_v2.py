"""Windows-only immutable reset-control protocol (Revisions 2.4 through 2.9).

This module deliberately contains only reset planning/execution/recovery code.
Ordinary runtime interlocking remains in :mod:`app.maintenance_lock` and is
platform independent.  The implementation never opens the project database
unless the separately confirmed execution or recovery entry point reaches the
SQLite phase.
"""

from __future__ import annotations

import calendar
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .maintenance_lock import (
    MaintenanceLockError,
    ResetRecoveryRequiredError,
    acquire_database_lease,
)
from .schema_validation import SchemaValidationError, validate_database_integrity, validate_v14_foundation


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESET_PROTOCOL_VERSION = "room_shared_reset_v2"
LEGACY_RESET_PROTOCOL_VERSION = "room_shared_reset_v1"
JOURNAL_VERSION = 2
GENERATION_VERSION = 1
JOURNAL_STORAGE_PROTOCOL_VERSION = "immutable_generation_v1"
MAXIMUM_GENERATION_COUNT = 4096
LEGACY_V1_IMPLEMENTATION_COMMITS = {
    "d9d9717b4e882c19ef41e44a6f0c63fc062de41a"
}
GENERATION_FILENAME_REGEX = (
    r"^\.helios-room-reset-state\.([0-9a-f]{64})\.g"
    r"([0-9]{20})\.([0-9a-f]{64})\.json$"
)
GENERATION_RE = re.compile(GENERATION_FILENAME_REGEX)
GENERATION_PARTIAL_SUFFIX = ".partial"
EVIDENCE_RE = re.compile(
    r"^\.helios-room-reset-state\.([0-9a-f]{64})\.legacy-evidence\."
    r"([0-9a-f]{64})\.json$"
)
RESERVED_PREFIX = ".helios-room-reset-state."
LEGACY_NAMES = {
    ".helios-room-reset-state.json",
    ".helios-room-reset-state.json.next",
}
STAGES = (
    "ready_to_quarantine", "quarantining", "original_quarantined",
    "installing_fresh", "fresh_installed", "validating_fresh",
    "fresh_validated", "cleaning_quarantine", "finalizing",
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

IGNORE_BLOCK = """# Helios Room database and reset artifacts
/backups/
/data/helios.db
/data/helios.db-wal
/data/helios.db-shm
/data/.helios-room-database.lock
/data/.helios-room-reset-state.json
/data/.helios-room-reset-state.json.next
/data/.helios-room-reset-state.*.json
/data/.helios-room-reset-state.*.json.partial
/data/.helios*.reset-*
"""


def _error(code: str) -> legacy.DatabaseResetError:
    return legacy._error(code)


def _require_windows() -> None:
    if os.name != "nt":
        raise _error("reset_platform_unsupported")


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_canonical(raw: bytes, *, maximum: int = 1_048_576) -> Any:
    if not raw or len(raw) > maximum or raw.startswith(b"\xef\xbb\xbf"):
        raise _error("reset_recovery_invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _error("reset_recovery_invalid") from error
    if _canonical_bytes(value) != raw:
        raise _error("reset_recovery_invalid")
    return value


def _read_canonical(path: Path, *, maximum: int = 1_048_576) -> tuple[Any, bytes, str]:
    info = legacy._safe_regular(path)
    identity = legacy._path_identity(path)
    descriptor = legacy._open_existing_regular(
        path, identity, share_write=False
    )
    try:
        chunks: list[bytes] = []
        length = 0
        while True:
            block = os.read(descriptor, min(65_536, maximum + 1 - length))
            if not block:
                break
            chunks.append(block)
            length += len(block)
            if length > maximum:
                raise _error("reset_recovery_invalid")
        after = os.fstat(descriptor)
        if (
            after.st_nlink != 1
            or after.st_size != info.st_size
            or legacy._descriptor_identity(descriptor, after) != identity
        ):
            raise _error("reset_recovery_invalid")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    return _load_canonical(raw, maximum=maximum), raw, identity


def _read_exact_prefix(path: Path, expected: bytes) -> tuple[str, bytes]:
    """Return the identity only for an exact (possibly empty) byte prefix."""

    legacy._safe_regular(path)
    identity = legacy._path_identity(path)
    descriptor = legacy._open_existing_regular(
        path, identity, share_write=False
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= len(expected):
            block = os.read(descriptor, min(65_536, len(expected) + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            total > len(expected)
            or not expected.startswith(raw)
            or after.st_size != total
            or legacy._descriptor_identity(descriptor, after) != identity
        ):
            raise _error("reset_recovery_invalid")
        return identity, raw
    finally:
        os.close(descriptor)


def _validate_file_prefix(path: Path, source: Path) -> str:
    """Validate an interrupted copy as an exact prefix of its bound source."""

    source_info = legacy._safe_regular(source)
    source_identity = legacy._path_identity(source)
    source_descriptor = legacy._open_existing_regular(
        source, source_identity, share_write=False
    )
    try:
        candidate_info = legacy._safe_regular(path)
        if candidate_info.st_size > source_info.st_size:
            raise _error("reset_recovery_invalid")
        candidate_identity = legacy._path_identity(path)
        candidate_descriptor = legacy._open_existing_regular(
            path, candidate_identity, share_write=False
        )
        try:
            remaining = candidate_info.st_size
            while remaining:
                size = min(65_536, remaining)
                candidate_block = os.read(candidate_descriptor, size)
                source_block = os.read(source_descriptor, size)
                if not candidate_block or candidate_block != source_block:
                    raise _error("reset_recovery_invalid")
                remaining -= len(candidate_block)
            candidate_after = os.fstat(candidate_descriptor)
            source_after = os.fstat(source_descriptor)
            if (
                os.read(candidate_descriptor, 1)
                or candidate_after.st_size != candidate_info.st_size
                or source_after.st_size != source_info.st_size
                or legacy._descriptor_identity(candidate_descriptor, candidate_after)
                != candidate_identity
                or legacy._descriptor_identity(source_descriptor, source_after)
                != source_identity
            ):
                raise _error("reset_recovery_invalid")
            return candidate_identity
        finally:
            os.close(candidate_descriptor)
    finally:
        os.close(source_descriptor)


def _directory_observation(path: Path, *, required: bool) -> dict[str, Any]:
    info = legacy._safe_directory(path, required=required)
    if info is None:
        return {
            "exists": False,
            "file_type": "absent",
            "identity": None,
            "reparse_point": None,
        }
    return {
        "exists": True,
        "file_type": "directory",
        "identity": legacy._path_identity(path),
        "reparse_point": False,
    }


def _file_observation(path: Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {
            "exists": False,
            "file_type": "absent",
            "identity": None,
            "link_count": None,
            "mtime_ns": None,
            "sha256": None,
            "sha256_status": "not-present",
            "size": None,
        }
    info = legacy._safe_regular(path)
    return {
        "exists": True,
        "file_type": "regular",
        "identity": legacy._path_identity(path),
        "link_count": 1,
        "mtime_ns": info.st_mtime_ns,
        "sha256": legacy._stable_sha256(path),
        "sha256_status": "stable",
        "size": info.st_size,
    }


def _artifact_paths(stamp: str) -> dict[str, str]:
    token = "0" * 64
    generation = (
        f"data/.helios-room-reset-state.{token}.g"
        f"00000000000000000001.{token}.json"
    )
    backup = f"backups/helios-pre-room-shared-reset-{stamp}.db"
    audit = f"backups/helios-pre-room-shared-reset-{stamp}.audit.json"
    return {
        "audit_partial_path": f"{audit}.partial",
        "audit_path": audit,
        "backup_copy_partial_path": f"data/.helios.db.reset-backup-copy-{stamp}",
        "backup_partial_path": f"{backup}.partial",
        "backup_path": backup,
        "database_path": "data/helios.db",
        "database_shm_path": "data/helios.db-shm",
        "database_wal_path": "data/helios.db-wal",
        "failed_new_database_path": f"data/.helios.db.reset-failed-new-{stamp}",
        "failed_new_shm_path": f"data/.helios.db-shm.reset-failed-new-{stamp}",
        "failed_new_wal_path": f"data/.helios.db-wal.reset-failed-new-{stamp}",
        "fresh_database_staging_path": f"data/.helios.db.reset-fresh-{stamp}",
        "fresh_shm_staging_path": f"data/.helios.db-shm.reset-fresh-{stamp}",
        "fresh_wal_staging_path": f"data/.helios.db-wal.reset-fresh-{stamp}",
        "generation_partial_probe_path": f"{generation}.partial",
        "generation_probe_path": generation,
        "legacy_journal_path": "data/.helios-room-reset-state.json",
        "legacy_next_path": "data/.helios-room-reset-state.json.next",
        "maintenance_lock_path": "data/.helios-room-database.lock",
        "original_quarantine_database_path": f"data/.helios.db.reset-original-{stamp}",
        "original_quarantine_shm_path": f"data/.helios.db-shm.reset-original-{stamp}",
        "original_quarantine_wal_path": f"data/.helios.db-wal.reset-original-{stamp}",
        "restoring_database_path": f"data/.helios.db.reset-restoring-{stamp}",
        "restoring_shm_path": f"data/.helios.db-shm.reset-restoring-{stamp}",
        "restoring_wal_path": f"data/.helios.db-wal.reset-restoring-{stamp}",
    }


def _absence_paths(paths: dict[str, str]) -> list[str]:
    names = (
        "audit_partial_path", "audit_path", "backup_copy_partial_path",
        "backup_partial_path", "backup_path", "failed_new_database_path",
        "failed_new_shm_path", "failed_new_wal_path",
        "fresh_database_staging_path", "fresh_shm_staging_path",
        "fresh_wal_staging_path", "legacy_journal_path", "legacy_next_path",
        "original_quarantine_database_path", "original_quarantine_shm_path",
        "original_quarantine_wal_path", "restoring_database_path",
        "restoring_shm_path", "restoring_wal_path",
    )
    return sorted((paths[name] for name in names), key=lambda value: value.encode("utf-8"))


def _enumerate_reserved(data: Path) -> list[str]:
    legacy._safe_directory(data)
    try:
        names = (
            legacy._windows_enumerate_directory(data)
            if os.name == "nt"
            else [entry.name for entry in os.scandir(data)]
        )
    except (OSError, legacy.DatabaseResetError) as error:
        raise _error("reset_path_unsafe") from error
    found = [
        name for name in names
        if name in LEGACY_NAMES or name.startswith(RESERVED_PREFIX)
    ]
    return sorted(found, key=lambda value: value.encode("utf-8"))


def _git_evidence(root: Path, prospective: Iterable[str]) -> tuple[str, dict[str, Any]]:
    prospective_list = sorted(set(prospective), key=lambda value: value.encode("utf-8"))
    head, blob = legacy._git_safety(root, prospective_list)
    committed = legacy._git(root, "show", f"{head}:.gitignore")
    if committed.decode("utf-8").count(IGNORE_BLOCK) != 1:
        raise _error("reset_git_safety_invalid")
    return head, {
        "gitignore_blob_oid": blob,
        "gitignore_sha256": _sha256(committed),
        "prospective_ignore": [
            {"ignored": True, "path": path} for path in prospective_list
        ],
        "status_byte_length": 0,
        "status_sha256": EMPTY_SHA256,
    }


def _close_handle(handle: int) -> None:
    try:
        legacy._windows_close_handle(handle)
    except legacy.DatabaseResetError as error:
        raise _error("reset_durability_unsupported") from error


def _volume_information(root: str) -> tuple[int, str]:
    from ctypes import wintypes

    serial = wintypes.DWORD()
    filesystem = ctypes.create_unicode_buffer(32)
    if not ctypes.windll.kernel32.GetVolumeInformationW(
        root, None, 0, ctypes.byref(serial), None, None, filesystem, len(filesystem)
    ):
        raise _error("reset_durability_unsupported")
    return int(serial.value), filesystem.value


def _volume_root(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetVolumePathNameW(str(path), buffer, len(buffer)):
        raise _error("reset_durability_unsupported")
    return buffer.value


def _volume_guid(root: str) -> str:
    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer)):
        raise _error("reset_durability_unsupported")
    value = buffer.value
    prefix = "\\\\?\\Volume{"
    suffix = "}\\"
    identifier = (
        value[len(prefix):-len(suffix)]
        if value.startswith(prefix) and value.endswith(suffix)
        else ""
    )
    match = re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        identifier,
    )
    if match is None:
        raise _error("reset_durability_unsupported")
    return f"{prefix}{identifier.lower()}{suffix}"


def _directory_probe(
    name: str, path: Path | None, expected_identity: str | None
) -> dict[str, Any]:
    if path is None:
        return {
            "error_code": None,
            "flush_succeeded": None,
            "identity": None,
            "name": name,
            "state": "absent",
        }
    from ctypes import wintypes

    handle = legacy._windows_open_handle(
        path,
        0x80000000 | 0x40000000,
        0x02000000 | 0x00200000 | 0x80000000,
    )
    try:
        identity = legacy._windows_handle_identity(handle)
        if expected_identity is None or identity != expected_identity:
            raise _error("reset_durability_unsupported")
        flush = ctypes.windll.kernel32.FlushFileBuffers
        flush.argtypes = [wintypes.HANDLE]
        flush.restype = wintypes.BOOL
        if flush(wintypes.HANDLE(handle)):
            succeeded, error_code = True, None
        else:
            succeeded = False
            error_code = int(ctypes.windll.kernel32.GetLastError())
            if error_code not in {5, 6}:
                raise _error("reset_durability_unsupported")
        return {
            "error_code": error_code,
            "flush_succeeded": succeeded,
            "identity": identity,
            "name": name,
            "state": "present",
        }
    finally:
        _close_handle(handle)


def _open_and_flush_volume(guid: str, expected_serial: str) -> None:
    from ctypes import wintypes

    path = guid[:-1]
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(
        path,
        0x80000000 | 0x40000000,
        0x1 | 0x2,
        None,
        3,
        0x80000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise _error("reset_durability_unsupported")
    try:
        identity = legacy._windows_handle_identity(int(handle))
        if identity.split(":")[1] != expected_serial:
            raise _error("reset_durability_unsupported")
        if not ctypes.windll.kernel32.FlushFileBuffers(handle):
            raise _error("reset_durability_unsupported")
    finally:
        _close_handle(int(handle))


def _probe_durability(root: Path) -> dict[str, Any]:
    """Return the exact closed Win32 capability evidence or fail pre-artifact."""

    _require_windows()
    data = root / "data"
    backups = root / "backups"
    legacy._safe_directory(root)
    legacy._safe_directory(data)
    backups_info = legacy._safe_directory(backups, required=False)
    roots = [_volume_root(root), _volume_root(data)]
    if backups_info is not None:
        roots.append(_volume_root(backups))
    if len(set(roots)) != 1:
        raise _error("reset_durability_unsupported")
    volume_root = roots[0]
    if int(ctypes.windll.kernel32.GetDriveTypeW(volume_root)) != 3:
        raise _error("reset_durability_unsupported")
    serial32, filesystem = _volume_information(volume_root)
    if filesystem not in {"NTFS", "ReFS"}:
        raise _error("reset_durability_unsupported")
    guid = _volume_guid(volume_root)
    probes = [
        _directory_probe("repository", root, legacy._path_identity(root)),
        _directory_probe("data", data, legacy._path_identity(data)),
        _directory_probe(
            "backups", backups if backups_info is not None else None,
            None if backups_info is None else legacy._path_identity(backups),
        ),
    ]
    serials = {
        probe["identity"].split(":")[1]
        for probe in probes
        if probe["identity"] is not None
    }
    if len(serials) != 1:
        raise _error("reset_durability_unsupported")
    serial16 = next(iter(serials))
    if backups_info is None:
        fallback = "backups_absent"
    elif any(probe["flush_succeeded"] is False for probe in probes):
        fallback = "directory_flush_error"
    else:
        fallback = None
    if fallback is None:
        mode = "directory_flush"
        volume_opened: bool = False
        volume_flush_succeeded: bool | None = None
    else:
        _open_and_flush_volume(guid, serial16)
        mode = "volume_flush"
        volume_opened = True
        volume_flush_succeeded = True
    return {
        "api_contract": "win32_flush_v1",
        "directory_probes": probes,
        "drive_type": 3,
        "fallback_reason": fallback,
        "filesystem_name": filesystem,
        "handle_volume_serial_hex": serial16,
        "mode": mode,
        "volume_flush_succeeded": volume_flush_succeeded,
        "volume_guid": guid,
        "volume_information_serial_hex": f"{serial32:08x}",
        "volume_opened": volume_opened,
        "volume_root_resolved": True,
    }


def _flush_namespace(authority: dict[str, Any], parent: Path) -> None:
    """Complete exactly the persisted namespace durability operation."""

    try:
        if authority["mode"] == "directory_flush":
            probe = next(
                item for item in authority["directory_probes"]
                if item["name"] == (
                    "data" if parent.name == "data" else
                    "backups" if parent.name == "backups" else "repository"
                )
            )
            if legacy._path_identity(parent) != probe["identity"]:
                raise _error("reset_recovery_invalid")
            legacy._flush_directory(parent)
        elif authority["mode"] == "volume_flush":
            _open_and_flush_volume(
                authority["volume_guid"], authority["handle_volume_serial_hex"]
            )
        else:
            raise _error("reset_recovery_invalid")
    except legacy.DatabaseResetError as error:
        raise _DurabilityBoundaryError(
            "reset_failed", legacy._error("reset_failed").message
        ) from error
    except Exception as error:
        raise _DurabilityBoundaryError(
            "reset_failed", legacy._error("reset_failed").message
        ) from error


def _move_verified(source: Path, target: Path, identity: str) -> None:
    try:
        legacy._move_verified(source, target, identity, flush_parent=False)
    except legacy.DatabaseResetError as error:
        if error.code == "reset_failed":
            raise _DurabilityBoundaryError(error.code, error.message) from error
        raise


def _unlink_verified(path: Path, identity: str) -> None:
    try:
        legacy._unlink_verified(path, identity, flush_parent=False)
    except legacy.DatabaseResetError as error:
        if error.code == "reset_failed":
            raise _DurabilityBoundaryError(error.code, error.message) from error
        raise


def _unlink_verified_bytes(path: Path, identity: str, expected: bytes) -> None:
    """Delete the exact content-bound object through the validating handle."""

    if os.name != "nt":
        raise _error("reset_platform_unsupported")
    import msvcrt

    with legacy._verified_parent(path) as (parent_handle, parent_identity):
        legacy._revalidate_parent(path, parent_identity)
        raw_handle = legacy._windows_create_relative(
            parent_handle,
            path.name,
            directory=False,
            create_new=False,
            desired_access=(
                0x00010000 | 0x00100000 | 0x00000080 | 0x00000001
            ),
            share_access=0x1,
        )
        descriptor: int | None = None
        try:
            legacy._windows_validate_handle(raw_handle, directory=False)
            if legacy._windows_handle_identity(raw_handle) != identity:
                raise _error("reset_path_unsafe")
            descriptor = msvcrt.open_osfhandle(
                raw_handle, getattr(os, "O_BINARY", 0) | os.O_RDONLY
            )
            raw_handle = 0
            chunks: list[bytes] = []
            total = 0
            while total <= len(expected):
                block = os.read(
                    descriptor, min(65_536, len(expected) + 1 - total)
                )
                if not block:
                    break
                chunks.append(block)
                total += len(block)
            info = os.fstat(descriptor)
            if (
                b"".join(chunks) != expected
                or info.st_size != len(expected)
                or legacy._descriptor_identity(descriptor, info) != identity
            ):
                raise _error("reset_recovery_invalid")
            legacy._revalidate_parent(path, parent_identity)
            legacy._windows_unlink_handle(msvcrt.get_osfhandle(descriptor))
            to_close = descriptor
            descriptor = None
            try:
                os.close(to_close)
            except OSError as error:
                raise _DurabilityBoundaryError(
                    "reset_failed", legacy._error("reset_failed").message
                ) from error
            legacy._revalidate_parent(path, parent_identity)
        except legacy.DatabaseResetError as error:
            if error.code == "reset_failed":
                raise _DurabilityBoundaryError(error.code, error.message) from error
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif raw_handle:
                legacy._windows_close_handle(raw_handle)


def _copy_verified(
    source: Path,
    target: Path,
    expected_sha256: str,
    crash_checkpoint: Callable[[str], None] | None,
    *,
    expected_source_identity: str | None = None,
) -> None:
    try:
        legacy._copy_verified(
            source,
            target,
            expected_sha256,
            crash_checkpoint,
            flush_parent=False,
            expected_source_identity=expected_source_identity,
        )
    except legacy.DatabaseResetError as error:
        if error.code == "reset_failed":
            raise _DurabilityBoundaryError(error.code, error.message) from error
        raise


def _verified_file_sha256(path: Path, expected_identity: str) -> str:
    if legacy._path_identity(path) != expected_identity:
        raise _error("reset_recovery_invalid")
    descriptor = legacy._open_existing_regular(
        path,
        expected_identity,
        share_write=False,
        share_delete=False,
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_nlink != 1
            or after.st_nlink != 1
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or legacy._descriptor_identity(descriptor, before)
            != expected_identity
            or legacy._descriptor_identity(descriptor, after)
            != expected_identity
        ):
            raise _error("reset_recovery_invalid")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _rmdir_verified(path: Path, identity: str) -> None:
    try:
        legacy._rmdir_verified(path, identity, flush_parent=False)
    except legacy.DatabaseResetError as error:
        if error.code == "reset_failed":
            raise _DurabilityBoundaryError(error.code, error.message) from error
        raise


def _cleanup_prejournal_artifacts(
    backup: Path,
    audit: Path,
    created_backups_directory: bool,
    authority: dict[str, Any],
) -> None:
    try:
        for path in (
            Path(f"{backup}.partial-wal"), Path(f"{backup}.partial-shm"),
            Path(f"{backup}.partial-journal"), Path(f"{backup}.partial"),
            backup, Path(f"{audit}.partial"),
        ):
            if os.path.lexists(path):
                _unlink_verified(path, legacy._path_identity(path))
                _flush_namespace(authority, path.parent)
        if created_backups_directory and os.path.lexists(backup.parent):
            _rmdir_verified(backup.parent, legacy._path_identity(backup.parent))
            _flush_namespace(authority, backup.parent.parent)
    except legacy.DatabaseResetError:
        raise
    except Exception as error:
        raise _DurabilityBoundaryError(
            "reset_failed", legacy._error("reset_failed").message
        ) from error


def _prospective_paths(paths: dict[str, str]) -> list[str]:
    return sorted(paths.values(), key=lambda value: value.encode("utf-8"))


def _build_plan(
    root: Path,
    database_path: Path | str,
    *,
    stamp: str,
    durability: dict[str, Any],
) -> dict[str, Any]:
    paths = _artifact_paths(stamp)
    head, git = _git_evidence(root, _prospective_paths(paths))
    database = legacy._relative_database(root, database_path)
    reserved = _enumerate_reserved(database.parent)
    if reserved:
        raise _error("database_reset_recovery_required")
    filesystem = {
        "backups": _directory_observation(root / "backups", required=False),
        "data": _directory_observation(root / "data", required=True),
        "database": _file_observation(database),
        "database_shm": _file_observation(Path(f"{database}-shm")),
        "database_wal": _file_observation(Path(f"{database}-wal")),
        "repository": _directory_observation(root, required=True),
    }
    if filesystem["database_wal"]["exists"] != filesystem["database_shm"]["exists"]:
        raise _error("reset_path_unsafe")
    absence = _absence_paths(paths)
    for relative in absence:
        if os.path.lexists(root / relative):
            raise _error("reset_path_unsafe")
    manifest = {
        "artifact_paths": paths,
        "durability": durability,
        "filesystem": filesystem,
        "generation_filename_regex": GENERATION_FILENAME_REGEX,
        "generation_partial_suffix": GENERATION_PARTIAL_SUFFIX,
        "generation_version": GENERATION_VERSION,
        "git": git,
        "implementation_commit": head,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "journal_version": JOURNAL_VERSION,
        "legacy_v1_implementation_commits": sorted(LEGACY_V1_IMPLEMENTATION_COMMITS),
        "maximum_generation_count": MAXIMUM_GENERATION_COUNT,
        "path_absence": [{"exists": False, "path": path} for path in absence],
        "plan_timestamp_utc": stamp,
        "platform": "windows",
        "recovery_commit_by_schema": dict(legacy.RECOVERY_COMMIT_BY_SCHEMA),
        "reserved_reset_state_entries": [],
        "reset_protocol_version": RESET_PROTOCOL_VERSION,
    }
    token = _sha256(_canonical_bytes(manifest))
    result = {
        "audit_path": paths["audit_path"],
        "backup_path": paths["backup_path"],
        "database_identity": filesystem["database"]["identity"],
        "database_path": "data/helios.db",
        "database_sha256": filesystem["database"]["sha256"],
        "implementation_commit": head,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "plan_token": token,
        "recovery_commit_by_schema": dict(legacy.RECOVERY_COMMIT_BY_SCHEMA),
        "reset_protocol_version": RESET_PROTOCOL_VERSION,
        "reviewed_plan_manifest": manifest,
        "status": "planned",
    }
    _validate_plan(result, expected_token=token)
    return result


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_durability(value: Any) -> None:
    keys = {
        "api_contract", "directory_probes", "drive_type", "fallback_reason",
        "filesystem_name", "handle_volume_serial_hex", "mode",
        "volume_flush_succeeded", "volume_guid",
        "volume_information_serial_hex", "volume_opened", "volume_root_resolved",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise _error("reset_recovery_invalid")
    probes = value.get("directory_probes") if isinstance(value, dict) else None
    if (
        value["api_contract"] != "win32_flush_v1"
        or type(value["drive_type"]) is not int
        or value["drive_type"] != 3
        or value["filesystem_name"] not in {"NTFS", "ReFS"}
        or not isinstance(value["handle_volume_serial_hex"], str)
        or re.fullmatch(r"[0-9a-f]{16}", value["handle_volume_serial_hex"]) is None
        or not isinstance(value["volume_information_serial_hex"], str)
        or re.fullmatch(r"[0-9a-f]{8}", value["volume_information_serial_hex"]) is None
        or value["volume_root_resolved"] is not True
        or not isinstance(probes, list)
        or len(probes) != 3
        or any(not isinstance(item, dict) for item in probes)
        or [item.get("name") for item in probes]
        != ["repository", "data", "backups"]
        or not _valid_volume_guid(value["volume_guid"])
    ):
        raise _error("reset_recovery_invalid")
    for index, probe in enumerate(probes):
        if set(probe) != {"error_code", "flush_succeeded", "identity", "name", "state"}:
            raise _error("reset_recovery_invalid")
        if probe["state"] == "present":
            if (
                re.fullmatch(r"windows:[0-9a-f]{16}:[0-9a-f]{32}", str(probe["identity"])) is None
                or type(probe["flush_succeeded"]) is not bool
                or (
                    probe["flush_succeeded"] is True and probe["error_code"] is not None
                )
                or (
                    probe["flush_succeeded"] is False and probe["error_code"] not in {5, 6}
                )
            ):
                raise _error("reset_recovery_invalid")
        elif not (
            index == 2
            and probe == {
                "error_code": None, "flush_succeeded": None, "identity": None,
                "name": "backups", "state": "absent",
            }
        ):
            raise _error("reset_recovery_invalid")
    present_serials = {
        probe["identity"].split(":")[1]
        for probe in probes
        if probe["state"] == "present"
    }
    if present_serials != {value["handle_volume_serial_hex"]}:
        raise _error("reset_recovery_invalid")
    mode = value["mode"]
    if mode == "directory_flush":
        if (
            value["fallback_reason"] is not None
            or value["volume_opened"] is not False
            or value["volume_flush_succeeded"] is not None
            or any(item.get("state") != "present" or item.get("flush_succeeded") is not True for item in value["directory_probes"])
        ):
            raise _error("reset_recovery_invalid")
    elif mode == "volume_flush":
        if (
            value["fallback_reason"] not in {"backups_absent", "directory_flush_error"}
            or value["volume_opened"] is not True
            or value["volume_flush_succeeded"] is not True
        ):
            raise _error("reset_recovery_invalid")
        if value["fallback_reason"] == "backups_absent":
            if probes[2]["state"] != "absent" or any(item["flush_succeeded"] is not True for item in probes[:2]):
                raise _error("reset_recovery_invalid")
        elif (
            any(item["state"] != "present" for item in probes)
            or not any(item["flush_succeeded"] is False for item in probes)
        ):
            raise _error("reset_recovery_invalid")
    else:
        raise _error("reset_recovery_invalid")


def _valid_volume_guid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    prefix = "\\\\?\\Volume{"
    suffix = "}\\"
    if not value.startswith(prefix) or not value.endswith(suffix):
        return False
    identifier = value[len(prefix):-len(suffix)]
    return re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        identifier,
    ) is not None


def _validate_plan(value: Any, *, expected_token: str | None = None) -> dict[str, Any]:
    top = {
        "audit_path", "backup_path", "database_identity", "database_path",
        "database_sha256", "implementation_commit",
        "journal_storage_protocol_version", "plan_token",
        "recovery_commit_by_schema", "reset_protocol_version",
        "reviewed_plan_manifest", "status",
    }
    if not isinstance(value, dict) or set(value) != top:
        raise _error("reset_recovery_invalid")
    manifest = value["reviewed_plan_manifest"]
    manifest_keys = {
        "artifact_paths", "durability", "filesystem",
        "generation_filename_regex", "generation_partial_suffix",
        "generation_version", "git", "implementation_commit",
        "journal_storage_protocol_version", "journal_version",
        "legacy_v1_implementation_commits", "maximum_generation_count",
        "path_absence", "plan_timestamp_utc", "platform",
        "recovery_commit_by_schema", "reserved_reset_state_entries",
        "reset_protocol_version",
    }
    if not isinstance(manifest, dict) or set(manifest) != manifest_keys:
        raise _error("reset_recovery_invalid")
    stamp = manifest.get("plan_timestamp_utc")
    if not isinstance(stamp, str) or re.fullmatch(r"[0-9]{8}T[0-9]{9}Z", stamp) is None:
        raise _error("reset_recovery_invalid")
    try:
        __import__("datetime").datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ")
    except ValueError as error:
        raise _error("reset_recovery_invalid") from error
    expected_paths = _artifact_paths(stamp)
    if manifest.get("artifact_paths") != expected_paths:
        raise _error("reset_recovery_invalid")
    filesystem = manifest.get("filesystem")
    filesystem_keys = {"backups", "data", "database", "database_shm", "database_wal", "repository"}
    if not isinstance(filesystem, dict) or set(filesystem) != filesystem_keys:
        raise _error("reset_recovery_invalid")
    present_directory = {"exists", "file_type", "identity", "reparse_point"}
    for name in ("repository", "data", "backups"):
        item = filesystem[name]
        if not isinstance(item, dict) or set(item) != present_directory:
            raise _error("reset_recovery_invalid")
        if item["exists"] is True:
            if (
                item["file_type"] != "directory"
                or item["reparse_point"] is not False
                or re.fullmatch(r"windows:[0-9a-f]{16}:[0-9a-f]{32}", str(item["identity"])) is None
            ):
                raise _error("reset_recovery_invalid")
        elif not (
            name == "backups"
            and item == {"exists": False, "file_type": "absent", "identity": None, "reparse_point": None}
        ):
            raise _error("reset_recovery_invalid")
    for name in ("database", "database_wal", "database_shm"):
        item = filesystem[name]
        if not isinstance(item, dict) or set(item) != legacy.OBSERVATION_FIELDS:
            raise _error("reset_recovery_invalid")
        legacy._validate_observation(item)
        if item["sha256_status"] not in {"stable", "not-present"}:
            raise _error("reset_recovery_invalid")
        if item["exists"] and (
            item["mtime_ns"] > 9_223_372_036_854_775_807
            or item["size"] > 9_223_372_036_854_775_807
        ):
            raise _error("reset_recovery_invalid")
        if item["exists"] and re.fullmatch(r"windows:[0-9a-f]{16}:[0-9a-f]{32}", item["identity"]) is None:
            raise _error("reset_recovery_invalid")
    if filesystem["database"]["exists"] is not True:
        raise _error("reset_recovery_invalid")
    git = manifest.get("git")
    if not isinstance(git, dict) or set(git) != {
        "gitignore_blob_oid", "gitignore_sha256", "prospective_ignore",
        "status_byte_length", "status_sha256",
    }:
        raise _error("reset_recovery_invalid")
    prospective = git["prospective_ignore"]
    expected_prospective = [
        {"ignored": True, "path": path}
        for path in sorted(expected_paths.values(), key=lambda item: item.encode("utf-8"))
    ]
    if (
        prospective != expected_prospective
        or type(git["status_byte_length"]) is not int
        or git["status_byte_length"] != 0
        or git["status_sha256"] != EMPTY_SHA256
        or not _valid_sha(git["gitignore_sha256"])
        or not legacy._valid_commit(git["gitignore_blob_oid"])
        or len(git["gitignore_blob_oid"]) != len(manifest["implementation_commit"])
    ):
        raise _error("reset_recovery_invalid")
    expected_absence = [
        {"exists": False, "path": path} for path in _absence_paths(expected_paths)
    ]
    if manifest.get("path_absence") != expected_absence:
        raise _error("reset_recovery_invalid")
    token = _sha256(_canonical_bytes(manifest))
    if (
        value["status"] != "planned"
        or value["database_path"] != "data/helios.db"
        or value["reset_protocol_version"] != RESET_PROTOCOL_VERSION
        or value["journal_storage_protocol_version"] != JOURNAL_STORAGE_PROTOCOL_VERSION
        or manifest["reset_protocol_version"] != RESET_PROTOCOL_VERSION
        or manifest["journal_storage_protocol_version"] != JOURNAL_STORAGE_PROTOCOL_VERSION
        or type(manifest["journal_version"]) is not int
        or manifest["journal_version"] != JOURNAL_VERSION
        or type(manifest["generation_version"]) is not int
        or manifest["generation_version"] != GENERATION_VERSION
        or manifest["generation_filename_regex"] != GENERATION_FILENAME_REGEX
        or manifest["generation_partial_suffix"] != GENERATION_PARTIAL_SUFFIX
        or type(manifest["maximum_generation_count"]) is not int
        or manifest["maximum_generation_count"] != MAXIMUM_GENERATION_COUNT
        or manifest["legacy_v1_implementation_commits"] != sorted(LEGACY_V1_IMPLEMENTATION_COMMITS)
        or manifest["recovery_commit_by_schema"] != legacy.RECOVERY_COMMIT_BY_SCHEMA
        or manifest["platform"] != "windows"
        or manifest["reserved_reset_state_entries"] != []
        or not legacy._valid_commit(manifest["implementation_commit"])
        or value["plan_token"] != token
        or (expected_token is not None and token != expected_token)
        or value["implementation_commit"] != manifest["implementation_commit"]
        or value["recovery_commit_by_schema"] != manifest["recovery_commit_by_schema"]
        or value["backup_path"] != manifest["artifact_paths"].get("backup_path")
        or value["audit_path"] != manifest["artifact_paths"].get("audit_path")
        or value["database_identity"] != manifest["filesystem"].get("database", {}).get("identity")
        or value["database_sha256"] != manifest["filesystem"].get("database", {}).get("sha256")
    ):
        raise _error("reset_recovery_invalid")
    _validate_durability(manifest["durability"])
    durability = manifest["durability"]
    probes = {item["name"]: item for item in durability["directory_probes"]}
    for name in ("repository", "data", "backups"):
        observed = filesystem[name]
        probe = probes[name]
        if observed["exists"] != (probe["state"] == "present"):
            raise _error("reset_recovery_invalid")
        if observed["exists"] and observed["identity"] != probe["identity"]:
            raise _error("reset_recovery_invalid")
    for item in filesystem.values():
        identity = item.get("identity")
        if identity is not None and identity.split(":")[1] != durability["handle_volume_serial_hex"]:
            raise _error("reset_recovery_invalid")
    if not _valid_sha(value["plan_token"]) or not legacy._valid_commit(value["implementation_commit"]):
        raise _error("reset_recovery_invalid")
    return value


def plan_database_reset(
    database_path: Path | str, *, repository_root: Path | str = PROJECT_ROOT
) -> dict[str, Any]:
    _require_windows()
    root = Path(repository_root).resolve(strict=True)
    stamp, _iso = legacy._utc_stamp()
    paths = _artifact_paths(stamp)
    _git_evidence(root, _prospective_paths(paths))
    if _enumerate_reserved(root / "data"):
        raise _error("database_reset_recovery_required")
    durability = _probe_durability(root)
    try:
        lease = acquire_database_lease(root / "data" / "helios.db", shared=False)
    except ResetRecoveryRequiredError as error:
        raise _error("database_reset_recovery_required") from error
    except MaintenanceLockError as error:
        raise _error("database_maintenance_in_progress") from error
    try:
        return _build_plan(
            root, database_path, stamp=stamp, durability=durability
        )
    finally:
        lease.close()


@dataclass
class Generation:
    path: Path
    partial: bool
    token: str
    sequence: int
    content_hash: str
    envelope: dict[str, Any]
    raw: bytes
    identity: str


def _generation_name(token: str, sequence: int, digest: str) -> str:
    return f".helios-room-reset-state.{token}.g{sequence:020d}.{digest}.json"


def _validate_native_journal(value: Any, token: str, commit: str, sequence: int) -> dict[str, Any]:
    # Revision 2 retains the closed Revision 2.3 object and changes only these values.
    if not isinstance(value, dict) or set(value) != legacy.JOURNAL_FIELDS:
        raise _error("reset_recovery_invalid")
    candidate = dict(value)
    if (
        candidate.get("journal_version") != JOURNAL_VERSION
        or candidate.get("reset_protocol_version") != RESET_PROTOCOL_VERSION
        or candidate.get("journal_sequence") != sequence
    ):
        raise _error("reset_recovery_invalid")
    compatibility = dict(candidate)
    compatibility["journal_version"] = 1
    compatibility["reset_protocol_version"] = LEGACY_RESET_PROTOCOL_VERSION
    compatibility["quarantine"] = {
        key: (
            None if item is None else {
                "identity": item["identity"],
                "path": f"data/.helios.db{suffix}.reset-{token}.original",
            }
        )
        for key, suffix, item in (
            ("database", "", candidate["quarantine"]["database"]),
            ("wal", "-wal", candidate["quarantine"]["wal"]),
            ("shm", "-shm", candidate["quarantine"]["shm"]),
        )
    }
    legacy._validate_journal(
        compatibility, expected_token=token, implementation_commit=commit
    )
    for mapping in (candidate["source_files"], candidate["quarantine"]):
        for item in mapping.values():
            if item is not None and re.fullmatch(
                r"windows:[0-9a-f]{16}:[0-9a-f]{32}", item["identity"]
            ) is None:
                raise _error("reset_recovery_invalid")
    return candidate


def _validate_legacy_evidence(value: Any, token: str, converter: str) -> dict[str, Any]:
    keys = {
        "converter_implementation_commit", "durability", "evidence_version",
        "journal_storage_protocol_version", "legacy_plan_token", "platform",
        "reset_protocol_version",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise _error("reset_recovery_invalid")
    if (
        value["converter_implementation_commit"] != converter
        or type(value["evidence_version"]) is not int
        or value["evidence_version"] != 1
        or value["journal_storage_protocol_version"] != JOURNAL_STORAGE_PROTOCOL_VERSION
        or value["legacy_plan_token"] != token
        or value["platform"] != "windows"
        or value["reset_protocol_version"] != LEGACY_RESET_PROTOCOL_VERSION
    ):
        raise _error("reset_recovery_invalid")
    _validate_durability(value["durability"])
    return value


def _validate_legacy_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "journal_path", "journal_sha256", "next_path", "next_sha256", "selected"
    }:
        raise _error("reset_recovery_invalid")
    if (
        value["journal_path"] != "data/.helios-room-reset-state.json"
        or value["next_path"] != "data/.helios-room-reset-state.json.next"
        or value["selected"] not in {"journal", "next"}
        or any(item is not None and not _valid_sha(item) for item in (value["journal_sha256"], value["next_sha256"]))
    ):
        raise _error("reset_recovery_invalid")
    selected_hash = value[f"{value['selected']}_sha256"]
    if selected_hash is None:
        raise _error("reset_recovery_invalid")
    return value


def _validate_envelope(
    value: Any, *, token: str, content_hash: str, sequence: int
) -> dict[str, Any]:
    keys = {
        "converter_implementation_commit", "generation_sequence",
        "generation_version", "journal", "legacy_recovery_evidence",
        "legacy_source", "origin", "previous_generation_sha256",
        "reviewed_plan_manifest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise _error("reset_recovery_invalid")
    if (
        sequence < 1
        or sequence > MAXIMUM_GENERATION_COUNT
        or type(value["generation_sequence"]) is not int
        or value["generation_sequence"] != sequence
        or type(value["generation_version"]) is not int
        or value["generation_version"] != 1
    ):
        raise _error("reset_recovery_invalid")
    if sequence == 1:
        if value["previous_generation_sha256"] is not None:
            raise _error("reset_recovery_invalid")
    elif not _valid_sha(value["previous_generation_sha256"]):
        raise _error("reset_recovery_invalid")
    origin = value["origin"]
    if origin == "native_v2":
        if (
            value["converter_implementation_commit"] is not None
            or value["legacy_source"] is not None
            or value["legacy_recovery_evidence"] is not None
            or not isinstance(value["reviewed_plan_manifest"], dict)
        ):
            raise _error("reset_recovery_invalid")
        manifest = value["reviewed_plan_manifest"]
        if _sha256(_canonical_bytes(manifest)) != token:
            raise _error("reset_recovery_invalid")
        commit = manifest.get("implementation_commit")
        journal = _validate_native_journal(value["journal"], token, commit, sequence)
        filesystem = manifest.get("filesystem") if isinstance(manifest, dict) else None
        paths = manifest.get("artifact_paths") if isinstance(manifest, dict) else None
        if not isinstance(filesystem, dict) or not isinstance(paths, dict):
            raise _error("reset_recovery_invalid")
        _validate_plan({
            "audit_path": paths.get("audit_path"),
            "backup_path": paths.get("backup_path"),
            "database_identity": filesystem.get("database", {}).get("identity"),
            "database_path": "data/helios.db",
            "database_sha256": filesystem.get("database", {}).get("sha256"),
            "implementation_commit": commit,
            "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
            "plan_token": token,
            "recovery_commit_by_schema": manifest.get("recovery_commit_by_schema"),
            "reset_protocol_version": RESET_PROTOCOL_VERSION,
            "reviewed_plan_manifest": manifest,
            "status": "planned",
        }, expected_token=token)
        if (
            journal["audit_path"] != paths["audit_path"]
            or journal["backup"]["path"] != paths["backup_path"]
        ):
            raise _error("reset_recovery_invalid")
        expected_quarantine_paths = {
            "database": paths["original_quarantine_database_path"],
            "wal": paths["original_quarantine_wal_path"],
            "shm": paths["original_quarantine_shm_path"],
        }
        for key, item in journal["quarantine"].items():
            if item is not None and item["path"] != expected_quarantine_paths[key]:
                raise _error("reset_recovery_invalid")
        plan_observations = {
            "database": filesystem["database"],
            "wal": filesystem["database_wal"],
            "shm": filesystem["database_shm"],
        }
        if (
            journal["source_observations"]["before_open"] != plan_observations
            or journal["source_observations"]["after_close"] != plan_observations
        ):
            raise _error("reset_recovery_invalid")
        for key, observation in plan_observations.items():
            source = journal["source_files"][key]
            if not observation["exists"]:
                if source is not None:
                    raise _error("reset_recovery_invalid")
                continue
            if (
                not isinstance(source, dict)
                or source["identity"] != observation["identity"]
                or source["link_count"] != observation["link_count"]
                or source["mtime_ns"] != observation["mtime_ns"]
                or source["sha256"] != observation["sha256"]
                or source["size"] != observation["size"]
            ):
                raise _error("reset_recovery_invalid")
    elif origin == "legacy_v1_conversion":
        converter = value["converter_implementation_commit"]
        if (
            not legacy._valid_commit(converter)
            or value["reviewed_plan_manifest"] is not None
            or value["legacy_source"] is None
            or value["legacy_recovery_evidence"] is None
        ):
            raise _error("reset_recovery_invalid")
        source = _validate_legacy_source(value["legacy_source"])
        _validate_legacy_evidence(value["legacy_recovery_evidence"], token, converter)
        journal = value["journal"]
        if not isinstance(journal, dict):
            raise _error("reset_recovery_invalid")
        selected_sequence = journal.get("journal_sequence") - sequence + 1
        if type(selected_sequence) is not int or selected_sequence <= 0:
            raise _error("reset_recovery_invalid")
        legacy._validate_journal(
            journal,
            expected_token=token,
            implementation_commit=journal.get("implementation_commit"),
        )
        if journal["implementation_commit"] not in LEGACY_V1_IMPLEMENTATION_COMMITS:
            raise _error("reset_recovery_invalid")
        if (
            source[f"{source['selected']}_sha256"] is None
            or (
                sequence == 1
                and source[f"{source['selected']}_sha256"]
                != _sha256(_canonical_bytes(journal))
            )
        ):
            raise _error("reset_recovery_invalid")
    else:
        raise _error("reset_recovery_invalid")
    if _sha256(_canonical_bytes(value)) != content_hash:
        raise _error("reset_recovery_invalid")
    return value


class GenerationStore:
    def __init__(
        self,
        root: Path,
        token: str,
        authority: dict[str, Any],
        *,
        origin: str,
        reviewed_plan_manifest: dict[str, Any] | None,
        legacy_recovery_evidence: dict[str, Any] | None = None,
        legacy_source: dict[str, Any] | None = None,
        converter_implementation_commit: str | None = None,
        generations: list[Generation] | None = None,
        chain_hashes: list[str] | None = None,
    ) -> None:
        self.root = root
        self.data = root / "data"
        self.token = token
        self.authority = authority
        self.origin = origin
        self.reviewed_plan_manifest = reviewed_plan_manifest
        self.legacy_recovery_evidence = legacy_recovery_evidence
        self.legacy_source = legacy_source
        self.converter_implementation_commit = converter_implementation_commit
        self.generations = list(generations or [])
        self.chain_hashes = list(chain_hashes or [item.content_hash for item in self.generations])

    @property
    def journal(self) -> dict[str, Any]:
        if not self.generations:
            raise _error("reset_recovery_invalid")
        return dict(self.generations[-1].envelope["journal"])

    def append(
        self,
        journal: dict[str, Any],
        stage: str,
        crash_checkpoint: Callable[[str], None] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        sequence = len(self.generations) + 1
        if sequence > MAXIMUM_GENERATION_COUNT or stage not in STAGES:
            raise _error("reset_failed")
        value = dict(journal)
        value["stage"] = stage
        if self.origin == "native_v2":
            value["journal_sequence"] = sequence
            value["journal_version"] = JOURNAL_VERSION
            value["reset_protocol_version"] = RESET_PROTOCOL_VERSION
        else:
            if self.generations:
                value["journal_sequence"] = (
                    self.generations[-1].envelope["journal"]["journal_sequence"] + 1
                )
                value["updated_at"] = legacy._utc_stamp()[1]
            # Conversion generation 1 preserves the selected legacy journal's
            # sequence and timestamp byte-for-byte.
        if self.origin == "native_v2":
            value["updated_at"] = legacy._utc_stamp()[1]
        envelope = {
            "converter_implementation_commit": self.converter_implementation_commit,
            "generation_sequence": sequence,
            "generation_version": GENERATION_VERSION,
            "journal": value,
            "legacy_recovery_evidence": self.legacy_recovery_evidence,
            "legacy_source": self.legacy_source,
            "origin": self.origin,
            "previous_generation_sha256": (
                None if not self.generations else self.generations[-1].content_hash
            ),
            "reviewed_plan_manifest": self.reviewed_plan_manifest,
        }
        raw = _canonical_bytes(envelope)
        digest = _sha256(raw)
        name = _generation_name(self.token, sequence, digest)
        final = self.data / name
        partial = Path(f"{final}{GENERATION_PARTIAL_SUFFIX}")
        legacy._git_safety(
            self.root,
            [f"data/{name}", f"data/{name}{GENERATION_PARTIAL_SUFFIX}"],
        )
        if os.path.lexists(final) or os.path.lexists(partial):
            raise _error("reset_failed")
        with _open_control_partial(partial, create_new=True) as control:
            identity = control.identity
            checkpoint_prefix = label or stage
            if crash_checkpoint is not None:
                boundary = max(1, len(raw) // 2)
                legacy._write_all(control.descriptor, raw[:boundary])
                crash_checkpoint(f"{checkpoint_prefix}:generation_partial_write")
                legacy._write_all(control.descriptor, raw[boundary:])
            else:
                legacy._write_all(control.descriptor, raw)
            _promote_open_control(
                control,
                final,
                raw,
                crash_checkpoint=crash_checkpoint,
                checkpoint_prefix=checkpoint_prefix,
            )
        _flush_namespace(self.authority, self.data)
        parsed, installed, installed_identity = _read_canonical(final)
        _validate_envelope(parsed, token=self.token, content_hash=digest, sequence=sequence)
        if installed != raw or installed_identity != identity:
            raise _error("reset_failed")
        generation = Generation(final, False, self.token, sequence, digest, envelope, raw, identity)
        self.generations.append(generation)
        self.chain_hashes.append(digest)
        if crash_checkpoint is not None:
            crash_checkpoint(checkpoint_prefix)
        journal.clear()
        journal.update(value)
        return value

    def hashes(self) -> list[str]:
        return list(self.chain_hashes)


def _read_generation(path: Path, *, partial: bool) -> Generation:
    base = path.name[:-len(GENERATION_PARTIAL_SUFFIX)] if partial else path.name
    match = GENERATION_RE.fullmatch(base)
    if match is None:
        raise _error("reset_recovery_invalid")
    token, sequence_text, digest = match.groups()
    sequence = int(sequence_text)
    value, raw, identity = _read_canonical(path)
    _validate_envelope(value, token=token, content_hash=digest, sequence=sequence)
    return Generation(path, partial, token, sequence, digest, value, raw, identity)


def _discover_generations(root: Path, expected_token: str) -> tuple[list[Generation], list[Path], list[Path], list[Path]]:
    completed: list[Generation] = []
    partials: list[Path] = []
    evidence: list[Path] = []
    legacy_paths: list[Path] = []
    for name in _enumerate_reserved(root / "data"):
        path = root / "data" / name
        if name in LEGACY_NAMES:
            legacy_paths.append(path)
            continue
        evidence_base = name[:-len(GENERATION_PARTIAL_SUFFIX)] if name.endswith(GENERATION_PARTIAL_SUFFIX) else name
        if EVIDENCE_RE.fullmatch(evidence_base):
            evidence.append(path)
            continue
        is_partial = name.endswith(GENERATION_PARTIAL_SUFFIX)
        base = name[:-len(GENERATION_PARTIAL_SUFFIX)] if is_partial else name
        match = GENERATION_RE.fullmatch(base)
        if match is None or match.group(1) != expected_token:
            raise _error("reset_recovery_invalid")
        if is_partial:
            legacy._safe_regular(path)
            partials.append(path)
        else:
            completed.append(_read_generation(path, partial=False))
    return completed, partials, evidence, legacy_paths


def _validate_chain(generations: list[Generation], expected_token: str, current_commit: str) -> list[Generation]:
    ordered = sorted(generations, key=lambda item: item.sequence)
    if not ordered:
        return []
    if len(ordered) > MAXIMUM_GENERATION_COUNT:
        raise _error("reset_recovery_invalid")
    if [item.sequence for item in ordered] != list(range(1, len(ordered) + 1)):
        raise _error("reset_recovery_invalid")
    first = ordered[0].envelope
    origin = first["origin"]
    fixed_journal_keys = legacy.JOURNAL_FIELDS - {"journal_sequence", "stage", "updated_at"}
    previous_stage_index: int | None = None
    first_legacy_sequence = first["journal"].get("journal_sequence")
    for index, generation in enumerate(ordered):
        envelope = generation.envelope
        if generation.token != expected_token or envelope["origin"] != origin:
            raise _error("reset_recovery_invalid")
        if index and envelope["previous_generation_sha256"] != ordered[index - 1].content_hash:
            raise _error("reset_recovery_invalid")
        if any(
            envelope["journal"][key] != first["journal"][key]
            for key in fixed_journal_keys
        ):
            raise _error("reset_recovery_invalid")
        stage_index = STAGES.index(envelope["journal"]["stage"])
        if previous_stage_index is not None and (
            stage_index < previous_stage_index or stage_index > previous_stage_index + 1
        ):
            raise _error("reset_recovery_invalid")
        previous_stage_index = stage_index
        if origin == "native_v2":
            if envelope["reviewed_plan_manifest"] != first["reviewed_plan_manifest"]:
                raise _error("reset_recovery_invalid")
            if envelope["journal"]["implementation_commit"] != current_commit:
                raise _error("reset_recovery_invalid")
        else:
            if (
                envelope["legacy_recovery_evidence"] != first["legacy_recovery_evidence"]
                or envelope["legacy_source"] != first["legacy_source"]
                or envelope["converter_implementation_commit"] != current_commit
                or envelope["journal"]["journal_sequence"]
                != first_legacy_sequence + index
            ):
                raise _error("reset_recovery_invalid")
    return ordered


def _validate_terminal_subset(
    root: Path,
    generations: list[Generation],
    expected_token: str,
    current_commit: str,
) -> tuple[list[Generation], list[str]]:
    ordered = sorted(generations, key=lambda item: item.sequence)
    if not ordered:
        raise _error("reset_recovery_invalid")
    terminal = ordered[-1]
    journal = terminal.envelope["journal"]
    audit_path = root / journal["audit_path"]
    if not os.path.lexists(audit_path):
        raise _error("reset_recovery_invalid")
    audit, audit_raw, _identity = _read_canonical(audit_path)
    origin = terminal.envelope["origin"]
    if origin == "native_v2":
        if not isinstance(audit, dict):
            raise _error("reset_recovery_invalid")
        hashes = audit.get("generation_chain_sha256")
        if (
            not isinstance(hashes, list)
            or not hashes
            or any(not _valid_sha(item) for item in hashes)
            or audit.get("journal_storage_protocol_version") != JOURNAL_STORAGE_PROTOCOL_VERSION
            or type(audit.get("terminal_generation_sequence")) is not int
            or audit.get("terminal_generation_sequence") != len(hashes)
            or audit.get("terminal_generation_sha256") != hashes[-1]
        ):
            raise _error("reset_recovery_invalid")
        compatibility = dict(audit)
        for key in (
            "generation_chain_sha256", "journal_storage_protocol_version",
            "terminal_generation_sequence", "terminal_generation_sha256",
        ):
            compatibility.pop(key, None)
        compatibility["reset_protocol_version"] = LEGACY_RESET_PROTOCOL_VERSION
        legacy_journal = dict(journal)
        legacy_journal["reset_protocol_version"] = LEGACY_RESET_PROTOCOL_VERSION
        legacy_journal["journal_version"] = 1
        legacy_journal["quarantine"] = {
            key: (
                None if item is None else {
                    "identity": item["identity"],
                    "path": f"data/.helios.db{suffix}.reset-{expected_token}.original",
                }
            )
            for key, suffix, item in (
                ("database", "", journal["quarantine"]["database"]),
                ("wal", "-wal", journal["quarantine"]["wal"]),
                ("shm", "-shm", journal["quarantine"]["shm"]),
            )
        }
        legacy._validate_audit(compatibility, legacy_journal)
        if set(audit) != set(compatibility) | {
            "generation_chain_sha256", "journal_storage_protocol_version",
            "terminal_generation_sequence", "terminal_generation_sha256",
        }:
            raise _error("reset_recovery_invalid")
    else:
        legacy._validate_audit(audit, journal)
        chain_manifest_path = Path(f"{audit_path}.generation-chain.json")
        if not os.path.lexists(chain_manifest_path):
            raise _error("reset_recovery_invalid")
        manifest, _raw, _manifest_identity = _read_canonical(chain_manifest_path)
        hashes = manifest.get("generation_chain_sha256") if isinstance(manifest, dict) else None
        if (
            not isinstance(hashes, list)
            or not hashes
            or set(manifest) != {
                "audit_path", "audit_sha256", "converter_implementation_commit",
                "generation_chain_sha256", "journal_storage_protocol_version",
                "origin", "plan_token", "terminal_generation_sequence",
                "terminal_generation_sha256",
            }
            or manifest.get("audit_path") != journal["audit_path"]
            or manifest.get("audit_sha256") != _sha256(audit_raw)
            or manifest.get("origin") != "legacy_v1_conversion"
            or manifest.get("plan_token") != expected_token
            or manifest.get("converter_implementation_commit") != current_commit
            or type(manifest.get("terminal_generation_sequence")) is not int
            or manifest.get("terminal_generation_sequence") != len(hashes)
            or manifest.get("terminal_generation_sha256") != hashes[-1]
        ):
            raise _error("reset_recovery_invalid")
    if terminal.sequence != len(hashes) or terminal.content_hash != hashes[-1]:
        raise _error("reset_recovery_invalid")
    for generation in ordered:
        if (
            generation.sequence < 1
            or generation.sequence > len(hashes)
            or generation.token != expected_token
            or generation.content_hash != hashes[generation.sequence - 1]
        ):
            raise _error("reset_recovery_invalid")
        expected_previous = None if generation.sequence == 1 else hashes[generation.sequence - 2]
        if generation.envelope["previous_generation_sha256"] != expected_previous:
            raise _error("reset_recovery_invalid")
        if generation.envelope["origin"] != origin:
            raise _error("reset_recovery_invalid")
    # Validate fixed content and implementation relationships on the remaining
    # subset without requiring already-audited deleted predecessors to reappear.
    fixed = legacy.JOURNAL_FIELDS - {"journal_sequence", "stage", "updated_at"}
    first_remaining = ordered[0].envelope
    for generation in ordered:
        envelope = generation.envelope
        if any(envelope["journal"][key] != first_remaining["journal"][key] for key in fixed):
            raise _error("reset_recovery_invalid")
        if origin == "native_v2":
            if (
                envelope["journal"]["implementation_commit"] != current_commit
                or envelope["reviewed_plan_manifest"] != terminal.envelope["reviewed_plan_manifest"]
            ):
                raise _error("reset_recovery_invalid")
        elif envelope["converter_implementation_commit"] != current_commit:
            raise _error("reset_recovery_invalid")
        elif (
            envelope["legacy_recovery_evidence"]
            != terminal.envelope["legacy_recovery_evidence"]
            or envelope["legacy_source"] != terminal.envelope["legacy_source"]
        ):
            raise _error("reset_recovery_invalid")
    return ordered, hashes


def _select_chain(
    root: Path,
    generations: list[Generation],
    expected_token: str,
    current_commit: str,
) -> tuple[list[Generation], list[str]]:
    try:
        chain = _validate_chain(generations, expected_token, current_commit)
        return chain, [item.content_hash for item in chain]
    except legacy.DatabaseResetError:
        return _validate_terminal_subset(root, generations, expected_token, current_commit)


def _stamp_from_paths(backup: str | None, audit: str | None) -> str:
    if not isinstance(backup, str) or not isinstance(audit, str):
        raise _error("reset_plan_stale")
    match = re.fullmatch(
        r"backups/helios-pre-room-shared-reset-([0-9]{8}T[0-9]{9}Z)\.db",
        backup,
    )
    if match is None or audit != f"backups/helios-pre-room-shared-reset-{match.group(1)}.audit.json":
        raise _error("reset_plan_stale")
    try:
        __import__("datetime").datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ")
    except ValueError as error:
        raise _error("reset_plan_stale") from error
    return match.group(1)


def _current_commit(root: Path, paths: Iterable[str]) -> str:
    return _git_evidence(root, paths)[0]


def _inspect_stable_durability_authority(root: Path) -> dict[str, Any]:
    """Read stable volume and directory authority without selecting a flush mode."""

    data = root / "data"
    backups = root / "backups"
    try:
        legacy._safe_directory(root)
        legacy._safe_directory(data)
        backups_info = legacy._safe_directory(backups, required=False)
        paths = [root, data] + ([backups] if backups_info is not None else [])
        volume_roots = [_volume_root(path) for path in paths]
        if len(set(volume_roots)) != 1:
            raise _error("reset_recovery_invalid")
        volume_root = volume_roots[0]
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(volume_root))
        serial32, filesystem = _volume_information(volume_root)
        guid = _volume_guid(volume_root)
        identities = {
            "repository": legacy._path_identity(root),
            "data": legacy._path_identity(data),
            "backups": (
                None if backups_info is None else legacy._path_identity(backups)
            ),
        }
    except legacy.DatabaseResetError as error:
        if error.code in {"reset_path_unsafe", "reset_recovery_invalid"}:
            raise _error("reset_recovery_invalid") from error
        raise
    serials = {
        identity.split(":")[1]
        for identity in identities.values()
        if identity is not None
    }
    if len(serials) != 1:
        raise _error("reset_recovery_invalid")
    return {
        "drive_type": drive_type,
        "filesystem_name": filesystem,
        "handle_volume_serial_hex": next(iter(serials)),
        "identities": identities,
        "volume_guid": guid,
        "volume_information_serial_hex": f"{serial32:08x}",
        "volume_root_resolved": True,
    }


def _revalidate_persisted_authority(root: Path, persisted: dict[str, Any]) -> dict[str, Any]:
    """Validate stable evidence and execute the already selected operation."""

    _validate_durability(persisted)
    try:
        stable_now = _inspect_stable_durability_authority(root)
    except legacy.DatabaseResetError as error:
        if error.code == "reset_durability_unsupported":
            raise _error("reset_failed") from error
        raise
    persisted_probes = {
        item["name"]: item for item in persisted["directory_probes"]
    }
    stable_fields = (
        "drive_type", "filesystem_name", "handle_volume_serial_hex",
        "volume_information_serial_hex", "volume_guid", "volume_root_resolved",
    )
    if any(stable_now[key] != persisted[key] for key in stable_fields):
        raise _error("reset_recovery_invalid")
    if (
        stable_now["identities"]["repository"]
        != persisted_probes["repository"]["identity"]
        or stable_now["identities"]["data"]
        != persisted_probes["data"]["identity"]
    ):
        raise _error("reset_recovery_invalid")
    current_backup_identity = stable_now["identities"]["backups"]
    if persisted_probes["backups"]["state"] == "present":
        if current_backup_identity != persisted_probes["backups"]["identity"]:
            raise _error("reset_recovery_invalid")
    elif (
        current_backup_identity is not None
        and current_backup_identity.split(":")[1]
        != persisted["handle_volume_serial_hex"]
    ):
        raise _error("reset_recovery_invalid")
    if persisted["mode"] == "directory_flush":
        paths = {
            "repository": root,
            "data": root / "data",
            "backups": root / "backups",
        }
        try:
            current_probes = [
                _directory_probe(
                    name,
                    paths[name],
                    persisted_probes[name]["identity"],
                )
                for name in ("repository", "data", "backups")
            ]
        except legacy.DatabaseResetError as error:
            raise _error("reset_failed") from error
        if any(item["flush_succeeded"] is not True for item in current_probes):
            raise _error("reset_failed")
    else:
        try:
            _open_and_flush_volume(
                persisted["volume_guid"], persisted["handle_volume_serial_hex"]
            )
        except legacy.DatabaseResetError as error:
            raise _error("reset_failed") from error
    return persisted


@dataclass
class _ControlPartial:
    path: Path
    descriptor: int
    parent_handle: int
    parent_identity: str
    identity: str

    @property
    def handle(self) -> int:
        import msvcrt

        return int(msvcrt.get_osfhandle(self.descriptor))


@contextmanager
def _open_control_partial(path: Path, *, create_new: bool):
    if os.name != "nt":
        raise _error("reset_platform_unsupported")
    import msvcrt

    with legacy._verified_parent(path) as (parent_handle, parent_identity):
        legacy._revalidate_parent(path, parent_identity)
        handle = legacy._windows_create_relative(
            parent_handle,
            path.name,
            directory=False,
            create_new=create_new,
            desired_access=(
                0x80000000 | 0x40000000 | 0x00010000
                | 0x00100000 | 0x00000080
            ),
            share_access=0,
        )
        descriptor: int | None = None
        try:
            descriptor = msvcrt.open_osfhandle(
                handle, getattr(os, "O_BINARY", 0) | os.O_RDWR
            )
            handle = 0
            raw_handle = int(msvcrt.get_osfhandle(descriptor))
            legacy._windows_validate_handle(raw_handle, directory=False)
            identity = legacy._windows_handle_identity(raw_handle)
            if (
                legacy._relative_entry_identity(path, parent_handle)
                != identity
            ):
                raise _error("reset_path_unsafe")
            yield _ControlPartial(
                path,
                descriptor,
                parent_handle,
                parent_identity,
                identity,
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif handle:
                legacy._windows_close_handle(handle)


def _control_bytes(control: _ControlPartial, *, maximum: int = 1_048_576) -> bytes:
    os.lseek(control.descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    length = 0
    while True:
        block = os.read(control.descriptor, min(65_536, maximum + 1 - length))
        if not block:
            break
        chunks.append(block)
        length += len(block)
        if length > maximum:
            raise _error("reset_recovery_invalid")
    info = os.fstat(control.descriptor)
    if (
        info.st_nlink != 1
        or info.st_size != length
        or legacy._descriptor_identity(control.descriptor, info)
        != control.identity
    ):
        raise _error("reset_path_unsafe")
    return b"".join(chunks)


def _control_sha256(control: _ControlPartial) -> str:
    os.lseek(control.descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    before = os.fstat(control.descriptor)
    while True:
        block = os.read(control.descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(control.descriptor)
    if (
        before.st_nlink != 1
        or after.st_nlink != 1
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or legacy._descriptor_identity(control.descriptor, before)
        != control.identity
        or legacy._descriptor_identity(control.descriptor, after)
        != control.identity
    ):
        raise _error("reset_path_unsafe")
    return digest.hexdigest()


def _promote_open_control(
    control: _ControlPartial,
    final: Path,
    expected_raw: bytes | None,
    *,
    expected_sha256: str | None = None,
    crash_checkpoint: Callable[[str], None] | None = None,
    checkpoint_prefix: str | None = None,
) -> str:
    if final.parent != control.path.parent:
        raise _error("reset_path_unsafe")
    try:
        legacy._windows_flush_file_handle(control.handle)
        if expected_raw is not None:
            if _control_bytes(control) != expected_raw:
                raise _error("reset_recovery_invalid")
            content_hash = _sha256(expected_raw)
        else:
            content_hash = _control_sha256(control)
        if expected_sha256 is not None and content_hash != expected_sha256:
            raise _error("reset_recovery_invalid")
        if crash_checkpoint is not None and checkpoint_prefix is not None:
            crash_checkpoint(f"{checkpoint_prefix}:generation_file_durable")
            crash_checkpoint(f"{checkpoint_prefix}:generation_before_promotion")
        legacy._revalidate_parent(control.path, control.parent_identity)
        if (
            legacy._relative_entry_identity(
                control.path, control.parent_handle
            )
            != control.identity
            or legacy._relative_entry_exists(final, control.parent_handle)
        ):
            raise _error("reset_recovery_invalid")
        legacy._windows_rename(
            control.handle, final, control.parent_handle, replace=False
        )
        legacy._windows_flush_file_handle(control.handle)
        if crash_checkpoint is not None and checkpoint_prefix is not None:
            crash_checkpoint(f"{checkpoint_prefix}:generation_promoted")
        legacy._revalidate_parent(final, control.parent_identity)
        if (
            legacy._relative_entry_identity(final, control.parent_handle)
            != control.identity
        ):
            raise _error("reset_failed")
    except legacy.DatabaseResetError as error:
        if error.code == "reset_failed":
            raise _DurabilityBoundaryError(error.code, error.message) from error
        raise
    return content_hash


def _write_and_install_json(
    partial: Path,
    final: Path,
    value: Any,
    authority: dict[str, Any],
    *,
    crash_checkpoint: Callable[[str], None] | None = None,
    partial_checkpoint: str | None = None,
) -> tuple[bytes, str]:
    raw = _canonical_bytes(value)
    with _open_control_partial(partial, create_new=True) as control:
        if crash_checkpoint is not None and partial_checkpoint is not None:
            boundary = max(1, len(raw) // 2)
            legacy._write_all(control.descriptor, raw[:boundary])
            crash_checkpoint(partial_checkpoint)
            legacy._write_all(control.descriptor, raw[boundary:])
        else:
            legacy._write_all(control.descriptor, raw)
        identity = control.identity
        _promote_open_control(control, final, raw)
    _flush_namespace(authority, final.parent)
    return raw, identity


def _promote_existing_json_partial(
    partial: Path,
    final: Path,
    expected_raw: bytes,
    expected_identity: str,
    authority: dict[str, Any],
) -> None:
    with _open_control_partial(partial, create_new=False) as control:
        if control.identity != expected_identity:
            raise _error("reset_path_unsafe")
        _promote_open_control(control, final, expected_raw)
    _flush_namespace(authority, final.parent)


@dataclass
class _SqliteStaging:
    path: Path
    parent_handle: int
    parent_identity: str
    guard_handle: int
    identity: str
    elevated: _ControlPartial | None = None


@contextmanager
def _open_sqlite_staging(path: Path):
    if os.name != "nt":
        raise _error("reset_platform_unsupported")
    with legacy._verified_parent(path) as (parent_handle, parent_identity):
        legacy._revalidate_parent(path, parent_identity)
        guard_handle = legacy._windows_create_relative(
            parent_handle,
            path.name,
            directory=False,
            create_new=True,
            desired_access=0x00100000 | 0x00000080,
            share_access=0x1 | 0x2 | 0x4,
        )
        staging: _SqliteStaging | None = None
        try:
            legacy._windows_validate_handle(guard_handle, directory=False)
            identity = legacy._windows_handle_identity(guard_handle)
            if (
                legacy._relative_entry_identity(path, parent_handle)
                != identity
            ):
                raise _error("reset_path_unsafe")
            staging = _SqliteStaging(
                path,
                parent_handle,
                parent_identity,
                guard_handle,
                identity,
            )
            yield staging
        finally:
            if staging is not None and staging.elevated is not None:
                os.close(staging.elevated.descriptor)
                staging.elevated = None
            if staging is None or staging.guard_handle:
                legacy._windows_close_handle(
                    guard_handle if staging is None else staging.guard_handle
                )


def _elevate_sqlite_staging(staging: _SqliteStaging) -> _ControlPartial:
    if staging.elevated is not None or not staging.guard_handle:
        raise _error("reset_path_unsafe")
    import msvcrt

    legacy._revalidate_parent(staging.path, staging.parent_identity)
    if (
        legacy._relative_entry_identity(staging.path, staging.parent_handle)
        != staging.identity
    ):
        raise _error("reset_path_unsafe")
    elevated_handle = legacy._windows_create_relative(
        staging.parent_handle,
        staging.path.name,
        directory=False,
        create_new=False,
        desired_access=(
            0x80000000 | 0x40000000 | 0x00010000
            | 0x00100000 | 0x00000080
        ),
        share_access=0,
    )
    descriptor: int | None = None
    try:
        legacy._windows_validate_handle(elevated_handle, directory=False)
        if legacy._windows_handle_identity(elevated_handle) != staging.identity:
            raise _error("reset_path_unsafe")
        snapshot = legacy._windows_handle_snapshot(elevated_handle)
        if (
            snapshot["file_id"] != staging.identity
            or snapshot["directory"]
            or snapshot["delete_pending"]
            or snapshot["link_count"] != 1
            or snapshot["reparse_tag"] != 0
        ):
            raise _error("reset_path_unsafe")
        legacy._revalidate_parent(staging.path, staging.parent_identity)
        if (
            legacy._relative_entry_identity(
                staging.path, staging.parent_handle
            )
            != staging.identity
        ):
            raise _error("reset_path_unsafe")
        descriptor = msvcrt.open_osfhandle(
            elevated_handle, getattr(os, "O_BINARY", 0) | os.O_RDWR
        )
        elevated_handle = 0
        legacy._windows_close_handle(staging.guard_handle)
        staging.guard_handle = 0
        staging.elevated = _ControlPartial(
            staging.path,
            descriptor,
            staging.parent_handle,
            staging.parent_identity,
            staging.identity,
        )
        return staging.elevated
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        elif elevated_handle:
            legacy._windows_close_handle(elevated_handle)
        raise


def _native_audit_value(store: GenerationStore, journal: dict[str, Any], outcome: str, terminal: str) -> dict[str, Any]:
    value = legacy._audit_value(journal, outcome, terminal)
    value["reset_protocol_version"] = RESET_PROTOCOL_VERSION
    hashes = store.hashes()
    value.update({
        "generation_chain_sha256": hashes,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "terminal_generation_sequence": len(hashes),
        "terminal_generation_sha256": hashes[-1],
    })
    return value


def _validate_native_audit(value: Any, store: GenerationStore, journal: dict[str, Any]) -> dict[str, Any]:
    expected_keys = legacy.AUDIT_FIELDS | {
        "generation_chain_sha256", "journal_storage_protocol_version",
        "terminal_generation_sequence", "terminal_generation_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise _error("reset_recovery_invalid")
    base = {key: value[key] for key in legacy.AUDIT_FIELDS}
    compatibility_journal = {**journal, "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION}
    compatibility_base = {**base, "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION}
    legacy._validate_audit(compatibility_base, compatibility_journal)
    if base["reset_protocol_version"] != RESET_PROTOCOL_VERSION:
        raise _error("reset_recovery_invalid")
    hashes = store.hashes()
    if (
        not isinstance(value["generation_chain_sha256"], list)
        or value["generation_chain_sha256"] != hashes
        or value["journal_storage_protocol_version"] != JOURNAL_STORAGE_PROTOCOL_VERSION
        or type(value["terminal_generation_sequence"]) is not int
        or value["terminal_generation_sequence"] != len(hashes)
        or value["terminal_generation_sha256"] != hashes[-1]
    ):
        raise _error("reset_recovery_invalid")
    return value


def _install_terminal_audit(
    store: GenerationStore,
    journal: dict[str, Any],
    outcome: str,
    terminal: str,
    crash_checkpoint: Callable[[str], None] | None,
) -> dict[str, Any]:
    audit = store.root / journal["audit_path"]
    partial = Path(f"{audit}.partial")
    if store.origin == "native_v2":
        value = _native_audit_value(store, journal, outcome, terminal)
    else:
        value = legacy._audit_value(journal, outcome, terminal)
    if os.path.lexists(audit):
        existing, existing_raw, _identity = _read_canonical(audit)
        if store.origin == "native_v2":
            _validate_native_audit(existing, store, journal)
        else:
            legacy._validate_audit(existing, journal)
        if existing["outcome"] != outcome:
            raise _error("reset_recovery_invalid")
        if os.path.lexists(partial):
            partial_identity, partial_raw = _read_exact_prefix(
                partial, existing_raw
            )
            _unlink_verified_bytes(partial, partial_identity, partial_raw)
            _flush_namespace(store.authority, partial.parent)
        return existing
    promoted = False
    if os.path.lexists(partial):
        try:
            partial_value, partial_raw, partial_identity = _read_canonical(partial)
        except legacy.DatabaseResetError:
            partial_identity = legacy._path_identity(partial)
            _unlink_verified(partial, partial_identity)
            _flush_namespace(store.authority, partial.parent)
        else:
            try:
                if store.origin == "native_v2":
                    _validate_native_audit(partial_value, store, journal)
                else:
                    legacy._validate_audit(partial_value, journal)
            except legacy.DatabaseResetError:
                raise
            if partial_value["outcome"] != outcome:
                raise _error("reset_recovery_invalid")
            _promote_existing_json_partial(
                partial,
                audit,
                partial_raw,
                partial_identity,
                store.authority,
            )
            promoted = True
    if not promoted:
        _write_and_install_json(
            partial,
            audit,
            value,
            store.authority,
            crash_checkpoint=crash_checkpoint,
            partial_checkpoint="recovery:audit_write_partial",
        )
    if crash_checkpoint is not None:
        crash_checkpoint("recovery:audit_partial_durable")
    return value


def _report(journal: dict[str, Any], status: str, *, native: bool = True) -> dict[str, Any]:
    if status == "reset":
        result = legacy._reset_report({**journal, "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION})
        result["reset_protocol_version"] = RESET_PROTOCOL_VERSION if native else LEGACY_RESET_PROTOCOL_VERSION
    else:
        result = {
            "audit_path": journal["audit_path"],
            "database_path": journal["database_path"],
            "implementation_commit": journal["implementation_commit"],
            "old_schema_label": journal["source_schema_label"],
            "plan_token": journal["plan_token"],
            "recovery_commit": journal["recovery_commit"],
            "reset_protocol_version": RESET_PROTOCOL_VERSION if native else LEGACY_RESET_PROTOCOL_VERSION,
            "schema_label": journal["source_schema_label"],
            "status": "restored_source",
        }
    if native:
        result["journal_storage_protocol_version"] = JOURNAL_STORAGE_PROTOCOL_VERSION
    return result


def _delete_generation_controls(store: GenerationStore) -> None:
    if not store.generations:
        return
    terminal = store.generations[-1]

    def validate_remaining() -> None:
        remaining = [
            _read_generation(store.data / name, partial=False)
            for name in _enumerate_reserved(store.data)
            if GENERATION_RE.fullmatch(name)
        ]
        if not remaining:
            raise _error("reset_recovery_invalid")
        prospective = [f"data/{item.path.name}" for item in remaining]
        current_commit = _current_commit(store.root, prospective)
        _validate_terminal_subset(
            store.root, remaining, store.token, current_commit
        )

    validate_remaining()
    for name in _enumerate_reserved(store.data):
        if not name.endswith(GENERATION_PARTIAL_SUFFIX):
            continue
        path = store.data / name
        generation = _read_generation(path, partial=True)
        if (
            generation.token != store.token
            or generation.sequence > len(store.chain_hashes)
            or generation.content_hash != store.chain_hashes[generation.sequence - 1]
        ):
            raise _error("reset_recovery_invalid")
        _unlink_verified_bytes(path, generation.identity, generation.raw)
        _flush_namespace(store.authority, store.data)
        validate_remaining()
    for generation in sorted(
        store.generations[:-1], key=lambda item: item.sequence
    ):
        if os.path.lexists(generation.path):
            _unlink_verified_bytes(
                generation.path, generation.identity, generation.raw
            )
            _flush_namespace(store.authority, store.data)
            validate_remaining()
    if _enumerate_reserved(store.data) != [terminal.path.name]:
        raise _error("reset_recovery_invalid")
    if os.path.lexists(terminal.path):
        _unlink_verified_bytes(terminal.path, terminal.identity, terminal.raw)
        _flush_namespace(store.authority, store.data)
    if _enumerate_reserved(store.data):
        raise _error("reset_recovery_invalid")


def _component_paths(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Path]]:
    paths = manifest["artifact_paths"]
    active = {
        "database": root / paths["database_path"],
        "wal": root / paths["database_wal_path"],
        "shm": root / paths["database_shm_path"],
    }
    quarantine = {
        "database": root / paths["original_quarantine_database_path"],
        "wal": root / paths["original_quarantine_wal_path"],
        "shm": root / paths["original_quarantine_shm_path"],
    }
    return active, quarantine


def execute_database_reset(
    database_path: Path | str,
    *,
    expected_plan_token: str | None,
    expected_backup_path: str | None,
    expected_audit_path: str | None,
    confirm_destroy_canonical_history: bool,
    repository_root: Path | str = PROJECT_ROOT,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not confirm_destroy_canonical_history or not all((expected_plan_token, expected_backup_path, expected_audit_path)):
        raise _error("reset_confirmation_required")
    if not _valid_sha(expected_plan_token):
        raise _error("reset_plan_stale")
    stamp = _stamp_from_paths(expected_backup_path, expected_audit_path)
    _require_windows()
    root = Path(repository_root).resolve(strict=True)
    paths = _artifact_paths(stamp)
    _git_evidence(root, _prospective_paths(paths))
    if _enumerate_reserved(root / "data"):
        raise _error("database_reset_recovery_required")
    preliminary = _probe_durability(root)
    database = root / "data" / "helios.db"
    try:
        lease = acquire_database_lease(database, shared=False)
    except ResetRecoveryRequiredError as error:
        raise _error("database_reset_recovery_required") from error
    except MaintenanceLockError as error:
        raise _error("database_maintenance_in_progress") from error
    store: GenerationStore | None = None
    backup = root / expected_backup_path
    audit = root / expected_audit_path
    created_backups = False
    try:
        under_lease = _probe_durability(root)
        if under_lease != preliminary:
            raise _error("reset_plan_stale")
        plan = _build_plan(root, database_path, stamp=stamp, durability=under_lease)
        if (
            plan["plan_token"] != expected_plan_token
            or plan["backup_path"] != expected_backup_path
            or plan["audit_path"] != expected_audit_path
        ):
            raise _error("reset_plan_stale")
        manifest = plan["reviewed_plan_manifest"]
        before = legacy._observations(database)
        backups = root / "backups"
        if not os.path.lexists(backups):
            legacy._mkdir_verified(backups)
            _flush_namespace(under_lease, root)
            created_backups = True
        partial = Path(f"{backup}.partial")
        if any(os.path.lexists(item) for item in (backup, partial, audit, Path(f"{audit}.partial"))):
            raise _error("reset_failed")
        with _open_sqlite_staging(partial) as backup_staging:
            source = legacy._open_reset_source(database)
            try:
                source_label, source_digest = legacy._validate_source(source)
                target = legacy._open_direct(partial, read_only=False)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.rollback()
                source.close()
            after = legacy._observations(database)
            if before["database"] != after["database"] or before["wal"] != after["wal"]:
                raise _error("reset_failed")
            if (
                legacy._relative_entry_identity(
                    partial, backup_staging.parent_handle
                )
                != backup_staging.identity
            ):
                raise _error("reset_path_unsafe")
            verified = legacy._open_connection(partial, immutable=True)
            try:
                backup_label, backup_digest = legacy._validate_source(verified)
            finally:
                verified.rollback()
                verified.close()
            if (backup_label, backup_digest) != (source_label, source_digest):
                raise _error("reset_failed")
            if (
                legacy._relative_entry_identity(
                    partial, backup_staging.parent_handle
                )
                != backup_staging.identity
            ):
                raise _error("reset_path_unsafe")
            backup_control = _elevate_sqlite_staging(backup_staging)
            backup_sha = _promote_open_control(
                backup_control, backup, expected_raw=None
            )
        _flush_namespace(under_lease, backup.parent)
        active, quarantine_paths = _component_paths(root, manifest)
        source_components = {
            key: (legacy._component(path, f"data/{path.name}") if os.path.lexists(path) else None)
            for key, path in active.items()
        }
        source_files = {
            key: None if item is None else legacy._source_file(item)
            for key, item in source_components.items()
        }
        quarantine = {
            key: None if source_files[key] is None else {
                "identity": source_files[key]["identity"],
                "path": quarantine_paths[key].relative_to(root).as_posix(),
            }
            for key in active
        }
        journal = {
            "audit_path": expected_audit_path,
            "backup": {"identity": legacy._path_identity(backup), "path": expected_backup_path, "sha256": backup_sha},
            "database_path": "data/helios.db",
            "implementation_commit": plan["implementation_commit"],
            "journal_sequence": 0,
            "journal_version": JOURNAL_VERSION,
            "logical_digest": source_digest,
            "plan_manifest_sha256": expected_plan_token,
            "plan_token": expected_plan_token,
            "quarantine": quarantine,
            "recovery_commit": legacy.RECOVERY_COMMIT_BY_SCHEMA[source_label],
            "reset_protocol_version": RESET_PROTOCOL_VERSION,
            "source_files": source_files,
            "source_observations": {"before_open": before, "after_close": after},
            "source_schema_label": source_label,
            "stage": "ready_to_quarantine",
            "updated_at": legacy._utc_stamp()[1],
        }
        store = GenerationStore(
            root, expected_plan_token, under_lease,
            origin="native_v2", reviewed_plan_manifest=manifest,
        )
        store.append(journal, "ready_to_quarantine", crash_checkpoint)
        for key in ("database", "wal", "shm"):
            if quarantine[key] is None:
                continue
            store.append(journal, "quarantining", crash_checkpoint)
            _move_verified(active[key], quarantine_paths[key], quarantine[key]["identity"])
            _flush_namespace(under_lease, root / "data")
        store.append(journal, "original_quarantined", crash_checkpoint)
        fresh_staging = root / manifest["artifact_paths"]["fresh_database_staging_path"]
        store.append(journal, "installing_fresh", crash_checkpoint)
        with _open_sqlite_staging(fresh_staging) as fresh_staging_state:
            template = sqlite3.connect(":memory:")
            template.row_factory = sqlite3.Row
            template.execute("PRAGMA foreign_keys=ON")
            try:
                template.executescript(
                    legacy.DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8")
                )
                legacy._seed_initial_room(template)
                fresh = legacy._open_direct(fresh_staging, read_only=False)
                try:
                    template.backup(fresh)
                finally:
                    fresh.close()
            finally:
                template.close()
            if any(
                os.path.lexists(Path(f"{fresh_staging}{suffix}"))
                for suffix in ("-wal", "-shm", "-journal")
            ):
                raise _error("reset_failed")
            if (
                legacy._relative_entry_identity(
                    fresh_staging, fresh_staging_state.parent_handle
                )
                != fresh_staging_state.identity
            ):
                raise _error("reset_path_unsafe")
            store.append(journal, "installing_fresh", crash_checkpoint)
            fresh_control = _elevate_sqlite_staging(fresh_staging_state)
            _promote_open_control(
                fresh_control, database, expected_raw=None
            )
        _flush_namespace(under_lease, database.parent)
        fresh = legacy._open_direct(database, read_only=False)
        try:
            mode = fresh.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise _error("reset_failed")
        finally:
            fresh.close()
        store.append(journal, "fresh_installed", crash_checkpoint)
        store.append(journal, "validating_fresh", crash_checkpoint)
        fresh = legacy._open_direct(database, read_only=True)
        try:
            validate_v14_foundation(fresh)
            validate_database_integrity(fresh)
        finally:
            fresh.rollback()
            fresh.close()
        store.append(journal, "fresh_validated", crash_checkpoint)
        for key in ("database", "wal", "shm"):
            item = quarantine[key]
            if item is None or not os.path.lexists(quarantine_paths[key]):
                continue
            store.append(journal, "cleaning_quarantine", crash_checkpoint)
            _unlink_verified(quarantine_paths[key], item["identity"])
            _flush_namespace(under_lease, quarantine_paths[key].parent)
        store.append(journal, "finalizing", crash_checkpoint)
        _install_terminal_audit(store, journal, "reset", "1.4", crash_checkpoint)
        _delete_generation_controls(store)
        return _report(journal, "reset")
    except legacy.DatabaseResetError as error:
        if store is None:
            _cleanup_prejournal_artifacts(
                backup, audit, created_backups,
                locals().get("under_lease", preliminary),
            )
        elif not isinstance(error, _DurabilityBoundaryError):
            try:
                _restore_v2(store, store.journal, crash_checkpoint=None)
            except Exception:
                pass
        raise
    except Exception as error:
        if store is None:
            _cleanup_prejournal_artifacts(
                backup, audit, created_backups,
                locals().get("under_lease", preliminary),
            )
        elif store.generations:
            try:
                _restore_v2(store, store.journal, crash_checkpoint=None)
            except Exception:
                pass
        raise _error("reset_failed") from error
    finally:
        lease.close()


EVIDENCE_RECORD_KEYS = {
    "converter_implementation_commit", "evidence_record_version",
    "journal_storage_protocol_version", "legacy_plan_token",
    "legacy_recovery_evidence", "legacy_source", "origin",
    "selected_legacy_journal_sha256", "selected_legacy_path",
}


@dataclass
class LegacySelection:
    journal: dict[str, Any]
    selected_path: Path
    source: dict[str, Any]


@dataclass
class EvidenceRecord:
    path: Path
    partial: bool
    token: str
    content_hash: str
    value: dict[str, Any]
    raw: bytes
    identity: str


def _safe_raw_hash(path: Path) -> str:
    return legacy._stable_sha256(path)


def _decimal_prefix_possible(
    fragment: str, width: int, minimum: int, maximum: int
) -> bool:
    return (
        len(fragment) <= width
        and fragment.isdigit()
        and any(
            str(value).zfill(width).startswith(fragment)
            for value in range(minimum, maximum + 1)
        )
    )


def _utc_millisecond_prefix_possible(raw: bytes) -> bool:
    if len(raw) > 24:
        return False
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        return False
    literals = {4: "-", 7: "-", 10: "T", 13: ":", 16: ":", 19: ".", 23: "Z"}
    for index, character in enumerate(value):
        expected = literals.get(index)
        if expected is not None:
            if character != expected:
                return False
        elif not character.isdigit():
            return False
    fields = (
        (0, 4, 1, 9999),
        (5, 7, 1, 12),
        (8, 10, 1, 31),
        (11, 13, 0, 23),
        (14, 16, 0, 59),
        (17, 19, 0, 59),
        (20, 23, 0, 999),
    )
    for start, end, minimum, maximum in fields:
        if len(value) <= start:
            continue
        fragment = value[start:min(len(value), end)]
        if not _decimal_prefix_possible(fragment, end - start, minimum, maximum):
            return False
    if len(value) >= 10:
        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        if day > calendar.monthrange(year, month)[1]:
            return False
    return True


def _is_demonstrably_incomplete_legacy_next(
    raw: bytes,
    journal: dict[str, Any],
    *,
    expected_token: str,
    implementation_commit: str,
) -> bool:
    marker = b"2000-01-01T00:00:00.000Z"
    for stage in STAGES:
        successor = dict(journal)
        successor["journal_sequence"] = journal["journal_sequence"] + 1
        successor["stage"] = stage
        successor["updated_at"] = marker.decode("ascii")
        try:
            legacy._validate_journal(
                successor,
                expected_token=expected_token,
                implementation_commit=implementation_commit,
            )
        except legacy.DatabaseResetError:
            continue
        candidate = _canonical_bytes(successor)
        marker_at = candidate.find(marker)
        if marker_at < 0 or candidate.find(marker, marker_at + 1) >= 0:
            raise _error("reset_recovery_invalid")
        before = candidate[:marker_at]
        after = candidate[marker_at + len(marker):]
        if len(raw) <= len(before):
            if raw == before[:len(raw)] and len(raw) < len(candidate):
                return True
            continue
        if raw[:len(before)] != before:
            continue
        timestamp_length = min(len(raw) - len(before), len(marker))
        timestamp = raw[len(before):len(before) + timestamp_length]
        if not _utc_millisecond_prefix_possible(timestamp):
            continue
        if timestamp_length < len(marker):
            return True
        if not legacy._valid_utc_millisecond(timestamp.decode("ascii")):
            continue
        remainder = raw[len(before) + len(marker):]
        if len(remainder) < len(after) and remainder == after[:len(remainder)]:
            return True
    return False


def _select_legacy(root: Path, expected_token: str) -> LegacySelection:
    data = root / "data"
    state = data / ".helios-room-reset-state.json"
    next_path = data / ".helios-room-reset-state.json.next"
    present = [path for path in (state, next_path) if os.path.lexists(path)]
    if not present:
        raise _error("reset_recovery_invalid")
    hashes = {
        "journal": _safe_raw_hash(state) if state in present else None,
        "next": _safe_raw_hash(next_path) if next_path in present else None,
    }
    valid: dict[str, dict[str, Any]] = {}
    for key, path in (("journal", state), ("next", next_path)):
        if path not in present:
            continue
        try:
            candidate, _raw, _identity = _read_canonical(path)
        except legacy.DatabaseResetError as error:
            if key == "journal" or state not in present or "journal" not in valid:
                raise _error("reset_recovery_invalid") from error
            raw = _stable_partial_snapshot(path)[0]
            journal = valid["journal"]
            if not _is_demonstrably_incomplete_legacy_next(
                raw,
                journal,
                expected_token=expected_token,
                implementation_commit=journal["implementation_commit"],
            ):
                raise _error("reset_recovery_invalid") from error
            continue
        if not isinstance(candidate, dict):
            raise _error("reset_recovery_invalid")
        commit = candidate.get("implementation_commit")
        if commit not in LEGACY_V1_IMPLEMENTATION_COMMITS:
            raise _error("reset_recovery_invalid")
        valid[key] = legacy._validate_journal(
            candidate, expected_token=expected_token,
            implementation_commit=commit,
        )
    if "journal" in valid:
        selected = "journal"
        if "next" in valid:
            if (
                valid["next"]["journal_sequence"]
                != valid["journal"]["journal_sequence"] + 1
                or valid["next"]["plan_token"] != valid["journal"]["plan_token"]
            ):
                raise _error("reset_recovery_invalid")
            selected = "next"
    elif "next" in valid and valid["next"]["journal_sequence"] == 1:
        selected = "next"
    else:
        raise _error("reset_recovery_invalid")
    selected_path = state if selected == "journal" else next_path
    source = {
        "journal_path": "data/.helios-room-reset-state.json",
        "journal_sha256": hashes["journal"],
        "next_path": "data/.helios-room-reset-state.json.next",
        "next_sha256": hashes["next"],
        "selected": selected,
    }
    _validate_legacy_source(source)
    return LegacySelection(valid[selected], selected_path, source)


def _legacy_recovery_evidence(
    token: str, converter: str, durability: dict[str, Any]
) -> dict[str, Any]:
    value = {
        "converter_implementation_commit": converter,
        "durability": durability,
        "evidence_version": 1,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "legacy_plan_token": token,
        "platform": "windows",
        "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION,
    }
    return _validate_legacy_evidence(value, token, converter)


def _evidence_value(
    selection: LegacySelection,
    token: str,
    converter: str,
    durability: dict[str, Any],
) -> dict[str, Any]:
    evidence = _legacy_recovery_evidence(token, converter, durability)
    selected_key = selection.source["selected"]
    return {
        "converter_implementation_commit": converter,
        "evidence_record_version": 1,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "legacy_plan_token": token,
        "legacy_recovery_evidence": evidence,
        "legacy_source": selection.source,
        "origin": "legacy_v1_conversion",
        "selected_legacy_journal_sha256": selection.source[f"{selected_key}_sha256"],
        "selected_legacy_path": selection.selected_path.relative_to(selection.selected_path.parents[1]).as_posix(),
    }


def _evidence_name(token: str, digest: str) -> str:
    return f".helios-room-reset-state.{token}.legacy-evidence.{digest}.json"


def _validate_evidence_record(path: Path, *, partial: bool, expected_token: str, converter: str) -> EvidenceRecord:
    base = path.name[:-len(GENERATION_PARTIAL_SUFFIX)] if partial else path.name
    match = EVIDENCE_RE.fullmatch(base)
    if match is None or match.group(1) != expected_token:
        raise _error("reset_recovery_invalid")
    token, digest = match.groups()
    value, raw, identity = _read_canonical(path)
    if not isinstance(value, dict) or set(value) != EVIDENCE_RECORD_KEYS:
        raise _error("reset_recovery_invalid")
    if (
        value["converter_implementation_commit"] != converter
        or type(value["evidence_record_version"]) is not int
        or value["evidence_record_version"] != 1
        or value["journal_storage_protocol_version"] != JOURNAL_STORAGE_PROTOCOL_VERSION
        or value["legacy_plan_token"] != token
        or value["origin"] != "legacy_v1_conversion"
        or value["selected_legacy_path"] not in {
            "data/.helios-room-reset-state.json",
            "data/.helios-room-reset-state.json.next",
        }
        or _sha256(raw) != digest
    ):
        raise _error("reset_recovery_invalid")
    source = _validate_legacy_source(value["legacy_source"])
    selected = source["selected"]
    if (
        value["selected_legacy_path"]
        != source[f"{selected}_path"]
        or value["selected_legacy_journal_sha256"]
        != source[f"{selected}_sha256"]
    ):
        raise _error("reset_recovery_invalid")
    _validate_legacy_evidence(value["legacy_recovery_evidence"], token, converter)
    return EvidenceRecord(path, partial, token, digest, value, raw, identity)


def _stable_partial_snapshot(path: Path) -> tuple[bytes, str, int, int]:
    info = legacy._safe_regular(path)
    if info.st_size < 0 or info.st_size > 1_048_576:
        raise _error("reset_recovery_invalid")
    identity = legacy._path_identity(path)
    descriptor = legacy._open_existing_regular(
        path, identity, share_write=False
    )
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = 1_048_577
        while remaining:
            block = os.read(descriptor, min(65_536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        if remaining == 0:
            raise _error("reset_recovery_invalid")
        after = os.fstat(descriptor)
        if (
            before.st_nlink != 1
            or after.st_nlink != 1
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or legacy._descriptor_identity(descriptor, before) != identity
            or legacy._descriptor_identity(descriptor, after) != identity
        ):
            raise _error("reset_recovery_invalid")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) != after.st_size:
        raise _error("reset_recovery_invalid")
    return raw, identity, after.st_size, after.st_mtime_ns


def _expected_legacy_names(source: dict[str, Any]) -> list[str]:
    names = []
    if source["journal_sha256"] is not None:
        names.append(".helios-room-reset-state.json")
    if source["next_sha256"] is not None:
        names.append(".helios-room-reset-state.json.next")
    return sorted(names, key=lambda item: item.encode("utf-8"))


def _revalidate_legacy_context(
    root: Path,
    expected_token: str,
    converter: str,
    selection: LegacySelection,
    expected_reserved: list[str],
) -> None:
    if _enumerate_reserved(root / "data") != sorted(
        expected_reserved, key=lambda item: item.encode("utf-8")
    ):
        raise _error("reset_recovery_invalid")
    selected = _select_legacy(root, expected_token)
    if selected.source != selection.source or selected.journal != selection.journal:
        raise _error("reset_recovery_invalid")
    prospective = [f"data/{name}" for name in expected_reserved]
    if _current_commit(root, prospective) != converter:
        raise _error("reset_recovery_invalid")


def _validate_legacy_controls_subset(
    root: Path, source: dict[str, Any], *, terminal_cleanup: bool
) -> None:
    _validate_legacy_source(source)
    data = root / "data"
    state = data / ".helios-room-reset-state.json"
    next_path = data / ".helios-room-reset-state.json.next"
    present = {
        "journal": os.path.lexists(state),
        "next": os.path.lexists(next_path),
    }
    originally = {
        "journal": source["journal_sha256"] is not None,
        "next": source["next_sha256"] is not None,
    }
    if any(present[key] and not originally[key] for key in present):
        raise _error("reset_recovery_invalid")
    if not terminal_cleanup and present != originally:
        raise _error("reset_recovery_invalid")
    if terminal_cleanup and originally == {"journal": True, "next": True}:
        if present == {"journal": False, "next": True}:
            raise _error("reset_recovery_invalid")
    for key, path in (("journal", state), ("next", next_path)):
        if present[key] and _safe_raw_hash(path) != source[f"{key}_sha256"]:
            raise _error("reset_recovery_invalid")


def _write_evidence_record(
    root: Path,
    value: dict[str, Any],
    authority: dict[str, Any],
    crash_checkpoint: Callable[[str], None] | None,
) -> EvidenceRecord:
    raw = _canonical_bytes(value)
    digest = _sha256(raw)
    name = _evidence_name(value["legacy_plan_token"], digest)
    final = root / "data" / name
    partial = Path(f"{final}{GENERATION_PARTIAL_SUFFIX}")
    legacy._git_safety(root, [f"data/{name}", f"data/{name}{GENERATION_PARTIAL_SUFFIX}"])
    if os.path.lexists(final) or os.path.lexists(partial):
        raise _error("reset_recovery_invalid")
    installed_raw, _identity = _write_and_install_json(
        partial,
        final,
        value,
        authority,
        crash_checkpoint=crash_checkpoint,
        partial_checkpoint="legacy_evidence:partial_write",
    )
    if installed_raw != raw:
        raise _error("reset_failed")
    return _validate_evidence_record(
        final, partial=False, expected_token=value["legacy_plan_token"],
        converter=value["converter_implementation_commit"],
    )


def _handle_evidence_state(
    root: Path,
    expected_token: str,
    converter: str,
    selection: LegacySelection,
    evidence_paths: list[Path],
    generation_partials: list[Path],
    preliminary_authority: dict[str, Any],
    crash_checkpoint: Callable[[str], None] | None,
) -> tuple[EvidenceRecord, dict[str, Any]]:
    _validate_durability(preliminary_authority)
    finals = [path for path in evidence_paths if not path.name.endswith(GENERATION_PARTIAL_SUFFIX)]
    evidence_partials = [path for path in evidence_paths if path.name.endswith(GENERATION_PARTIAL_SUFFIX)]
    if len(finals) > 1 or len(evidence_partials) > 1:
        raise _error("reset_recovery_invalid")
    if finals and evidence_partials:
        final_record = _validate_evidence_record(
            finals[0], partial=False, expected_token=expected_token,
            converter=converter,
        )
        partial_record = _validate_evidence_record(
            evidence_partials[0], partial=True, expected_token=expected_token,
            converter=converter,
        )
        if final_record.raw != partial_record.raw:
            raise _error("reset_recovery_invalid")
        if final_record.value["legacy_source"] != selection.source:
            raise _error("reset_recovery_invalid")
        authority = _revalidate_persisted_authority(
            root, final_record.value["legacy_recovery_evidence"]["durability"]
        )
        _revalidate_legacy_context(
            root, expected_token, converter, selection,
            _expected_legacy_names(selection.source)
            + [finals[0].name, evidence_partials[0].name],
        )
        _unlink_verified_bytes(
            evidence_partials[0], partial_record.identity, partial_record.raw
        )
        _flush_namespace(authority, evidence_partials[0].parent)
        return final_record, authority
    if evidence_paths:
        path = evidence_paths[0]
        partial = path.name.endswith(GENERATION_PARTIAL_SUFFIX)
        if partial:
            try:
                record = _validate_evidence_record(
                    path, partial=True, expected_token=expected_token,
                    converter=converter,
                )
            except legacy.DatabaseResetError as validation_error:
                if generation_partials:
                    raise _error("reset_recovery_invalid")
                try:
                    _read_canonical(path)
                except legacy.DatabaseResetError as canonical_error:
                    if canonical_error.code != "reset_recovery_invalid":
                        raise
                else:
                    # Canonically complete but semantically invalid evidence
                    # is hostile state, not an interrupted write.
                    raise validation_error
                # Revision 2.8 provisional authority is selected under the
                # lease before disposition and expires after durable absence.
                partial_snapshot = _stable_partial_snapshot(path)
                expected_reserved = sorted(
                    _expected_legacy_names(selection.source) + [path.name],
                    key=lambda item: item.encode("utf-8"),
                )
                if _enumerate_reserved(root / "data") != expected_reserved:
                    raise _error("reset_recovery_invalid")
                provisional = _probe_durability(root)
                selection_now = _select_legacy(root, expected_token)
                if selection_now.source != selection.source:
                    raise _error("reset_recovery_invalid")
                if _current_commit(root, [f"data/{path.name}"]) != converter:
                    raise _error("reset_recovery_invalid")
                immediate = _probe_durability(root)
                if immediate != provisional:
                    raise _error("reset_recovery_invalid")
                if (
                    _stable_partial_snapshot(path) != partial_snapshot
                    or _enumerate_reserved(root / "data") != expected_reserved
                ):
                    raise _error("reset_recovery_invalid")
                try:
                    _unlink_verified_bytes(
                        path, partial_snapshot[1], partial_snapshot[0]
                    )
                    _flush_namespace(provisional, path.parent)
                except legacy.DatabaseResetError as error:
                    if error.code == "reset_path_unsafe":
                        raise _error("reset_recovery_invalid") from error
                    raise _error("reset_failed") from error
                try:
                    if os.path.lexists(path):
                        raise _error("reset_failed")
                    if (
                        _enumerate_reserved(root / "data")
                        != _expected_legacy_names(selection.source)
                        or _select_legacy(root, expected_token).source
                        != selection.source
                    ):
                        raise _error("reset_failed")
                except legacy.DatabaseResetError as error:
                    raise _error("reset_failed") from error
                conversion = _probe_durability(root)
                value = _evidence_value(selection, expected_token, converter, conversion)
                return _write_evidence_record(root, value, conversion, crash_checkpoint), conversion
            else:
                if record.value["legacy_source"] != selection.source:
                    raise _error("reset_recovery_invalid")
                final = Path(str(path)[:-len(GENERATION_PARTIAL_SUFFIX)])
                if os.path.lexists(final):
                    final_record = _validate_evidence_record(
                        final, partial=False, expected_token=expected_token,
                        converter=converter,
                    )
                    if final_record.raw != record.raw:
                        raise _error("reset_recovery_invalid")
                    _unlink_verified_bytes(path, record.identity, record.raw)
                    _flush_namespace(record.value["legacy_recovery_evidence"]["durability"], path.parent)
                    return final_record, final_record.value["legacy_recovery_evidence"]["durability"]
                authority = _revalidate_persisted_authority(
                    root, record.value["legacy_recovery_evidence"]["durability"]
                )
                _revalidate_legacy_context(
                    root, expected_token, converter, selection,
                    _expected_legacy_names(selection.source) + [path.name],
                )
                _promote_existing_json_partial(
                    path, final, record.raw, record.identity, authority
                )
                return _validate_evidence_record(
                    final, partial=False, expected_token=expected_token,
                    converter=converter,
                ), authority
        record = _validate_evidence_record(
            path, partial=False, expected_token=expected_token,
            converter=converter,
        )
        if record.value["legacy_source"] != selection.source:
            raise _error("reset_recovery_invalid")
        authority = _revalidate_persisted_authority(
            root, record.value["legacy_recovery_evidence"]["durability"]
        )
        return record, authority
    if generation_partials:
        raise _error("reset_recovery_invalid")
    conversion = _probe_durability(root)
    _revalidate_legacy_context(
        root, expected_token, converter, selection,
        _expected_legacy_names(selection.source),
    )
    value = _evidence_value(selection, expected_token, converter, conversion)
    return _write_evidence_record(root, value, conversion, crash_checkpoint), conversion


def _promote_or_rebuild_legacy_generation_partial(
    store: GenerationStore,
    journal: dict[str, Any],
    partial: Path,
    record: EvidenceRecord,
    crash_checkpoint: Callable[[str], None] | None,
) -> None:
    if store.generations:
        raise _error("reset_recovery_invalid")
    expected_journal = dict(journal)
    expected_journal["journal_sequence"] = journal["journal_sequence"]
    expected_journal["updated_at"] = journal["updated_at"]
    envelope = {
        "converter_implementation_commit": store.converter_implementation_commit,
        "generation_sequence": 1,
        "generation_version": GENERATION_VERSION,
        "journal": expected_journal,
        "legacy_recovery_evidence": store.legacy_recovery_evidence,
        "legacy_source": store.legacy_source,
        "origin": "legacy_v1_conversion",
        "previous_generation_sha256": None,
        "reviewed_plan_manifest": None,
    }
    expected_raw = _canonical_bytes(envelope)
    expected_hash = _sha256(expected_raw)
    match = GENERATION_RE.fullmatch(partial.name[:-len(GENERATION_PARTIAL_SUFFIX)])
    if match is None or match.groups() != (store.token, "00000000000000000001", expected_hash):
        raise _error("reset_recovery_invalid")
    raw, identity, size, mtime_ns = _stable_partial_snapshot(partial)
    final = Path(str(partial)[:-len(GENERATION_PARTIAL_SUFFIX)])
    if raw == expected_raw:
        _promote_existing_json_partial(
            partial, final, raw, identity, store.authority
        )
        store.generations.append(_read_generation(final, partial=False))
        return
    if len(raw) >= len(expected_raw) or expected_raw[:len(raw)] != raw:
        raise _error("reset_recovery_invalid")
    # Exact Revision 2.6 strict-prefix basename reuse exception.
    if _stable_partial_snapshot(partial) != (raw, identity, size, mtime_ns):
        raise _error("reset_recovery_invalid")
    _unlink_verified_bytes(partial, identity, raw)
    _flush_namespace(store.authority, partial.parent)
    if os.path.lexists(partial):
        raise _error("reset_failed")
    store.append(journal, journal["stage"], crash_checkpoint, "legacy_generation_rebuild")
    if store.generations[-1].raw != expected_raw:
        raise _error("reset_recovery_invalid")
    if record.value["legacy_recovery_evidence"] != store.generations[-1].envelope["legacy_recovery_evidence"]:
        raise _error("reset_recovery_invalid")


def _store_from_chain(
    root: Path, chain: list[Generation], chain_hashes: list[str] | None = None
) -> GenerationStore:
    if not chain:
        raise _error("reset_recovery_invalid")
    first = chain[0].envelope
    if first["origin"] == "native_v2":
        authority = first["reviewed_plan_manifest"]["durability"]
    else:
        authority = first["legacy_recovery_evidence"]["durability"]
    return GenerationStore(
        root,
        chain[0].token,
        authority,
        origin=first["origin"],
        reviewed_plan_manifest=first["reviewed_plan_manifest"],
        legacy_recovery_evidence=first["legacy_recovery_evidence"],
        legacy_source=first["legacy_source"],
        converter_implementation_commit=first["converter_implementation_commit"],
        generations=chain,
        chain_hashes=chain_hashes,
    )


def _operational_paths(store: GenerationStore, journal: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path], dict[str, Path]]:
    root = store.root
    active = {
        "database": root / "data/helios.db",
        "wal": root / "data/helios.db-wal",
        "shm": root / "data/helios.db-shm",
    }
    quarantines = {
        key: (None if journal["quarantine"][key] is None else root / journal["quarantine"][key]["path"])
        for key in active
    }
    if store.origin == "native_v2":
        paths = store.reviewed_plan_manifest["artifact_paths"]
        failed = {
            "database": root / paths["failed_new_database_path"],
            "wal": root / paths["failed_new_wal_path"],
            "shm": root / paths["failed_new_shm_path"],
        }
        restoring = {
            "database": root / paths["restoring_database_path"],
            "wal": root / paths["restoring_wal_path"],
            "shm": root / paths["restoring_shm_path"],
        }
    else:
        token = journal["plan_token"]
        failed = {
            key: root / f"data/.{path.name}.reset-{token}.failed-new"
            for key, path in active.items()
        }
        restoring = {
            "database": root / f"data/.helios.db.reset-{token}.restoring",
            "wal": root / f"data/.helios.db-wal.reset-{token}.restoring",
            "shm": root / f"data/.helios.db-shm.reset-{token}.restoring",
        }
    return active, quarantines, failed, restoring


def _active_source_matches(database: Path, journal: dict[str, Any]) -> bool:
    return legacy._active_source_matches(database, journal)


def _legacy_chain_manifest_value(
    store: GenerationStore, journal: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    audit = store.root / journal["audit_path"]
    audit_value, audit_raw, _audit_identity = _read_canonical(audit)
    legacy._validate_audit(audit_value, journal)
    path = Path(f"{audit}.generation-chain.json")
    hashes = store.hashes()
    value = {
        "audit_path": journal["audit_path"],
        "audit_sha256": _sha256(audit_raw),
        "converter_implementation_commit": store.converter_implementation_commit,
        "generation_chain_sha256": hashes,
        "journal_storage_protocol_version": JOURNAL_STORAGE_PROTOCOL_VERSION,
        "origin": "legacy_v1_conversion",
        "plan_token": journal["plan_token"],
        "terminal_generation_sequence": len(hashes),
        "terminal_generation_sha256": hashes[-1],
    }
    return path, value


def _validate_legacy_chain_manifest(
    store: GenerationStore, journal: dict[str, Any]
) -> Path:
    path, expected = _legacy_chain_manifest_value(store, journal)
    if not os.path.lexists(path):
        raise _error("reset_recovery_invalid")
    existing, _raw, _identity = _read_canonical(path)
    if existing != expected:
        raise _error("reset_recovery_invalid")
    return path


def _install_legacy_chain_manifest(store: GenerationStore, journal: dict[str, Any]) -> Path:
    path, value = _legacy_chain_manifest_value(store, journal)
    partial = Path(f"{path}.partial")
    if os.path.lexists(path):
        existing, existing_raw, _identity = _read_canonical(path)
        if existing != value:
            raise _error("reset_recovery_invalid")
        if os.path.lexists(partial):
            partial_identity, partial_raw = _read_exact_prefix(
                partial, existing_raw
            )
            _unlink_verified_bytes(partial, partial_identity, partial_raw)
            _flush_namespace(store.authority, partial.parent)
        return path
    if os.path.lexists(partial):
        try:
            existing, _raw, identity = _read_canonical(partial)
        except legacy.DatabaseResetError:
            identity, partial_raw = _read_exact_prefix(
                partial, _canonical_bytes(value)
            )
            _unlink_verified_bytes(partial, identity, partial_raw)
            _flush_namespace(store.authority, partial.parent)
        else:
            if existing != value:
                raise _error("reset_recovery_invalid")
            _promote_existing_json_partial(
                partial, path, _raw, identity, store.authority
            )
            return path
    _write_and_install_json(partial, path, value, store.authority)
    return path


def _cleanup_matching_evidence_records(
    store: GenerationStore,
    evidence_paths: list[Path],
    after_delete: Callable[[], None] | None = None,
) -> None:
    if not evidence_paths:
        return
    if store.origin != "legacy_v1_conversion" or len(evidence_paths) > 2:
        raise _error("reset_recovery_invalid")
    final_records: list[EvidenceRecord] = []
    partial_records: list[EvidenceRecord] = []
    for path in evidence_paths:
        partial = path.name.endswith(GENERATION_PARTIAL_SUFFIX)
        record = _validate_evidence_record(
            path, partial=partial, expected_token=store.token,
            converter=store.converter_implementation_commit,
        )
        if (
            record.value["legacy_recovery_evidence"] != store.legacy_recovery_evidence
            or record.value["legacy_source"] != store.legacy_source
        ):
            raise _error("reset_recovery_invalid")
        (partial_records if partial else final_records).append(record)
    if len(final_records) > 1 or len(partial_records) > 1:
        raise _error("reset_recovery_invalid")
    if final_records and partial_records and final_records[0].raw != partial_records[0].raw:
        raise _error("reset_recovery_invalid")
    for record in partial_records + final_records:
        _unlink_verified_bytes(record.path, record.identity, record.raw)
        _flush_namespace(store.authority, record.path.parent)
        if after_delete is not None:
            after_delete()


def _revalidate_committed_outcome(
    store: GenerationStore, journal: dict[str, Any]
) -> None:
    audit = _existing_terminal_audit(store, journal)
    if audit is None:
        raise _error("reset_recovery_invalid")
    terminal = store.generations[-1]
    installed_terminal = _read_generation(terminal.path, partial=False)
    if (
        installed_terminal.raw != terminal.raw
        or installed_terminal.identity != terminal.identity
    ):
        raise _error("reset_recovery_invalid")
    database = store.root / journal["database_path"]
    if audit["outcome"] == "restored_source":
        connection = legacy._open_reset_source(database)
        try:
            label, digest = legacy._validate_source(connection)
        finally:
            connection.rollback()
            connection.close()
        if (
            label != journal["source_schema_label"]
            or digest != journal["logical_digest"]
        ):
            raise _error("reset_recovery_invalid")
    elif audit["outcome"] == "reset":
        connection = legacy._open_direct(database, read_only=True)
        try:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
        finally:
            connection.rollback()
            connection.close()
    else:
        raise _error("reset_recovery_invalid")
    if store.origin == "legacy_v1_conversion":
        _validate_legacy_chain_manifest(store, journal)


def _cleanup_after_audit(store: GenerationStore, journal: dict[str, Any]) -> None:
    existing_audit = _existing_terminal_audit(store, journal)
    if existing_audit is None:
        raise _error("reset_recovery_invalid")
    if store.origin == "legacy_v1_conversion":
        _install_legacy_chain_manifest(store, journal)
    _revalidate_committed_outcome(store, journal)
    active, quarantines, failed, restoring = _operational_paths(store, journal)
    del active
    cleanup_collections: list[dict[str, Path | None]] = [quarantines, failed]
    if store.origin == "native_v2":
        paths = store.reviewed_plan_manifest["artifact_paths"]
        cleanup_collections.append({
            "database": store.root / paths["backup_copy_partial_path"]
        })
    cleanup_collections.append(restoring)
    if store.origin == "native_v2":
        paths = store.reviewed_plan_manifest["artifact_paths"]
        cleanup_collections.append({
            "database": store.root / paths["fresh_database_staging_path"],
            "wal": store.root / paths["fresh_wal_staging_path"],
            "shm": store.root / paths["fresh_shm_staging_path"],
        })
    for collection in cleanup_collections:
        for path in collection.values():
            if path is not None and os.path.lexists(path):
                _unlink_verified(path, legacy._path_identity(path))
                _flush_namespace(store.authority, path.parent)
                _revalidate_committed_outcome(store, journal)
    audit_partial = Path(f"{store.root / journal['audit_path']}.partial")
    if os.path.lexists(audit_partial):
        _audit_value, audit_raw, _audit_identity = _read_canonical(
            store.root / journal["audit_path"]
        )
        audit_partial_identity, audit_partial_raw = _read_exact_prefix(
            audit_partial, audit_raw
        )
        _unlink_verified_bytes(
            audit_partial, audit_partial_identity, audit_partial_raw
        )
        _flush_namespace(store.authority, audit_partial.parent)
        _revalidate_committed_outcome(store, journal)
    if store.origin == "legacy_v1_conversion":
        _validate_legacy_controls_subset(
            store.root, store.legacy_source, terminal_cleanup=True
        )
        evidence_paths = [
            store.data / name for name in _enumerate_reserved(store.data)
            if EVIDENCE_RE.fullmatch(
                name[:-len(GENERATION_PARTIAL_SUFFIX)]
                if name.endswith(GENERATION_PARTIAL_SUFFIX) else name
            )
        ]
        _cleanup_matching_evidence_records(
            store,
            evidence_paths,
            after_delete=lambda: _revalidate_committed_outcome(store, journal),
        )
        _validate_legacy_chain_manifest(store, journal)
        source = store.legacy_source
        for key in ("next", "journal"):
            path = store.root / source[f"{key}_path"]
            expected_hash = source[f"{key}_sha256"]
            if expected_hash is None or not os.path.lexists(path):
                continue
            if legacy._stable_sha256(path) != expected_hash:
                raise _error("reset_recovery_invalid")
            _unlink_verified(path, legacy._path_identity(path))
            _flush_namespace(store.authority, path.parent)
            _validate_legacy_controls_subset(
                store.root, store.legacy_source, terminal_cleanup=True
            )
            _revalidate_committed_outcome(store, journal)
    names = (
        legacy._windows_enumerate_directory(store.data)
        if os.name == "nt"
        else [entry.name for entry in os.scandir(store.data)]
    )
    unexpected_operational = sorted(
        name for name in names
        if name.startswith(".helios") and ".reset-" in name
    )
    if unexpected_operational:
        raise _error("reset_recovery_invalid")
    backups = store.root / "backups"
    backup_names = (
        legacy._windows_enumerate_directory(backups)
        if os.name == "nt"
        else [entry.name for entry in os.scandir(backups)]
    )
    forbidden_backup_names = {
        Path(f"{store.root / journal['backup']['path']}.partial").name,
        Path(f"{store.root / journal['backup']['path']}.partial-wal").name,
        Path(f"{store.root / journal['backup']['path']}.partial-shm").name,
        Path(f"{store.root / journal['backup']['path']}.partial-journal").name,
        Path(f"{store.root / journal['audit_path']}.partial").name,
        Path(f"{store.root / journal['audit_path']}.generation-chain.json.partial").name,
    }
    if any(name in forbidden_backup_names for name in backup_names):
        raise _error("reset_recovery_invalid")
    _revalidate_committed_outcome(store, journal)
    _delete_generation_controls(store)


def _existing_terminal_audit(
    store: GenerationStore, journal: dict[str, Any]
) -> dict[str, Any] | None:
    path = store.root / journal["audit_path"]
    if not os.path.lexists(path):
        return None
    value, _raw, _identity = _read_canonical(path)
    if store.origin == "native_v2":
        return _validate_native_audit(value, store, journal)
    return legacy._validate_audit(value, journal)


def _promote_terminal_audit_partial(
    store: GenerationStore, journal: dict[str, Any], expected_outcome: str
) -> dict[str, Any] | None:
    audit = store.root / journal["audit_path"]
    partial = Path(f"{audit}.partial")
    if os.path.lexists(audit) or not os.path.lexists(partial):
        return None
    try:
        value, _raw, identity = _read_canonical(partial)
        if store.origin == "native_v2":
            _validate_native_audit(value, store, journal)
        else:
            legacy._validate_audit(value, journal)
        if value["outcome"] != expected_outcome:
            raise _error("reset_recovery_invalid")
    except legacy.DatabaseResetError as error:
        if error.code == "reset_path_unsafe":
            raise
        try:
            _read_canonical(partial)
        except legacy.DatabaseResetError as canonical_error:
            if canonical_error.code == "reset_path_unsafe":
                raise
        else:
            raise error
        _unlink_verified(partial, legacy._path_identity(partial))
        _flush_namespace(store.authority, partial.parent)
        return None
    _promote_existing_json_partial(
        partial, audit, _raw, identity, store.authority
    )
    return value


def _restore_v2(
    store: GenerationStore,
    journal: dict[str, Any],
    crash_checkpoint: Callable[[str], None] | None,
) -> dict[str, Any]:
    root = store.root
    database = root / journal["database_path"]
    active, quarantines, failed, restoring = _operational_paths(store, journal)
    if not _active_source_matches(database, journal):
        for key, path in active.items():
            if not os.path.lexists(path):
                continue
            source = journal["source_files"][key]
            identity = legacy._path_identity(path)
            if source is not None and identity == source["identity"]:
                continue
            if os.path.lexists(failed[key]):
                raise _error("reset_recovery_invalid")
            store.append(journal, journal["stage"], crash_checkpoint, "recovery:restore_quarantine")
            _move_verified(path, failed[key], identity)
            _flush_namespace(store.authority, path.parent)
        complete_originals = all(
            item is None
            or (os.path.lexists(active[key]) and legacy._path_identity(active[key]) == item["identity"])
            or (quarantines[key] is not None and os.path.lexists(quarantines[key]) and legacy._path_identity(quarantines[key]) == item["identity"])
            for key, item in journal["quarantine"].items()
        )
        if complete_originals:
            for key, item in journal["quarantine"].items():
                if item is None or os.path.lexists(active[key]):
                    continue
                store.append(journal, journal["stage"], crash_checkpoint, "recovery:restore_original")
                _move_verified(quarantines[key], active[key], item["identity"])
                _flush_namespace(store.authority, active[key].parent)
        else:
            backup = root / journal["backup"]["path"]
            target = restoring["database"]
            copy_target = (
                root / store.reviewed_plan_manifest["artifact_paths"]["backup_copy_partial_path"]
                if store.origin == "native_v2" else target
            )
            for candidate in dict.fromkeys((copy_target, target)):
                if (
                    os.path.lexists(candidate)
                    and legacy._stable_sha256(candidate)
                    != journal["backup"]["sha256"]
                ):
                    interrupted_identity = _validate_file_prefix(
                        candidate, backup
                    )
                    store.append(
                        journal, journal["stage"], crash_checkpoint,
                        "recovery:remove_bad_copy",
                    )
                    _unlink_verified(candidate, interrupted_identity)
                    _flush_namespace(store.authority, candidate.parent)
            if not os.path.lexists(copy_target) and not os.path.lexists(target):
                store.append(
                    journal, journal["stage"], crash_checkpoint,
                    "recovery:backup_copy",
                )
                _copy_verified(
                    backup, copy_target, journal["backup"]["sha256"],
                    crash_checkpoint,
                    expected_source_identity=journal["backup"]["identity"],
                )
                _flush_namespace(store.authority, copy_target.parent)
            if copy_target != target and os.path.lexists(copy_target):
                if os.path.lexists(target):
                    raise _error("reset_recovery_invalid")
                store.append(
                    journal, journal["stage"], crash_checkpoint,
                    "recovery:prepare_restoring",
                )
                _move_verified(
                    copy_target, target, legacy._path_identity(copy_target)
                )
                _flush_namespace(store.authority, target.parent)
            if not os.path.lexists(database):
                store.append(journal, journal["stage"], crash_checkpoint, "recovery:install_backup")
                _move_verified(target, database, legacy._path_identity(target))
                _flush_namespace(store.authority, database.parent)
    store.append(journal, journal["stage"], crash_checkpoint, "recovery:validate_source")
    restored = legacy._open_reset_source(database)
    try:
        label, digest = legacy._validate_source(restored)
    finally:
        restored.rollback()
        restored.close()
    if label != journal["source_schema_label"] or digest != journal["logical_digest"]:
        raise _error("reset_recovery_invalid")
    store.append(journal, journal["stage"], crash_checkpoint, "recovery:audit")
    _install_terminal_audit(store, journal, "restored_source", label, crash_checkpoint)
    _cleanup_after_audit(store, journal)
    return _report(journal, "restored_source", native=store.origin == "native_v2")


def _complete_fresh_v2(
    store: GenerationStore,
    journal: dict[str, Any],
    crash_checkpoint: Callable[[str], None] | None,
) -> dict[str, Any]:
    if journal["stage"] not in {"fresh_validated", "cleaning_quarantine", "finalizing"}:
        raise _error("reset_recovery_invalid")
    database = store.root / journal["database_path"]
    store.append(journal, journal["stage"], crash_checkpoint, "recovery:fresh_validate")
    connection = legacy._open_direct(database, read_only=True)
    try:
        validate_v14_foundation(connection)
        validate_database_integrity(connection)
    finally:
        connection.rollback()
        connection.close()
    _active, quarantines, _failed, _restoring = _operational_paths(store, journal)
    for key, path in quarantines.items():
        if path is not None and os.path.lexists(path):
            store.append(journal, journal["stage"], crash_checkpoint, "recovery:fresh_cleanup")
            expected = journal["quarantine"][key]["identity"]
            _unlink_verified(path, expected)
            _flush_namespace(store.authority, path.parent)
    store.append(journal, journal["stage"], crash_checkpoint, "recovery:fresh_audit")
    _install_terminal_audit(store, journal, "reset", "1.4", crash_checkpoint)
    _cleanup_after_audit(store, journal)
    return _report(journal, "reset", native=store.origin == "native_v2")


def _open_reviewed_plan_handle(path: Path) -> int:
    """Open an external reviewed plan through the SOW's exact handle walk."""

    drive_root = Path(path.anchor)
    parts = path.relative_to(drive_root).parts
    if not parts:
        raise _error("reset_recovery_invalid")
    root_handle = legacy._windows_open_handle(
        drive_root,
        0x80000000,  # GENERIC_READ
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        share_access=0x1 | 0x2 | 0x4,
    )
    parent_handle = root_handle
    leaf_handle = 0
    try:
        legacy._windows_validate_handle(parent_handle, directory=True)
        volume_serial = legacy._windows_handle_snapshot(parent_handle)["file_id"].split(":")[1]
        for component in parts[:-1]:
            child = legacy._windows_create_relative(
                parent_handle,
                component,
                directory=True,
                create_new=False,
                desired_access=0x00100000 | 0x00000001 | 0x00000080,
                share_access=0x1 | 0x2 | 0x4,
            )
            try:
                legacy._windows_validate_handle(child, directory=True)
                if legacy._windows_handle_snapshot(child)["file_id"].split(":")[1] != volume_serial:
                    raise _error("reset_recovery_invalid")
            except Exception:
                legacy._windows_close_handle(child)
                raise
            if parent_handle != root_handle:
                legacy._windows_close_handle(parent_handle)
            parent_handle = child
        leaf_handle = legacy._windows_create_relative(
            parent_handle,
            parts[-1],
            directory=False,
            create_new=False,
            desired_access=0x00100000 | 0x00000001 | 0x00000080,
            share_access=0x1 | 0x4,
        )
        legacy._windows_validate_handle(leaf_handle, directory=False)
        if legacy._windows_handle_snapshot(leaf_handle)["file_id"].split(":")[1] != volume_serial:
            raise _error("reset_recovery_invalid")
        return leaf_handle
    except Exception:
        if leaf_handle:
            legacy._windows_close_handle(leaf_handle)
        raise
    finally:
        if parent_handle != root_handle:
            legacy._windows_close_handle(parent_handle)
        legacy._windows_close_handle(root_handle)


def _read_reviewed_plan(
    path_value: str, expected_token: str, current_commit: str
) -> dict[str, Any]:
    if not isinstance(path_value, str):
        raise _error("reset_recovery_invalid")
    try:
        utf16_length = len(path_value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise _error("reset_recovery_invalid") from error
    if (
        utf16_length < 3
        or utf16_length > 32_767
        or any(ord(character) < 32 for character in path_value)
        or "/" in path_value
        or re.fullmatch(r"[A-Za-z]:\\[^:]+", path_value) is None
    ):
        raise _error("reset_recovery_invalid")
    components = path_value[3:].split("\\")
    if any(
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        for component in components
    ):
        raise _error("reset_recovery_invalid")
    path = Path(path_value)
    drive_root = path_value[:3]
    if int(ctypes.windll.kernel32.GetDriveTypeW(drive_root)) != 3:
        raise _error("reset_recovery_invalid")
    _serial, filesystem = _volume_information(drive_root)
    if filesystem not in {"NTFS", "ReFS"}:
        raise _error("reset_recovery_invalid")
    try:
        handle = _open_reviewed_plan_handle(path)
    except legacy.DatabaseResetError as error:
        raise _error("reset_recovery_invalid") from error
    chunks: list[bytes] = []
    try:
        before = legacy._windows_handle_snapshot(handle)
        if (
            before["directory"]
            or before["delete_pending"]
            or before["link_count"] != 1
            or before["file_attributes"] & 0x400
            or before["end_of_file"] < 1
            or before["end_of_file"] > 1_048_576
        ):
            raise _error("reset_recovery_invalid")
        import msvcrt

        descriptor = msvcrt.open_osfhandle(
            handle, getattr(os, "O_BINARY", 0) | os.O_RDONLY
        )
        handle = 0
        try:
            remaining = before["end_of_file"]
            while remaining:
                block = os.read(descriptor, min(65_536, remaining))
                if not block:
                    raise _error("reset_recovery_invalid")
                chunks.append(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise _error("reset_recovery_invalid")
            after = legacy._windows_handle_snapshot(
                msvcrt.get_osfhandle(descriptor)
            )
            stable_fields = {
                "allocation_size", "delete_pending", "directory",
                "end_of_file", "file_attributes", "file_id",
                "last_write_time", "link_count", "reparse_tag",
            }
            if any(after[key] != before[key] for key in stable_fields):
                raise _error("reset_recovery_invalid")
        finally:
            os.close(descriptor)
    finally:
        if handle:
            legacy._windows_close_handle(handle)
    value = _load_canonical(b"".join(chunks))
    plan = _validate_plan(value, expected_token=expected_token)
    if plan["implementation_commit"] != current_commit:
        raise _error("reset_recovery_invalid")
    return plan


def _matches_incomplete_native_observation(
    key: str,
    current: dict[str, Any],
    planned: dict[str, Any],
) -> bool:
    if key != "database_shm" or not planned["exists"]:
        return current == planned
    return (
        current["exists"] is True
        and current["file_type"] == "regular"
        and current["identity"] == planned["identity"]
        and current["link_count"] == 1
    )


def _handle_native_partial_only(
    root: Path,
    partial: Path,
    expected_token: str,
    reviewed_plan_manifest: str | None,
    current_commit: str,
    preliminary: dict[str, Any],
) -> dict[str, Any] | Generation:
    match = GENERATION_RE.fullmatch(
        partial.name[:-len(GENERATION_PARTIAL_SUFFIX)]
    )
    if (
        match is None
        or match.group(1) != expected_token
        or match.group(2) != "00000000000000000001"
    ):
        raise _error("reset_recovery_invalid")
    try:
        complete = _read_generation(partial, partial=True)
    except legacy.DatabaseResetError as error:
        if legacy._stable_sha256(partial) == match.group(3):
            raise error
        if reviewed_plan_manifest is None:
            raise _error("reset_recovery_invalid")
        plan = _read_reviewed_plan(reviewed_plan_manifest, expected_token, current_commit)
        manifest = plan["reviewed_plan_manifest"]
        authority = _revalidate_persisted_authority(root, manifest["durability"])
        database = root / "data/helios.db"
        for key, path in (
            ("database", database),
            ("database_wal", Path(f"{database}-wal")),
            ("database_shm", Path(f"{database}-shm")),
        ):
            current_observation = _file_observation(path)
            planned_observation = manifest["filesystem"][key]
            if not _matches_incomplete_native_observation(
                key, current_observation, planned_observation
            ):
                raise _error("reset_recovery_invalid")
        backup = root / plan["backup_path"]
        if not os.path.lexists(backup):
            raise _error("reset_recovery_invalid")
        source = legacy._open_reset_source(database)
        backup_connection = legacy._open_connection(backup, immutable=True)
        try:
            if legacy._validate_source(source) != legacy._validate_source(backup_connection):
                raise _error("reset_recovery_invalid")
        finally:
            source.rollback(); source.close()
            backup_connection.rollback(); backup_connection.close()
        audit = root / plan["audit_path"]
        disallowed = [
            root / relative
            for name, relative in manifest["artifact_paths"].items()
            if name not in {
                "backup_path", "database_path", "database_wal_path",
                "database_shm_path", "generation_partial_probe_path",
                "generation_probe_path", "maintenance_lock_path",
            }
        ]
        if os.path.lexists(audit) or any(os.path.lexists(path) for path in disallowed):
            raise _error("reset_recovery_invalid")
        expected_reserved = [partial.name]
        snapshot = _stable_partial_snapshot(partial)
        if (
            _enumerate_reserved(root / "data") != expected_reserved
            or _current_commit(root, [f"data/{partial.name}"]) != current_commit
        ):
            raise _error("reset_recovery_invalid")
        authority = _revalidate_persisted_authority(root, manifest["durability"])
        if _stable_partial_snapshot(partial) != snapshot:
            raise _error("reset_recovery_invalid")
        _unlink_verified_bytes(partial, snapshot[1], snapshot[0])
        _flush_namespace(authority, partial.parent)
        if _enumerate_reserved(root / "data"):
            raise _error("reset_failed")
        return {
            "database_path": "data/helios.db",
            "plan_token": expected_token,
            "reset_protocol_version": RESET_PROTOCOL_VERSION,
            "status": "prejournal_aborted",
        }
    if complete.sequence != 1 or complete.token != expected_token or complete.envelope["origin"] != "native_v2":
        raise _error("reset_recovery_invalid")
    final = Path(str(partial)[:-len(GENERATION_PARTIAL_SUFFIX)])
    authority = complete.envelope["reviewed_plan_manifest"]["durability"]
    _revalidate_persisted_authority(root, authority)
    _promote_existing_json_partial(
        partial, final, complete.raw, complete.identity, authority
    )
    return _read_generation(final, partial=False)


def _handle_chain_partial(store: GenerationStore, partials: list[Path]) -> None:
    if not partials:
        return
    if len(partials) != 1:
        raise _error("reset_recovery_invalid")
    partial = partials[0]
    base = partial.name[:-len(GENERATION_PARTIAL_SUFFIX)]
    match = GENERATION_RE.fullmatch(base)
    if match is None or match.group(1) != store.token:
        raise _error("reset_recovery_invalid")
    sequence = int(match.group(2))
    if 1 <= sequence <= len(store.generations):
        completed = next(
            (
                item for item in store.generations
                if item.sequence == sequence
            ),
            None,
        )
        if completed is None:
            raise _error("reset_recovery_invalid")
        partial_generation = _read_generation(partial, partial=True)
        if (
            partial_generation.content_hash != completed.content_hash
            or partial_generation.raw != completed.raw
        ):
            raise _error("reset_recovery_invalid")
        _unlink_verified_bytes(
            partial, partial_generation.identity, partial_generation.raw
        )
        _flush_namespace(store.authority, partial.parent)
        return
    if sequence != len(store.generations) + 1:
        raise _error("reset_recovery_invalid")
    try:
        generation = _read_generation(partial, partial=True)
    except legacy.DatabaseResetError as error:
        filename_digest = match.group(3)
        if legacy._stable_sha256(partial) == filename_digest:
            raise error
        _unlink_verified(partial, legacy._path_identity(partial))
        _flush_namespace(store.authority, partial.parent)
        return
    if generation.envelope["previous_generation_sha256"] != store.generations[-1].content_hash:
        raise _error("reset_recovery_invalid")
    final = Path(str(partial)[:-len(GENERATION_PARTIAL_SUFFIX)])
    _promote_existing_json_partial(
        partial, final, generation.raw, generation.identity, store.authority
    )
    store.generations.append(_read_generation(final, partial=False))


def recover_database_reset(
    database_path: Path | str,
    *,
    expected_plan_token: str | None,
    action: str | None,
    confirm_reset_recovery: bool,
    reviewed_plan_manifest: str | None = None,
    repository_root: Path | str = PROJECT_ROOT,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not confirm_reset_recovery or not expected_plan_token or action not in {"restore-source", "complete-fresh"}:
        raise _error("reset_confirmation_required")
    if not _valid_sha(expected_plan_token):
        raise _error("reset_recovery_invalid")
    _require_windows()
    root = Path(repository_root).resolve(strict=True)
    representative = _prospective_paths(_artifact_paths("20000101T000000000Z"))
    _git_evidence(root, representative)
    preliminary = _probe_durability(root)
    database = root / "data/helios.db"
    try:
        lease = acquire_database_lease(database, shared=False, allow_recovery_state=True)
    except MaintenanceLockError as error:
        raise _error("database_maintenance_in_progress") from error
    try:
        current_commit = _current_commit(root, representative)
        completed, partials, evidence_paths, legacy_paths = _discover_generations(root, expected_plan_token)
        chain, chain_hashes = _select_chain(
            root, completed, expected_plan_token, current_commit
        ) if completed else ([], [])
        if chain:
            store = _store_from_chain(root, chain, chain_hashes)
            if store.origin == "legacy_v1_conversion" and reviewed_plan_manifest is not None:
                raise _error("reset_recovery_invalid")
            if store.origin == "native_v2" and reviewed_plan_manifest is not None:
                supplied = _read_reviewed_plan(reviewed_plan_manifest, expected_plan_token, current_commit)
                if supplied["reviewed_plan_manifest"] != store.reviewed_plan_manifest:
                    raise _error("reset_recovery_invalid")
            if store.origin == "legacy_v1_conversion":
                journal = store.journal
                audit = root / journal["audit_path"]
                retained_manifest = Path(f"{audit}.generation-chain.json")
                _validate_legacy_controls_subset(
                    root,
                    store.legacy_source,
                    terminal_cleanup=(
                        os.path.lexists(audit)
                        and os.path.lexists(retained_manifest)
                    ),
                )
            store.authority = _revalidate_persisted_authority(root, store.authority)
            _cleanup_matching_evidence_records(store, evidence_paths)
            journal = store.journal
            expected_outcome = "restored_source" if action == "restore-source" else "reset"
            promoted_audit = _promote_terminal_audit_partial(
                store, journal, expected_outcome
            )
            terminal_audit = _existing_terminal_audit(store, journal)
            if terminal_audit is not None:
                if terminal_audit["outcome"] != expected_outcome:
                    raise _error("reset_recovery_invalid")
                _cleanup_after_audit(store, journal)
                return _report(
                    journal, expected_outcome,
                    native=store.origin == "native_v2",
                )
            _handle_chain_partial(store, partials)
            journal = store.journal
            backup = root / journal["backup"]["path"]
            if (
                _verified_file_sha256(
                    backup, journal["backup"]["identity"]
                )
                != journal["backup"]["sha256"]
            ):
                raise _error("reset_recovery_invalid")
            if action == "restore-source":
                return _restore_v2(store, journal, crash_checkpoint)
            return _complete_fresh_v2(store, journal, crash_checkpoint)
        if legacy_paths or evidence_paths:
            if reviewed_plan_manifest is not None:
                raise _error("reset_recovery_invalid")
            selection = _select_legacy(root, expected_plan_token)
            record, authority = _handle_evidence_state(
                root, expected_plan_token, current_commit, selection,
                evidence_paths, partials, preliminary, crash_checkpoint,
            )
            store = GenerationStore(
                root, expected_plan_token, authority,
                origin="legacy_v1_conversion", reviewed_plan_manifest=None,
                legacy_recovery_evidence=record.value["legacy_recovery_evidence"],
                legacy_source=record.value["legacy_source"],
                converter_implementation_commit=current_commit,
            )
            _revalidate_legacy_context(
                root, expected_plan_token, current_commit, selection,
                _expected_legacy_names(selection.source)
                + [record.path.name]
                + [path.name for path in partials],
            )
            if partials:
                if len(partials) != 1:
                    raise _error("reset_recovery_invalid")
                _promote_or_rebuild_legacy_generation_partial(
                    store, selection.journal, partials[0], record, crash_checkpoint
                )
            else:
                store.append(selection.journal, selection.journal["stage"], crash_checkpoint, "legacy_conversion")
            generation = store.generations[0]
            if (
                generation.envelope["legacy_recovery_evidence"] != record.value["legacy_recovery_evidence"]
                or generation.envelope["legacy_source"] != record.value["legacy_source"]
            ):
                raise _error("reset_recovery_invalid")
            if os.path.lexists(record.path):
                _revalidate_legacy_context(
                    root, expected_plan_token, current_commit, selection,
                    _expected_legacy_names(selection.source)
                    + [record.path.name, generation.path.name],
                )
                reread_record = _validate_evidence_record(
                    record.path, partial=False,
                    expected_token=expected_plan_token,
                    converter=current_commit,
                )
                if reread_record.raw != record.raw:
                    raise _error("reset_recovery_invalid")
                _unlink_verified_bytes(record.path, record.identity, record.raw)
                _flush_namespace(authority, record.path.parent)
            journal = store.journal
            if action == "restore-source":
                return _restore_v2(store, journal, crash_checkpoint)
            return _complete_fresh_v2(store, journal, crash_checkpoint)
        if len(partials) == 1:
            handled = _handle_native_partial_only(
                root, partials[0], expected_plan_token,
                reviewed_plan_manifest, current_commit, preliminary,
            )
            if isinstance(handled, dict):
                return handled
            store = _store_from_chain(root, [handled])
            store.authority = _revalidate_persisted_authority(root, store.authority)
            if action == "restore-source":
                return _restore_v2(store, store.journal, crash_checkpoint)
            return _complete_fresh_v2(store, store.journal, crash_checkpoint)
        raise _error("reset_recovery_invalid")
    except legacy.DatabaseResetError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error, SchemaValidationError) as error:
        raise _error("reset_recovery_invalid") from error
    finally:
        lease.close()


# Imported last to avoid a circular-import failure when this implementation
# module is inspected directly.  All references occur at call time.
from . import reset_database as legacy  # noqa: E402


class _DurabilityBoundaryError(legacy.DatabaseResetError):
    """Publicly reset_failed, internally marks a post-artifact boundary."""
