"""Guarded, separately authorized fresh-database reset protocol.

The public functions are deliberately inert unless their exact plan/execution
or recovery arguments are supplied.  Automated callers must use temporary
repository fixtures; implementation approval alone never authorizes calling
these functions for the project ``data/helios.db``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .database import DEFAULT_SCHEMA_PATH, _seed_initial_room
from .maintenance_lock import (
    MaintenanceLockError,
    ResetRecoveryRequiredError,
    acquire_database_lease,
    coordination_paths,
)
from .read_snapshot import _has_wal_header, _open_connection
from .schema_validation import (
    SchemaValidationError,
    V12_HISTORY,
    V13_HISTORY,
    schema_history,
    validate_database_integrity,
    validate_v12_source,
    validate_v13_foundation,
    validate_v14_foundation,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_RESET_PROTOCOL_VERSION = "room_shared_reset_v1"
RESET_PROTOCOL_VERSION = LEGACY_RESET_PROTOCOL_VERSION
RECOVERY_COMMIT_BY_SCHEMA = {
    "1.2": "d9d9717b4e882c19ef41e44a6f0c63fc062de41a",
    "1.3": "d9d9717b4e882c19ef41e44a6f0c63fc062de41a",
}
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
STAGES = (
    "ready_to_quarantine", "quarantining", "original_quarantined",
    "installing_fresh", "fresh_installed", "validating_fresh",
    "fresh_validated", "cleaning_quarantine", "finalizing",
)
JOURNAL_FIELDS = {
    "audit_path", "backup", "database_path", "implementation_commit",
    "journal_sequence", "journal_version", "logical_digest",
    "plan_manifest_sha256", "plan_token", "quarantine", "recovery_commit",
    "reset_protocol_version", "source_files", "source_observations",
    "source_schema_label", "stage", "updated_at",
}
OBSERVATION_FIELDS = {
    "exists", "file_type", "identity", "link_count", "mtime_ns",
    "sha256", "sha256_status", "size",
}
AUDIT_FIELDS = {
    "audit_version", "backup_identity", "backup_path", "backup_sha256",
    "completed_at", "database_path", "implementation_commit", "logical_digest",
    "outcome", "plan_manifest_sha256", "plan_token", "quarantine_identities",
    "recovery_commit", "reset_protocol_version", "source_observations",
    "source_schema_label", "terminal_schema_label",
}


class DatabaseResetError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _error(code: str) -> DatabaseResetError:
    values = {
        "reset_confirmation_required": ("Reset execution requires the exact reviewed plan and explicit confirmation.", 2),
        "database_maintenance_in_progress": ("The Helios Room database is unavailable during maintenance.", 1),
        "reset_path_unsafe": ("The database reset target could not be verified safely.", 1),
        "reset_plan_stale": ("The reviewed database reset plan no longer matches the live files.", 1),
        "reset_git_safety_invalid": ("The database reset Git safety contract is not satisfied.", 1),
        "reset_source_invalid": ("The database is not an eligible reset source.", 1),
        "database_reset_recovery_required": ("The Helios Room database requires reset recovery before it can be opened.", 1),
        "reset_recovery_invalid": ("The database reset recovery state could not be verified safely.", 1),
        "reset_failed": ("The database reset did not complete safely.", 1),
        "reset_platform_unsupported": ("Database reset planning, execution, and recovery are supported only on Windows.", 1),
        "reset_durability_unsupported": ("The Windows filesystem cannot provide the required database reset durability guarantees.", 1),
    }
    message, exit_code = values[code]
    return DatabaseResetError(code, message, exit_code=exit_code)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_sha256(path: Path) -> str:
    before = _safe_regular(path)
    before_identity = _path_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if _descriptor_identity(descriptor, opened) != before_identity or opened.st_nlink != 1:
            raise _error("reset_path_unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    after = _safe_regular(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise _error("reset_path_unsafe")
    if _path_identity(path) != before_identity:
        raise _error("reset_path_unsafe")
    return digest.hexdigest()


def _identity(info: os.stat_result) -> str:
    return f"posix:{info.st_dev}:{info.st_ino}"


def _windows_typed_handle(handle: Any):
    """Return one full-width Win32 HANDLE without implicit integer coercion."""

    import ctypes
    from ctypes import wintypes

    if isinstance(handle, wintypes.HANDLE):
        return handle
    return wintypes.HANDLE(handle)


def _windows_handle_identity(handle: Any) -> str:
    import ctypes
    from ctypes import wintypes

    class FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", ctypes.c_ubyte * 16),
        ]

    info = FILE_ID_INFO()
    function = ctypes.windll.kernel32.GetFileInformationByHandleEx
    function.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    if not function(
        _windows_typed_handle(handle), 18,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        raise _error("reset_path_unsafe")
    return f"windows:{info.VolumeSerialNumber:016x}:{bytes(info.FileId).hex()}"


def _descriptor_identity(descriptor: int, info: os.stat_result) -> str:
    if os.name != "nt":
        return _identity(info)
    import msvcrt

    return _windows_handle_identity(msvcrt.get_osfhandle(descriptor))


def _path_identity(path: Path) -> str:
    if os.name != "nt":
        return _identity(path.lstat())
    handle = _windows_open_path_no_follow(path)
    try:
        return _windows_handle_identity(handle)
    finally:
        _windows_close_handle(handle)


def _safe_regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exception:
        raise _error("reset_path_unsafe") from exception
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or path.is_symlink()
        or attributes & reparse
    ):
        raise _error("reset_path_unsafe")
    if os.name == "nt":
        handle = _windows_open_path_no_follow(path, expect_directory=False)
        _windows_close_handle(handle)
    return info


def _safe_directory(path: Path, *, required: bool = True) -> os.stat_result | None:
    if not os.path.lexists(path) and not required:
        return None
    try:
        info = path.lstat()
    except OSError as exception:
        raise _error("reset_path_unsafe") from exception
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or attributes & reparse:
        raise _error("reset_path_unsafe")
    if os.name == "nt":
        handle = _windows_open_directory_chain(path)
        _windows_close_handle(handle)
    return info


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as exception:
        raise _error("reset_git_safety_invalid") from exception
    if result.returncode:
        raise _error("reset_git_safety_invalid")
    return result.stdout


def _prospective_paths(timestamp: str, token_hint: str = "0" * 64) -> list[str]:
    stem = f"backups/helios-pre-room-shared-reset-{timestamp}"
    paths = [
        "data/helios.db", "data/helios.db-wal", "data/helios.db-shm",
        "data/.helios-room-database.lock", "data/.helios-room-reset-state.json",
        "data/.helios-room-reset-state.json.next", f"{stem}.db", f"{stem}.db.partial",
        f"{stem}.db.partial-wal", f"{stem}.db.partial-shm", f"{stem}.db.partial-journal",
        f"{stem}.audit.json", f"{stem}.audit.json.partial",
    ]
    for name in ("helios.db", "helios.db-wal", "helios.db-shm"):
        paths.extend((
            f"data/.{name}.reset-{token_hint}.original",
            f"data/.{name}.reset-{token_hint}.failed-new",
            f"data/.{name}.reset-{token_hint}.restoring",
        ))
    return paths


def _git_safety(root: Path, prospective: list[str]) -> tuple[str, str]:
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if not (len(head) in {40, 64} and all(c in "0123456789abcdef" for c in head)):
        raise _error("reset_git_safety_invalid")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise _error("reset_git_safety_invalid")
    committed_ignore = _git(root, "show", f"{head}:.gitignore").decode("utf-8")
    ignore_blob = _git(root, "rev-parse", f"{head}:.gitignore").decode("ascii").strip()
    if not _valid_commit(ignore_blob):
        raise _error("reset_git_safety_invalid")
    if IGNORE_BLOCK not in committed_ignore:
        raise _error("reset_git_safety_invalid")
    for relative in prospective:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-q", relative],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise _error("reset_git_safety_invalid")
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if tracked.returncode == 0:
            raise _error("reset_git_safety_invalid")
    return head, ignore_blob


def _utc_stamp() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    return stamp, iso


def _relative_database(root: Path, database_path: Path | str) -> Path:
    root = root.resolve(strict=True)
    expected = root / "data" / "helios.db"
    supplied = Path(database_path)
    if not supplied.is_absolute():
        supplied = root / supplied
    try:
        if supplied.resolve(strict=True) != expected.resolve(strict=True):
            raise _error("reset_path_unsafe")
    except OSError as exception:
        raise _error("reset_path_unsafe") from exception
    _safe_directory(root)
    _safe_directory(expected.parent)
    _safe_regular(expected)
    return expected


def _component(path: Path, relative: str) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"exists": False, "file_type": "absent", "identity": None, "link_count": None,
                "mtime_ns": None, "sha256": None, "sha256_status": "not-present", "size": None,
                "path": relative}
    info = _safe_regular(path)
    return {"exists": True, "file_type": "regular", "identity": _path_identity(path),
            "link_count": info.st_nlink, "mtime_ns": info.st_mtime_ns,
            "sha256": _stable_sha256(path), "sha256_status": "stable", "size": info.st_size,
            "path": relative}


def _observation(path: Path) -> dict[str, Any]:
    component = _component(path, "")
    return {key: component[key] for key in (
        "exists", "file_type", "identity", "link_count", "mtime_ns",
        "sha256", "sha256_status", "size",
    )}


def _observations(database: Path) -> dict[str, Any]:
    return {
        "database": _observation(database),
        "shm": _observation(Path(f"{database}-shm")),
        "wal": _observation(Path(f"{database}-wal")),
    }


def _source_file(component: dict[str, Any]) -> dict[str, Any]:
    return {key: component[key] for key in (
        "identity", "link_count", "mtime_ns", "path", "sha256", "size",
    )}


def _make_plan(
    root: Path, database_path: Path | str, *, stamp: str, backup_path: str, audit_path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    prospective = _prospective_paths(stamp)
    head, ignore_blob = _git_safety(root, prospective)
    database = _relative_database(root, database_path)
    data = database.parent
    backups = root / "backups"
    data_info = _safe_directory(data)
    backups_info = _safe_directory(backups, required=False)
    components = {
        "database": _component(database, "data/helios.db"),
        "wal": _component(Path(f"{database}-wal"), "data/helios.db-wal"),
        "shm": _component(Path(f"{database}-shm"), "data/helios.db-shm"),
    }
    if components["wal"]["exists"] != components["shm"]["exists"]:
        raise _error("reset_path_unsafe")
    manifest = {
        "audit_path": audit_path,
        "backup_path": backup_path,
        "components": components,
        "database_path": "data/helios.db",
        "directories": {
            "backups": None if backups_info is None else _path_identity(backups),
            "data": _path_identity(data),
            "repository": _path_identity(root),
        },
        "gitignore_blob_identity": ignore_blob,
        "implementation_commit": head,
        "prospective_ignore_results": {
            relative: {"ignored": True, "tracked": False}
            for relative in sorted(prospective)
        },
        "recovery_commit_by_schema": dict(RECOVERY_COMMIT_BY_SCHEMA),
        "reset_protocol_version": RESET_PROTOCOL_VERSION,
        "worktree_status": "",
    }
    token = _sha256_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    plan = {
        "audit_path": audit_path,
        "backup_path": backup_path,
        "database_identity": components["database"]["identity"],
        "database_path": "data/helios.db",
        "database_sha256": components["database"]["sha256"],
        "implementation_commit": head,
        "plan_token": token,
        "recovery_commit_by_schema": dict(RECOVERY_COMMIT_BY_SCHEMA),
        "reset_protocol_version": RESET_PROTOCOL_VERSION,
        "status": "planned",
    }
    return plan, manifest


_BACKUP_PATH_PATTERN = re.compile(
    r"backups/helios-pre-room-shared-reset-(\d{8}T\d{9}Z)\.db\Z"
)


def _reviewed_output_paths(backup_path: str, audit_path: str) -> str:
    if not isinstance(backup_path, str) or not isinstance(audit_path, str):
        raise _error("reset_plan_stale")
    match = _BACKUP_PATH_PATTERN.fullmatch(backup_path)
    if match is None:
        raise _error("reset_plan_stale")
    stamp = match.group(1)
    if audit_path != f"backups/helios-pre-room-shared-reset-{stamp}.audit.json":
        raise _error("reset_plan_stale")
    return stamp


def _reset_output_paths(root: Path, backup_path: str, audit_path: str) -> tuple[Path, ...]:
    backup = root / backup_path
    audit = root / audit_path
    return (
        backup, Path(f"{backup}.partial"), Path(f"{backup}.partial-wal"),
        Path(f"{backup}.partial-shm"), Path(f"{backup}.partial-journal"),
        audit, Path(f"{audit}.partial"),
    )


def _valid_commit(value: Any) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and re.fullmatch(
        r"[0-9a-f]+", value
    ) is not None


def _valid_utc_millisecond(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") == value


def _validate_observation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != OBSERVATION_FIELDS:
        raise _error("reset_recovery_invalid")
    exists = value["exists"]
    if type(exists) is not bool:
        raise _error("reset_recovery_invalid")
    if exists:
        if (
            value["file_type"] != "regular"
            or not isinstance(value["identity"], str)
            or type(value["link_count"]) is not int
            or value["link_count"] != 1
            or type(value["mtime_ns"]) is not int
            or value["mtime_ns"] < 0
            or type(value["size"]) is not int
            or value["size"] < 0
            or value["sha256_status"] not in {"stable", "unstable"}
            or (
                value["sha256_status"] == "stable"
                and (
                    not isinstance(value.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
                )
            )
            or (value["sha256_status"] == "unstable" and value["sha256"] is not None)
        ):
            raise _error("reset_recovery_invalid")
    elif value != {
        "exists": False, "file_type": "absent", "identity": None,
        "link_count": None, "mtime_ns": None, "sha256": None,
        "sha256_status": "not-present", "size": None,
    }:
        raise _error("reset_recovery_invalid")


def _validate_journal(
    value: Any, *, expected_token: str, implementation_commit: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS:
        raise _error("reset_recovery_invalid")
    if (
        value["journal_version"] != 1
        or type(value["journal_sequence"]) is not int
        or value["journal_sequence"] <= 0
        or value["stage"] not in STAGES
        or value["reset_protocol_version"] != LEGACY_RESET_PROTOCOL_VERSION
        or value["plan_token"] != expected_token
        or value["plan_manifest_sha256"] != expected_token
        or value["database_path"] != "data/helios.db"
        or value["implementation_commit"] != implementation_commit
        or not _valid_commit(value["implementation_commit"])
        or value["source_schema_label"] not in RECOVERY_COMMIT_BY_SCHEMA
        or value["recovery_commit"]
        != RECOVERY_COMMIT_BY_SCHEMA.get(value["source_schema_label"])
        or not isinstance(value.get("logical_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["logical_digest"]) is None
        or not _valid_utc_millisecond(value["updated_at"])
    ):
        raise _error("reset_recovery_invalid")
    backup = value["backup"]
    if not isinstance(backup, dict) or set(backup) != {"identity", "path", "sha256"}:
        raise _error("reset_recovery_invalid")
    try:
        _reviewed_output_paths(backup.get("path"), value["audit_path"])
    except DatabaseResetError as exception:
        raise _error("reset_recovery_invalid") from exception
    if (
        not isinstance(backup["identity"], str)
        or not isinstance(backup.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", backup["sha256"]) is None
    ):
        raise _error("reset_recovery_invalid")
    source_files = value["source_files"]
    quarantine = value["quarantine"]
    if (
        not isinstance(source_files, dict)
        or set(source_files) != {"database", "shm", "wal"}
        or not isinstance(quarantine, dict)
        or set(quarantine) != {"database", "shm", "wal"}
    ):
        raise _error("reset_recovery_invalid")
    for key, suffix in (("database", ""), ("wal", "-wal"), ("shm", "-shm")):
        source = source_files[key]
        quarantined = quarantine[key]
        if (source is None) != (quarantined is None) or (key == "database" and source is None):
            raise _error("reset_recovery_invalid")
        if source is None:
            continue
        if not isinstance(source, dict) or set(source) != {
            "identity", "link_count", "mtime_ns", "path", "sha256", "size",
        }:
            raise _error("reset_recovery_invalid")
        expected_path = f"data/helios.db{suffix}"
        if (
            source["path"] != expected_path
            or not isinstance(source["identity"], str)
            or source["link_count"] != 1
            or type(source["mtime_ns"]) is not int
            or source["mtime_ns"] < 0
            or type(source["size"]) is not int
            or source["size"] < 0
            or not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
            or not isinstance(quarantined, dict)
            or set(quarantined) != {"identity", "path"}
            or quarantined["identity"] != source["identity"]
            or quarantined["path"]
            != f"data/.helios.db{suffix}.reset-{expected_token}.original"
        ):
            raise _error("reset_recovery_invalid")
    if (source_files["wal"] is None) != (source_files["shm"] is None):
        raise _error("reset_recovery_invalid")
    observations = value["source_observations"]
    if not isinstance(observations, dict) or set(observations) != {"after_close", "before_open"}:
        raise _error("reset_recovery_invalid")
    for moment in observations.values():
        if not isinstance(moment, dict) or set(moment) != {"database", "shm", "wal"}:
            raise _error("reset_recovery_invalid")
        for observation in moment.values():
            _validate_observation(observation)
    return value


def _read_control_json(path: Path) -> dict[str, Any]:
    _safe_regular(path)
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, 1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or os.read(descriptor, 1):
            raise _error("reset_recovery_invalid")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise _error("reset_recovery_invalid") from exception
    if _json_bytes(value) != raw:
        raise _error("reset_recovery_invalid")
    return value


def _validate_audit(value: Any, journal: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != AUDIT_FIELDS:
        raise _error("reset_recovery_invalid")
    outcome = value.get("outcome")
    terminal = "1.4" if outcome == "reset" else journal["source_schema_label"]
    expected = {
        "audit_version": 1,
        "backup_identity": journal["backup"]["identity"],
        "backup_path": journal["backup"]["path"],
        "backup_sha256": journal["backup"]["sha256"],
        "database_path": journal["database_path"],
        "implementation_commit": journal["implementation_commit"],
        "logical_digest": journal["logical_digest"],
        "outcome": outcome,
        "plan_manifest_sha256": journal["plan_manifest_sha256"],
        "plan_token": journal["plan_token"],
        "quarantine_identities": {
            key: None if item is None else item["identity"]
            for key, item in journal["quarantine"].items()
        },
        "recovery_commit": journal["recovery_commit"],
        "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION,
        "source_observations": journal["source_observations"],
        "source_schema_label": journal["source_schema_label"],
        "terminal_schema_label": terminal,
    }
    if outcome not in {"reset", "restored_source"} or not _valid_utc_millisecond(
        value.get("completed_at")
    ):
        raise _error("reset_recovery_invalid")
    if {key: value[key] for key in expected} != expected:
        raise _error("reset_recovery_invalid")
    return value


def plan_database_reset(
    database_path: Path | str, *, repository_root: Path | str = PROJECT_ROOT
) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    stamp, _ = _utc_stamp()
    backup = f"backups/helios-pre-room-shared-reset-{stamp}.db"
    audit = f"backups/helios-pre-room-shared-reset-{stamp}.audit.json"
    if any(os.path.lexists(path) for path in _reset_output_paths(root, backup, audit)):
        raise _error("reset_path_unsafe")
    # The first check proves the prospective lock path is committed as ignored.
    _git_safety(root, _prospective_paths(stamp))
    _safe_directory(root)
    _safe_directory(root / "data")
    try:
        lease = acquire_database_lease(root / "data" / "helios.db", shared=False)
    except ResetRecoveryRequiredError as exception:
        raise _error("database_reset_recovery_required") from exception
    except MaintenanceLockError as exception:
        raise _error("database_maintenance_in_progress") from exception
    try:
        plan, _ = _make_plan(root, database_path, stamp=stamp, backup_path=backup, audit_path=audit)
        return plan
    finally:
        lease.close()


def _typed(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"blob_b64": base64.b64encode(value).decode("ascii")}
    raise TypeError("unsupported SQLite value")


def logical_digest(connection: sqlite3.Connection) -> str:
    objects = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    tables = [row[1] for row in objects if row[0] == "table" and not str(row[1]).startswith(("messages_fts_", "seed_memories_fts_", "room_memories_fts_"))]
    data: dict[str, Any] = {"schema": [list(map(_typed, row)) for row in objects], "tables": {}}
    for table in tables:
        quoted = '"' + str(table).replace('"', '""') + '"'
        columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
        data["tables"][table] = {
            "columns": [list(map(_typed, row)) for row in columns],
            "rows": [list(map(_typed, row)) for row in rows],
        }
    return _sha256_bytes(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _open_direct(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if read_only:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
    return connection


def _open_reset_source(path: Path) -> sqlite3.Connection:
    wal = Path(f"{path}-wal").exists()
    shm = Path(f"{path}-shm").exists()
    if wal != shm:
        raise _error("reset_source_invalid")
    if not wal and not _has_wal_header(path):
        raise _error("reset_source_invalid")
    return _open_connection(path, immutable=not wal)


def _validate_source(connection: sqlite3.Connection) -> tuple[str, str]:
    history = schema_history(connection)
    if history == V12_HISTORY:
        validate_v12_source(connection)
        label = "1.2"
    elif history == V13_HISTORY:
        validate_v13_foundation(connection)
        label = "1.3"
    else:
        raise _error("reset_source_invalid")
    validate_database_integrity(connection)
    return label, logical_digest(connection)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short file write")
        view = view[written:]


def _write_durable(
    path: Path,
    value: Any,
    *,
    replace: bool = False,
    crash_checkpoint: Callable[[str], None] | None = None,
    partial_checkpoint: str | None = None,
) -> None:
    if replace:
        raise _error("reset_path_unsafe")
    descriptor = _open_new_regular(path, read_write=False)
    try:
        encoded = _json_bytes(value)
        if crash_checkpoint is not None and partial_checkpoint is not None:
            boundary = max(1, len(encoded) // 2)
            _write_all(descriptor, encoded[:boundary])
            crash_checkpoint(partial_checkpoint)
            _write_all(descriptor, encoded[boundary:])
        else:
            _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reserve_regular(path: Path) -> str:
    descriptor = _open_new_regular(path, read_write=True)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _error("reset_path_unsafe")
        return _descriptor_identity(descriptor, info)
    finally:
        os.close(descriptor)


def _windows_open_handle(
    path: Path, access: int, flags: int, *, share_access: int = 0x1 | 0x2 | 0x4
) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), access, share_access, None, 3, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise _error("reset_path_unsafe")
    return int(handle)


def _windows_close_handle(handle: Any) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(_windows_typed_handle(handle)):
        get_last_error = ctypes.windll.kernel32.GetLastError
        get_last_error.argtypes = []
        get_last_error.restype = wintypes.DWORD
        error_code = int(get_last_error())
        raise _error("reset_path_unsafe") from OSError(
            error_code, "CloseHandle"
        )


def _windows_flush_file_handle(handle: Any) -> None:
    import ctypes
    from ctypes import wintypes

    flush = ctypes.windll.kernel32.FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    if not flush(_windows_typed_handle(handle)):
        raise _error("reset_failed")


def _windows_close_after_mutation(handle: int) -> None:
    try:
        _windows_close_handle(handle)
    except DatabaseResetError as error:
        raise _error("reset_failed") from error


def _windows_handle_snapshot(handle: int) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

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

    query = ctypes.windll.kernel32.GetFileInformationByHandleEx
    query.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    query.restype = wintypes.BOOL
    attributes = FILE_ATTRIBUTE_TAG_INFO()
    standard = FILE_STANDARD_INFO()
    basic = FILE_BASIC_INFO()
    if not query(
        wintypes.HANDLE(handle), 9, ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    ) or not query(
        wintypes.HANDLE(handle), 1, ctypes.byref(standard),
        ctypes.sizeof(standard),
    ) or not query(
        wintypes.HANDLE(handle), 0, ctypes.byref(basic), ctypes.sizeof(basic)
    ):
        raise _error("reset_path_unsafe")
    return {
        "allocation_size": int(standard.AllocationSize),
        "change_time": int(basic.ChangeTime),
        "creation_time": int(basic.CreationTime),
        "delete_pending": bool(standard.DeletePending),
        "directory": bool(standard.Directory),
        "end_of_file": int(standard.EndOfFile),
        "file_attributes": int(attributes.FileAttributes),
        "file_id": _windows_handle_identity(handle),
        "last_access_time": int(basic.LastAccessTime),
        "last_write_time": int(basic.LastWriteTime),
        "link_count": int(standard.NumberOfLinks),
        "reparse_tag": int(attributes.ReparseTag),
    }


def _windows_validate_handle(handle: int, *, directory: bool) -> None:
    snapshot = _windows_handle_snapshot(handle)
    if (
        snapshot["file_attributes"] & 0x400
        or snapshot["directory"] is not directory
        or snapshot["delete_pending"]
        or (not directory and snapshot["link_count"] != 1)
    ):
        raise _error("reset_path_unsafe")


def _windows_absolute_parts(path: Path) -> tuple[str, tuple[str, ...]]:
    absolute = Path(os.path.abspath(path))
    anchor = absolute.anchor
    if re.fullmatch(r"[A-Za-z]:\\", anchor) is None:
        raise _error("reset_path_unsafe")
    relative = absolute.relative_to(anchor)
    parts = relative.parts
    if any(
        not part
        or part in {".", ".."}
        or part.endswith((" ", "."))
        or ":" in part
        or "\\" in part
        or "/" in part
        for part in parts
    ):
        raise _error("reset_path_unsafe")
    return anchor, parts


def _windows_open_directory_chain(path: Path) -> int:
    anchor, parts = _windows_absolute_parts(path)
    handle = _windows_open_handle(
        Path(anchor),
        0x00100000 | 0x00000001 | 0x00000020 | 0x00000080,
        0x02000000 | 0x00200000,
    )
    try:
        _windows_validate_handle(handle, directory=True)
        for part in parts:
            child = _windows_create_relative(
                handle,
                part,
                directory=True,
                create_new=False,
                desired_access=(
                    0x00100000 | 0x00000001 | 0x00000020 | 0x00000080
                ),
            )
            try:
                _windows_validate_handle(child, directory=True)
            except Exception:
                _windows_close_handle(child)
                raise
            _windows_close_handle(handle)
            handle = child
        return handle
    except Exception:
        _windows_close_handle(handle)
        raise


def _windows_open_path_no_follow(
    path: Path, *, expect_directory: bool | None = None
) -> int:
    absolute = Path(os.path.abspath(path))
    if absolute == Path(absolute.anchor):
        if expect_directory is False:
            raise _error("reset_path_unsafe")
        return _windows_open_directory_chain(absolute)
    if expect_directory is None:
        try:
            info = absolute.lstat()
        except OSError as error:
            raise _error("reset_path_unsafe") from error
        directory = stat.S_ISDIR(info.st_mode)
    else:
        directory = expect_directory
    parent = _windows_open_directory_chain(absolute.parent)
    try:
        handle = _windows_create_relative(
            parent,
            absolute.name,
            directory=directory,
            create_new=False,
            desired_access=(
                0x00100000 | 0x00000001 | 0x00000020 | 0x00000080
            ),
        )
        try:
            _windows_validate_handle(handle, directory=directory)
        except Exception:
            _windows_close_handle(handle)
            raise
        return handle
    finally:
        _windows_close_handle(parent)


@contextmanager
def _verified_parent(path: Path):
    """Hold the exact non-reparse parent while a destructive name change occurs."""

    parent = path.parent
    if os.name == "nt":
        handle = _windows_open_directory_chain(parent)
        identity = _windows_handle_identity(handle)
        try:
            yield handle, identity
            revalidated = _windows_open_directory_chain(parent)
            try:
                current_identity = _windows_handle_identity(revalidated)
            finally:
                _windows_close_handle(revalidated)
            if current_identity != identity:
                raise _error("reset_path_unsafe")
        finally:
            _windows_close_handle(handle)
        return
    _safe_directory(parent)
    identity = _path_identity(parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent, flags)
    try:
        if _descriptor_identity(descriptor, os.fstat(descriptor)) != identity:
            raise _error("reset_path_unsafe")
        yield descriptor, identity
        if _path_identity(parent) != identity:
            raise _error("reset_path_unsafe")
    finally:
        os.close(descriptor)


def _revalidate_parent(path: Path, expected_identity: str) -> None:
    if os.name == "nt":
        handle = _windows_open_directory_chain(path.parent)
        try:
            identity = _windows_handle_identity(handle)
        finally:
            _windows_close_handle(handle)
    else:
        identity = _path_identity(path.parent)
    if identity != expected_identity:
        raise _error("reset_path_unsafe")


def _windows_create_relative(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    create_new: bool = True,
    desired_access: int | None = None,
    share_access: int = 0x1 | 0x2 | 0x4,
) -> int:
    import ctypes
    from ctypes import wintypes

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

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    name_buffer = ctypes.create_unicode_buffer(name)
    name_value = UNICODE_STRING(
        len(name.encode("utf-16-le")),
        len(name.encode("utf-16-le")) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = OBJECT_ATTRIBUTES(
        ctypes.sizeof(OBJECT_ATTRIBUTES), wintypes.HANDLE(parent_handle),
        ctypes.pointer(name_value), 0x40, None, None,
    )
    status_block = IO_STATUS_BLOCK()
    handle = wintypes.HANDLE()
    create = ctypes.windll.ntdll.NtCreateFile
    create.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        ctypes.POINTER(OBJECT_ATTRIBUTES), ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
    ]
    create.restype = ctypes.c_long
    if desired_access is None:
        desired_access = (
            0x00100000 | 0x00000080 | 0x00000008
            | (
                0x00000001 | 0x00000002
                if directory else 0x00000001 | 0x00000002 | 0x00000004
            )
        )
    options = (
        0x00000001 | 0x00200000 | 0x00000020
        if directory
        else 0x00000040 | 0x00200000 | 0x00000020
    )
    if create_new and not directory:
        options |= 0x00000002
    status = create(
        ctypes.byref(handle), desired_access, ctypes.byref(attributes),
        ctypes.byref(status_block), None, 0x10 if directory else 0x80,
        share_access, 2 if create_new else 1, options, None, 0,
    )
    if status < 0 or not handle.value:
        raise _error("reset_failed")
    return int(handle.value)


def _windows_enumerate_directory_handle(handle: int) -> list[str]:
    """Enumerate names through an already verified directory handle."""

    import ctypes
    import struct
    from ctypes import wintypes

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    before = _windows_handle_snapshot(handle)
    names: list[str] = []
    query = ctypes.windll.ntdll.NtQueryDirectoryFile
    query.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, wintypes.ULONG,
        wintypes.ULONG, wintypes.BOOLEAN, ctypes.c_void_p, wintypes.BOOLEAN,
    ]
    query.restype = ctypes.c_long
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(65_536)
        status_block = IO_STATUS_BLOCK()
        status = int(query(
            wintypes.HANDLE(handle), None, None, None,
            ctypes.byref(status_block), buffer, len(buffer), 12,
            False, None, restart,
        ))
        restart = False
        unsigned_status = status & 0xFFFFFFFF
        if unsigned_status == 0x80000006:  # STATUS_NO_MORE_FILES
            break
        if status < 0 and unsigned_status != 0x80000005:
            raise _error("reset_path_unsafe")
        length = int(status_block.Information)
        if length <= 0 or length > len(buffer):
            raise _error("reset_path_unsafe")
        raw = buffer.raw[:length]
        offset = 0
        while True:
            if offset + 12 > length:
                raise _error("reset_path_unsafe")
            next_offset, _file_index, name_length = struct.unpack_from(
                "<III", raw, offset
            )
            if (
                name_length == 0
                or name_length % 2
                or offset + 12 + name_length > length
            ):
                raise _error("reset_path_unsafe")
            try:
                name = raw[offset + 12:offset + 12 + name_length].decode(
                    "utf-16-le", errors="strict"
                )
            except UnicodeDecodeError as error:
                raise _error("reset_path_unsafe") from error
            if name not in {".", ".."}:
                names.append(name)
            if next_offset == 0:
                break
            if next_offset % 8 or next_offset < 12 or offset + next_offset >= length:
                raise _error("reset_path_unsafe")
            offset += next_offset
    after = _windows_handle_snapshot(handle)
    stable = {
        "delete_pending", "directory", "file_attributes", "file_id",
        "link_count", "reparse_tag",
    }
    if any(after[field] != before[field] for field in stable):
        raise _error("reset_path_unsafe")
    if len(names) != len(set(names)):
        raise _error("reset_path_unsafe")
    return names


def _windows_relative_entry_identity(
    parent_handle: int, entry_name: str
) -> str | None:
    """Resolve one child through the held directory handle without opening it."""

    import ctypes
    from ctypes import wintypes

    if (
        not entry_name
        or entry_name in {".", ".."}
        or "\\" in entry_name
        or "/" in entry_name
    ):
        raise _error("reset_path_unsafe")

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

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    name_buffer = ctypes.create_unicode_buffer(entry_name)
    encoded_length = len(entry_name.encode("utf-16-le"))
    name = UNICODE_STRING(
        encoded_length,
        encoded_length,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    query = ctypes.windll.ntdll.NtQueryDirectoryFile
    query.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, wintypes.ULONG,
        wintypes.ULONG, wintypes.BOOLEAN, ctypes.POINTER(UNICODE_STRING),
        wintypes.BOOLEAN,
    ]
    query.restype = ctypes.c_long
    parent_before = _windows_handle_snapshot(parent_handle)
    volume_serial = parent_before["file_id"].split(":")[1]
    buffer = ctypes.create_string_buffer(65_536)
    status_block = IO_STATUS_BLOCK()
    status = int(query(
        wintypes.HANDLE(parent_handle), None, None, None,
        ctypes.byref(status_block), buffer, len(buffer),
        60, True, ctypes.byref(name), True,
    ))
    unsigned_status = status & 0xFFFFFFFF
    if unsigned_status in {0x80000006, 0xC000000F}:
        result = None
    elif status < 0:
        raise _error("reset_path_unsafe")
    else:
        length = int(status_block.Information)
        if length < ctypes.sizeof(FILE_ID_EXTD_DIR_INFO) or length > len(buffer):
            raise _error("reset_path_unsafe")
        item = FILE_ID_EXTD_DIR_INFO.from_buffer(buffer)
        name_offset = ctypes.sizeof(FILE_ID_EXTD_DIR_INFO)
        name_length = int(item.FileNameLength)
        if (
            int(item.NextEntryOffset) != 0
            or name_length == 0
            or name_length % 2
            or name_offset + name_length > length
        ):
            raise _error("reset_path_unsafe")
        try:
            returned_name = buffer.raw[
                name_offset:name_offset + name_length
            ].decode("utf-16-le", errors="strict")
        except UnicodeDecodeError as error:
            raise _error("reset_path_unsafe") from error
        if returned_name.casefold() != entry_name.casefold():
            raise _error("reset_path_unsafe")
        if (
            int(item.FileAttributes) & (0x10 | 0x400)
            or int(item.ReparsePointTag) != 0
        ):
            raise _error("reset_path_unsafe")
        result = f"windows:{volume_serial}:{bytes(item.FileId).hex()}"
    parent_after = _windows_handle_snapshot(parent_handle)
    stable = {
        "delete_pending", "directory", "file_attributes", "file_id",
        "link_count", "reparse_tag",
    }
    if any(parent_after[field] != parent_before[field] for field in stable):
        raise _error("reset_path_unsafe")
    return result


def _windows_enumerate_directory(path: Path) -> list[str]:
    """Enumerate through a component-walked, verified directory handle."""

    handle = _windows_open_directory_chain(path)
    try:
        return _windows_enumerate_directory_handle(handle)
    finally:
        _windows_close_handle(handle)


def _windows_enumerate_directory_direct(path: Path) -> list[str]:
    """Enumerate a configured runtime directory through its final no-follow handle."""

    handle = _windows_open_handle(
        path,
        0x00100000 | 0x00000001 | 0x00000080,
        0x02000000 | 0x00200000,
    )
    try:
        _windows_validate_handle(handle, directory=True)
        return _windows_enumerate_directory_handle(handle)
    finally:
        _windows_close_handle(handle)


def _open_new_regular(
    path: Path, *, read_write: bool, share_delete: bool = True
) -> int:
    with _verified_parent(path) as (parent_handle, parent_identity):
        _revalidate_parent(path, parent_identity)
        if os.name == "nt":
            import msvcrt

            handle = _windows_create_relative(
                parent_handle,
                path.name,
                directory=False,
                share_access=0x1 | 0x2 | (0x4 if share_delete else 0),
            )
            flags = getattr(os, "O_BINARY", 0) | (
                os.O_RDWR if read_write else os.O_WRONLY
            )
            try:
                descriptor = msvcrt.open_osfhandle(handle, flags)
            except Exception:
                _windows_close_handle(handle)
                raise
        else:
            flags = (
                (os.O_RDWR if read_write else os.O_WRONLY)
                | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_handle)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _error("reset_path_unsafe")
            _revalidate_parent(path, parent_identity)
            if _path_identity(path) != _descriptor_identity(descriptor, info):
                raise _error("reset_path_unsafe")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor


def _open_existing_regular(
    path: Path,
    expected_identity: str,
    *,
    read_write: bool = False,
    share_write: bool = True,
    delete_access: bool = False,
    share_delete: bool = True,
) -> int:
    with _verified_parent(path) as (parent_handle, parent_identity):
        _revalidate_parent(path, parent_identity)
        if os.name == "nt":
            import msvcrt

            access = 0x00100000 | 0x00000080 | 0x00000001
            if read_write:
                access |= 0x00000002
            if delete_access:
                access |= 0x00010000
            handle = _windows_create_relative(
                parent_handle, path.name, directory=False,
                create_new=False, desired_access=access,
                share_access=(
                    0x1
                    | (0x2 if share_write else 0)
                    | (0x4 if share_delete else 0)
                ),
            )
            flags = getattr(os, "O_BINARY", 0) | (
                os.O_RDWR if read_write else os.O_RDONLY
            )
            try:
                descriptor = msvcrt.open_osfhandle(handle, flags)
            except Exception:
                _windows_close_handle(handle)
                raise
        else:
            flags = (
                (os.O_RDWR if read_write else os.O_RDONLY)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path.name, flags, dir_fd=parent_handle)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or _descriptor_identity(descriptor, info) != expected_identity
            ):
                raise _error("reset_path_unsafe")
            _revalidate_parent(path, parent_identity)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor


def _mkdir_verified(path: Path) -> None:
    with _verified_parent(path) as (parent_handle, parent_identity):
        _revalidate_parent(path, parent_identity)
        if os.name == "nt":
            handle = _windows_create_relative(parent_handle, path.name, directory=True)
            _windows_close_after_mutation(handle)
        else:
            os.mkdir(path.name, 0o700, dir_fd=parent_handle)
        _revalidate_parent(path, parent_identity)
        _safe_directory(path)


def _windows_rename(
    source_handle: int, target: Path, parent_handle: int, *, replace: bool
) -> None:
    import ctypes
    from ctypes import wintypes

    class FILE_RENAME_INFO(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.DWORD),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

    encoded = target.name.encode("utf-16-le")
    offset = FILE_RENAME_INFO.FileName.offset
    buffer = ctypes.create_string_buffer(offset + len(encoded) + 2)
    header = FILE_RENAME_INFO.from_buffer(buffer)
    header.ReplaceIfExists = 1 if replace else 0
    header.RootDirectory = wintypes.HANDLE(parent_handle)
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    status_block = IO_STATUS_BLOCK()
    setter = ctypes.windll.ntdll.NtSetInformationFile
    setter.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
        wintypes.ULONG, wintypes.ULONG,
    ]
    setter.restype = ctypes.c_long
    status = setter(
        wintypes.HANDLE(source_handle), ctypes.byref(status_block),
        buffer, len(buffer), 10,
    )
    if status < 0:
        raise _error("reset_failed")


def _windows_unlink_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    disposition = FILE_DISPOSITION_INFO(1)
    setter = ctypes.windll.kernel32.SetFileInformationByHandle
    setter.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    setter.restype = wintypes.BOOL
    if not setter(
        wintypes.HANDLE(handle), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise _error("reset_failed")


def _posix_rename_no_replace(
    source_name: str, target_name: str, parent_descriptor: int
) -> None:
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(parent_descriptor, source, parent_descriptor, target, 1)
    elif sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        rename = library.renameatx_np
        rename.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(parent_descriptor, source, parent_descriptor, target, 4)
    else:
        raise _error("reset_failed")
    if result != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), target_name)


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        identity = _path_identity(path)
        handle = _windows_open_handle(
            path,
            0x80000000 | 0x40000000,
            0x02000000 | 0x00200000 | 0x80000000,
        )
        try:
            if _windows_handle_identity(handle) != identity:
                raise _error("reset_path_unsafe")
            flush = ctypes.windll.kernel32.FlushFileBuffers
            flush.argtypes = [wintypes.HANDLE]
            flush.restype = wintypes.BOOL
            if not flush(wintypes.HANDLE(handle)):
                error_code = ctypes.windll.kernel32.GetLastError()
                raise _error("reset_failed") from OSError(error_code, "FlushFileBuffers")
            if _path_identity(path) != identity:
                raise _error("reset_path_unsafe")
        finally:
            _windows_close_after_mutation(handle)
        return
    with _verified_parent(path / ".directory-flush-sentinel") as (descriptor, identity):
        _revalidate_parent(path / ".directory-flush-sentinel", identity)
        os.fsync(descriptor)


def _relative_entry_identity(path: Path, parent_handle: int) -> str:
    if os.name == "nt":
        identity = _windows_relative_entry_identity(parent_handle, path.name)
        if identity is None:
            raise _error("reset_path_unsafe")
        return identity
    try:
        info = os.stat(path.name, dir_fd=parent_handle, follow_symlinks=False)
    except OSError as exception:
        raise _error("reset_path_unsafe") from exception
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _error("reset_path_unsafe")
    return _identity(info)


def _relative_entry_exists(path: Path, parent_handle: int) -> bool:
    if os.name == "nt":
        return _windows_relative_entry_identity(parent_handle, path.name) is not None
    try:
        os.stat(path.name, dir_fd=parent_handle, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exception:
        raise _error("reset_path_unsafe") from exception
    return True


def _revalidate_source_entry(
    path: Path, parent_handle: int, expected_identity: str
) -> None:
    if _relative_entry_identity(path, parent_handle) != expected_identity:
        raise _error("reset_path_unsafe")


def _rename_verified(
    source: Path,
    target: Path,
    expected_identity: str,
    *,
    replace: bool,
    expected_target_identity: str | None = None,
    flush_parent: bool = True,
) -> None:
    if source.parent != target.parent:
        raise _error("reset_path_unsafe")
    with _verified_parent(source) as (parent_handle, parent_identity):
        _revalidate_parent(source, parent_identity)
        if os.name == "nt":
            source_handle = _windows_create_relative(
                parent_handle, source.name, directory=False, create_new=False,
                desired_access=(
                    0x40000000 | 0x00010000 | 0x00000080 | 0x00100000
                ),
            )
            if _windows_handle_identity(source_handle) != expected_identity:
                _windows_close_handle(source_handle)
                raise _error("reset_path_unsafe")
        else:
            source_handle = os.open(
                source.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_handle,
            )
            if _descriptor_identity(source_handle, os.fstat(source_handle)) != expected_identity:
                os.close(source_handle)
                raise _error("reset_path_unsafe")
        target_exists = _relative_entry_exists(target, parent_handle)
        if replace:
            if (
                not target_exists
                or expected_target_identity is None
                or _relative_entry_identity(target, parent_handle)
                != expected_target_identity
            ):
                if os.name == "nt":
                    _windows_close_handle(source_handle)
                else:
                    os.close(source_handle)
                raise _error("reset_path_unsafe")
        elif target_exists:
            if os.name == "nt":
                _windows_close_handle(source_handle)
            else:
                os.close(source_handle)
            raise _error("reset_failed")
        try:
            mutation_completed = False
            _revalidate_parent(source, parent_identity)
            _revalidate_source_entry(source, parent_handle, expected_identity)
            if os.name == "nt":
                _windows_rename(
                    source_handle, target, parent_handle, replace=replace
                )
                mutation_completed = True
                _windows_flush_file_handle(source_handle)
            elif replace:
                os.rename(
                    source.name, target.name,
                    src_dir_fd=parent_handle, dst_dir_fd=parent_handle,
                )
            else:
                _posix_rename_no_replace(source.name, target.name, parent_handle)
        finally:
            if os.name == "nt":
                if mutation_completed:
                    _windows_close_after_mutation(source_handle)
                else:
                    _windows_close_handle(source_handle)
            else:
                os.close(source_handle)
        _revalidate_parent(target, parent_identity)
        if _relative_entry_identity(target, parent_handle) != expected_identity:
            raise _error("reset_failed")
        if os.name == "nt":
            descriptor = _open_existing_regular(
                target, expected_identity, read_write=True
            )
            try:
                if _descriptor_identity(descriptor, os.fstat(descriptor)) != expected_identity:
                    raise _error("reset_path_unsafe")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    if flush_parent:
        _flush_directory(target.parent)


def _install_no_overwrite(
    staging: Path, target: Path, *, flush_parent: bool = True
) -> None:
    _safe_regular(staging)
    _rename_verified(
        staging, target, _path_identity(staging), replace=False,
        flush_parent=flush_parent,
    )


def _move_verified(
    source: Path, target: Path, expected_identity: str, *, flush_parent: bool = True
) -> None:
    _rename_verified(
        source, target, expected_identity, replace=False,
        flush_parent=flush_parent,
    )


def _replace_verified(
    source: Path, target: Path, source_identity: str, target_identity: str
) -> None:
    _rename_verified(
        source, target, source_identity, replace=True,
        expected_target_identity=target_identity,
    )


def _unlink_verified(
    path: Path, expected_identity: str, *, flush_parent: bool = True
) -> None:
    with _verified_parent(path) as (parent_handle, parent_identity):
        _revalidate_parent(path, parent_identity)
        if os.name == "nt":
            handle = _windows_create_relative(
                parent_handle, path.name, directory=False, create_new=False,
                desired_access=0x00010000 | 0x00000080 | 0x00100000,
            )
            if _windows_handle_identity(handle) != expected_identity:
                _windows_close_handle(handle)
                raise _error("reset_path_unsafe")
        else:
            handle = os.open(
                path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_handle,
            )
            if _descriptor_identity(handle, os.fstat(handle)) != expected_identity:
                os.close(handle)
                raise _error("reset_path_unsafe")
        try:
            deletion_started = False
            _revalidate_parent(path, parent_identity)
            _revalidate_source_entry(path, parent_handle, expected_identity)
            if os.name == "nt":
                _windows_unlink_handle(handle)
                deletion_started = True
            else:
                os.unlink(path.name, dir_fd=parent_handle)
        finally:
            if os.name == "nt":
                if deletion_started:
                    _windows_close_after_mutation(handle)
                else:
                    _windows_close_handle(handle)
            else:
                os.close(handle)
        _revalidate_parent(path, parent_identity)
    if flush_parent:
        _flush_directory(path.parent)


def _unlink_existing(path: Path) -> None:
    if os.path.lexists(path):
        _safe_regular(path)
        _unlink_verified(path, _path_identity(path))


def _rmdir_verified(
    path: Path, expected_identity: str, *, flush_parent: bool = True
) -> None:
    _safe_directory(path)
    with _verified_parent(path) as (parent_handle, parent_identity):
        _revalidate_parent(path, parent_identity)
        if _path_identity(path) != expected_identity:
            raise _error("reset_path_unsafe")
        if os.name == "nt":
            handle = _windows_create_relative(
                parent_handle, path.name, directory=True, create_new=False,
                desired_access=0x00010000 | 0x00000080 | 0x00100000,
            )
            try:
                if _windows_handle_identity(handle) != expected_identity:
                    raise _error("reset_path_unsafe")
                if _path_identity(path) != expected_identity:
                    raise _error("reset_path_unsafe")
                _windows_unlink_handle(handle)
            finally:
                _windows_close_after_mutation(handle)
        else:
            os.rmdir(path.name, dir_fd=parent_handle)
        _revalidate_parent(path, parent_identity)
    if flush_parent:
        _flush_directory(path.parent)


def _copy_verified(
    source: Path,
    target: Path,
    expected_sha256: str,
    crash_checkpoint: Callable[[str], None] | None = None,
    *,
    flush_parent: bool = True,
    expected_source_identity: str | None = None,
) -> None:
    source_identity = _path_identity(source)
    if (
        expected_source_identity is not None
        and source_identity != expected_source_identity
    ):
        raise _error("reset_recovery_invalid")
    source_descriptor = _open_existing_regular(
        source,
        source_identity,
        share_write=False,
        share_delete=False,
    )
    try:
        target_descriptor = _open_new_regular(target, read_write=False)
        digest = hashlib.sha256()
        try:
            first_block = True
            while True:
                block = os.read(source_descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                if first_block and crash_checkpoint is not None:
                    boundary = max(1, len(block) // 2)
                    _write_all(target_descriptor, block[:boundary])
                    crash_checkpoint("recovery:backup_copy_partial")
                    _write_all(target_descriptor, block[boundary:])
                    first_block = False
                else:
                    _write_all(target_descriptor, block)
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        source_after = os.fstat(source_descriptor)
        if (
            source_after.st_nlink != 1
            or _descriptor_identity(source_descriptor, source_after)
            != source_identity
            or digest.hexdigest() != expected_sha256
        ):
            raise _error("reset_recovery_invalid")
    finally:
        os.close(source_descriptor)
    if _stable_sha256(target) != expected_sha256:
        raise _error("reset_recovery_invalid")
    if flush_parent:
        _flush_directory(target.parent)


def _journal_update(
    path: Path,
    next_path: Path,
    journal: dict[str, Any],
    stage: str,
    crash_checkpoint: Callable[[str], None] | None = None,
    checkpoint_name: str | None = None,
) -> None:
    if stage not in STAGES:
        raise _error("reset_failed")
    value = dict(journal)
    value["stage"] = stage
    value["journal_sequence"] = int(journal.get("journal_sequence", 0)) + 1
    value["updated_at"] = _utc_stamp()[1]
    _write_durable(next_path, value)
    label = checkpoint_name or stage
    if crash_checkpoint is not None:
        crash_checkpoint(f"{label}:next_durable")
    next_identity = _path_identity(next_path)
    if os.path.lexists(path):
        _replace_verified(next_path, path, next_identity, _path_identity(path))
    else:
        _move_verified(next_path, path, next_identity)
    journal.clear()
    journal.update(value)
    if crash_checkpoint is not None:
        crash_checkpoint(label)


def execute_database_reset(
    database_path: Path | str,
    *, expected_plan_token: str | None,
    expected_backup_path: str | None,
    expected_audit_path: str | None,
    confirm_destroy_canonical_history: bool,
    repository_root: Path | str = PROJECT_ROOT,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not confirm_destroy_canonical_history or not all((expected_plan_token, expected_backup_path, expected_audit_path)):
        raise _error("reset_confirmation_required")
    if not isinstance(expected_plan_token, str) or re.fullmatch(r"[0-9a-f]{64}", expected_plan_token) is None:
        raise _error("reset_plan_stale")
    stamp = _reviewed_output_paths(expected_backup_path, expected_audit_path)
    root = Path(repository_root).resolve(strict=True)
    _safe_directory(root)
    _safe_directory(root / "data")
    _git_safety(root, _prospective_paths(stamp, expected_plan_token))
    database = root / "data" / "helios.db"
    lock_path, state_path, next_path = coordination_paths(database)
    if os.path.lexists(state_path) or os.path.lexists(next_path):
        raise _error("database_reset_recovery_required")
    if any(
        os.path.lexists(path)
        for path in _reset_output_paths(root, expected_backup_path, expected_audit_path)
    ):
        raise _error("reset_plan_stale")
    try:
        lease = acquire_database_lease(database, shared=False)
    except ResetRecoveryRequiredError as exception:
        raise _error("database_reset_recovery_required") from exception
    except MaintenanceLockError as exception:
        raise _error("database_maintenance_in_progress") from exception
    backup = root / expected_backup_path
    audit = root / expected_audit_path
    journal: dict[str, Any] | None = None
    created_backups_directory = False
    try:
        plan, manifest = _make_plan(root, database_path, stamp=stamp, backup_path=expected_backup_path, audit_path=expected_audit_path)
        if plan["plan_token"] != expected_plan_token or plan["backup_path"] != expected_backup_path or plan["audit_path"] != expected_audit_path:
            raise _error("reset_plan_stale")
        before_observations = _observations(database)
        source = _open_reset_source(database)
        try:
            source_label, source_digest = _validate_source(source)
            backups = root / "backups"
            if manifest["directories"]["backups"] is None:
                try:
                    _mkdir_verified(backups)
                    created_backups_directory = True
                except OSError as exception:
                    raise _error("reset_failed") from exception
            backups_info = _safe_directory(backups)
            if (
                manifest["directories"]["backups"] is not None
                and _path_identity(backups) != manifest["directories"]["backups"]
            ):
                raise _error("reset_failed")
            partial = Path(f"{backup}.partial")
            if any(os.path.lexists(path) for path in (backup, partial, audit, Path(f"{audit}.partial"))):
                raise _error("reset_failed")
            partial_identity = _reserve_regular(partial)
            target = _open_direct(partial, read_only=False)
            try:
                source.backup(target)
            finally:
                target.close()
            if _path_identity(partial) != partial_identity:
                raise _error("reset_failed")
            if any(os.path.lexists(Path(f"{partial}{suffix}")) for suffix in ("-wal", "-shm", "-journal")):
                raise _error("reset_failed")
        finally:
            source.rollback()
            source.close()
        after_observations = _observations(database)
        for key in ("database", "wal"):
            if before_observations[key] != after_observations[key]:
                raise _error("reset_failed")
        verified = _open_connection(partial, immutable=True)
        try:
            backup_label, backup_digest = _validate_source(verified)
        finally:
            verified.rollback()
            verified.close()
        if (backup_label, backup_digest) != (source_label, source_digest):
            raise _error("reset_failed")
        backup_sha = _stable_sha256(partial)
        _install_no_overwrite(partial, backup)
        component_names = {"database": database, "wal": Path(f"{database}-wal"), "shm": Path(f"{database}-shm")}
        source_components = {
            key: (_component(path, f"data/{path.name}") if path.exists() else None)
            for key, path in component_names.items()
        }
        source_files = {
            key: (None if value is None else _source_file(value))
            for key, value in source_components.items()
        }
        quarantine = {
            key: (None if value is None else {
                "identity": value["identity"],
                "path": f"data/.{path.name}.reset-{expected_plan_token}.original",
            }) for (key, path), value in zip(component_names.items(), source_files.values())
        }
        journal = {
            "audit_path": expected_audit_path,
            "backup": {"identity": _path_identity(backup), "path": expected_backup_path, "sha256": backup_sha},
            "database_path": "data/helios.db", "implementation_commit": plan["implementation_commit"],
            "journal_sequence": 0, "journal_version": 1, "logical_digest": source_digest,
            "plan_manifest_sha256": expected_plan_token, "plan_token": expected_plan_token,
            "quarantine": quarantine, "recovery_commit": RECOVERY_COMMIT_BY_SCHEMA[source_label],
            "reset_protocol_version": RESET_PROTOCOL_VERSION, "source_files": source_files,
            "source_observations": {
                "before_open": before_observations,
                "after_close": after_observations,
            },
            "source_schema_label": source_label, "stage": "ready_to_quarantine", "updated_at": _utc_stamp()[1],
        }
        _journal_update(state_path, next_path, journal, "ready_to_quarantine", crash_checkpoint)
        _journal_update(state_path, next_path, journal, "quarantining", crash_checkpoint)
        for key, path in component_names.items():
            if quarantine[key] is not None:
                _move_verified(
                    path,
                    root / quarantine[key]["path"],
                    quarantine[key]["identity"],
                )
        _journal_update(state_path, next_path, journal, "original_quarantined", crash_checkpoint)
        _journal_update(state_path, next_path, journal, "installing_fresh", crash_checkpoint)
        fresh_identity = _reserve_regular(database)
        fresh = _open_direct(database, read_only=False)
        try:
            fresh.executescript(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
            _seed_initial_room(fresh)
        finally:
            fresh.close()
        if _path_identity(database) != fresh_identity:
            raise _error("reset_failed")
        _journal_update(state_path, next_path, journal, "fresh_installed", crash_checkpoint)
        _journal_update(state_path, next_path, journal, "validating_fresh", crash_checkpoint)
        fresh = _open_direct(database, read_only=True)
        try:
            validate_v14_foundation(fresh)
            validate_database_integrity(fresh)
        finally:
            fresh.rollback()
            fresh.close()
        _journal_update(state_path, next_path, journal, "fresh_validated", crash_checkpoint)
        _journal_update(state_path, next_path, journal, "cleaning_quarantine", crash_checkpoint)
        for value in quarantine.values():
            if value is not None:
                _unlink_verified(root / value["path"], value["identity"])
        _journal_update(state_path, next_path, journal, "finalizing", crash_checkpoint)
        audit_value = _audit_value(journal, "reset", "1.4")
        _write_durable(Path(f"{audit}.partial"), audit_value)
        _install_no_overwrite(Path(f"{audit}.partial"), audit)
        _unlink_existing(state_path)
        _unlink_existing(next_path)
        return _reset_report(journal)
    except DatabaseResetError:
        if (
            journal is not None
            and state_path.exists()
            and not os.path.lexists(audit)
        ):
            _restore_from_journal(root, journal, state_path, next_path)
        elif journal is None:
            _cleanup_prejournal_artifacts(backup, audit, created_backups_directory)
        raise
    except Exception as exception:
        if (
            journal is not None
            and state_path.exists()
            and not os.path.lexists(audit)
        ):
            try:
                _restore_from_journal(root, journal, state_path, next_path)
            except Exception:
                pass
        elif journal is None:
            _cleanup_prejournal_artifacts(backup, audit, created_backups_directory)
        raise _error("reset_failed") from exception
    finally:
        lease.close()


def _cleanup_prejournal_artifacts(
    backup: Path, audit: Path, created_backups_directory: bool
) -> None:
    for path in (
        Path(f"{backup}.partial-wal"), Path(f"{backup}.partial-shm"),
        Path(f"{backup}.partial-journal"), Path(f"{backup}.partial"),
        backup, Path(f"{audit}.partial"),
    ):
        if os.path.lexists(path):
            _unlink_existing(path)
    if created_backups_directory:
        try:
            _rmdir_verified(backup.parent, _path_identity(backup.parent))
        except OSError:
            pass


def _audit_value(journal: dict[str, Any], outcome: str, terminal: str) -> dict[str, Any]:
    return {
        "audit_version": 1, "backup_identity": journal["backup"]["identity"],
        "backup_path": journal["backup"]["path"], "backup_sha256": journal["backup"]["sha256"],
        "completed_at": _utc_stamp()[1], "database_path": journal["database_path"],
        "implementation_commit": journal["implementation_commit"], "logical_digest": journal["logical_digest"],
        "outcome": outcome, "plan_manifest_sha256": journal["plan_manifest_sha256"],
        "plan_token": journal["plan_token"],
        "quarantine_identities": {key: (None if value is None else value["identity"]) for key, value in journal["quarantine"].items()},
        "recovery_commit": journal["recovery_commit"], "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION,
        "source_observations": journal["source_observations"], "source_schema_label": journal["source_schema_label"],
        "terminal_schema_label": terminal,
    }


def _reset_report(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_path": journal["audit_path"], "backup_path": journal["backup"]["path"],
        "backup_sha256": journal["backup"]["sha256"], "foreign_key_violations": 0,
        "implementation_commit": journal["implementation_commit"], "integrity_check": "ok",
        "old_schema_label": journal["source_schema_label"], "plan_token": journal["plan_token"],
        "recovery_commit": journal["recovery_commit"], "reset_protocol_version": LEGACY_RESET_PROTOCOL_VERSION,
        "room_shared_effective_from_room_sequence_no": 1, "schema_label": "1.4", "status": "reset",
    }


def _checkpoint(callback: Callable[[str], None] | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _active_source_matches(database: Path, journal: dict[str, Any]) -> bool:
    if not os.path.lexists(database):
        return False
    try:
        restored = _open_reset_source(database)
        try:
            label, digest = _validate_source(restored)
        finally:
            restored.rollback()
            restored.close()
    except DatabaseResetError as exception:
        if exception.code == "reset_path_unsafe":
            raise
        return False
    except (SchemaValidationError, sqlite3.Error, OSError, TypeError, ValueError):
        return False
    return label == journal["source_schema_label"] and digest == journal["logical_digest"]


def _install_recovery_audit(
    root: Path, journal: dict[str, Any], outcome: str, terminal: str,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> None:
    audit = root / journal["audit_path"]
    partial = Path(f"{audit}.partial")
    if os.path.lexists(audit):
        value = _validate_audit(_read_control_json(audit), journal)
        if value["outcome"] != outcome:
            raise _error("reset_recovery_invalid")
        if os.path.lexists(partial):
            _unlink_existing(partial)
        return
    if os.path.lexists(partial):
        try:
            value = _validate_audit(_read_control_json(partial), journal)
        except DatabaseResetError as exception:
            if exception.code == "reset_path_unsafe":
                raise
            _unlink_existing(partial)
        else:
            if value["outcome"] != outcome:
                raise _error("reset_recovery_invalid")
    if not os.path.lexists(partial):
        _write_durable(
            partial, _audit_value(journal, outcome, terminal),
            crash_checkpoint=crash_checkpoint,
            partial_checkpoint="recovery:audit_write_partial",
        )
    _checkpoint(crash_checkpoint, "recovery:audit_partial_durable")
    _install_no_overwrite(partial, audit)


def _restore_from_journal(
    root: Path,
    journal: dict[str, Any],
    state: Path,
    next_path: Path,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> None:
    database = root / journal["database_path"]
    active = {
        "database": database,
        "wal": Path(f"{database}-wal"),
        "shm": Path(f"{database}-shm"),
    }
    token = journal["plan_token"]
    failed_paths = {
        key: root / f"data/.{path.name}.reset-{token}.failed-new"
        for key, path in active.items()
    }
    restoring = root / f"data/.helios.db.reset-{token}.restoring"
    if not _active_source_matches(database, journal):
        _journal_update(
            state, next_path, journal, journal["stage"], crash_checkpoint,
            "recovery:journal:restore_quarantine",
        )
        for key, path in active.items():
            if not os.path.lexists(path):
                continue
            source = journal["source_files"][key]
            identity = _path_identity(path)
            if source is not None and identity == source["identity"]:
                continue
            failed_path = failed_paths[key]
            if os.path.lexists(failed_path):
                raise _error("reset_recovery_invalid")
            _checkpoint(crash_checkpoint, f"recovery:before_quarantine:{key}")
            _move_verified(path, failed_path, identity)
            _checkpoint(crash_checkpoint, f"recovery:after_quarantine:{key}")

        _journal_update(
            state, next_path, journal, journal["stage"], crash_checkpoint,
            "recovery:journal:restore_install",
        )
        complete_originals = all(
            value is None
            or (
                os.path.lexists(active[key])
                and _path_identity(active[key]) == value["identity"]
            )
            or (
                os.path.lexists(root / value["path"])
                and _path_identity(root / value["path"]) == value["identity"]
            )
            for key, value in journal["quarantine"].items()
        )
        if complete_originals:
            for key, value in journal["quarantine"].items():
                if value is None or os.path.lexists(active[key]):
                    continue
                _checkpoint(crash_checkpoint, f"recovery:before_restore_original:{key}")
                _move_verified(root / value["path"], active[key], value["identity"])
                _checkpoint(crash_checkpoint, f"recovery:after_restore_original:{key}")
        else:
            for key, path in active.items():
                if not os.path.lexists(path):
                    continue
                failed_path = failed_paths[key]
                if os.path.lexists(failed_path):
                    _unlink_existing(failed_path)
                identity = _path_identity(path)
                _checkpoint(crash_checkpoint, f"recovery:before_quarantine_original:{key}")
                _move_verified(path, failed_path, identity)
                _checkpoint(crash_checkpoint, f"recovery:after_quarantine_original:{key}")
            backup = root / journal["backup"]["path"]
            if not os.path.lexists(restoring):
                _checkpoint(crash_checkpoint, "recovery:before_copy_backup")
                _copy_verified(
                    backup, restoring, journal["backup"]["sha256"],
                    crash_checkpoint,
                )
                _checkpoint(crash_checkpoint, "recovery:after_copy_backup")
            else:
                try:
                    restoring_valid = (
                        _stable_sha256(restoring) == journal["backup"]["sha256"]
                    )
                except DatabaseResetError as exception:
                    if exception.code == "reset_path_unsafe":
                        raise
                    restoring_valid = False
                if not restoring_valid:
                    _unlink_existing(restoring)
                    _checkpoint(crash_checkpoint, "recovery:before_copy_backup")
                    _copy_verified(
                        backup, restoring, journal["backup"]["sha256"],
                        crash_checkpoint,
                    )
                    _checkpoint(crash_checkpoint, "recovery:after_copy_backup")
            if not os.path.lexists(database):
                _checkpoint(crash_checkpoint, "recovery:before_install_backup")
                _move_verified(restoring, database, _path_identity(restoring))
                _checkpoint(crash_checkpoint, "recovery:after_install_backup")

    _journal_update(
        state, next_path, journal, journal["stage"], crash_checkpoint,
        "recovery:journal:restore_validate",
    )
    restored = _open_reset_source(database)
    try:
        label, digest = _validate_source(restored)
    finally:
        restored.rollback()
        restored.close()
    if label != journal["source_schema_label"] or digest != journal["logical_digest"]:
        raise _error("reset_recovery_invalid")
    _journal_update(
        state, next_path, journal, journal["stage"], crash_checkpoint,
        "recovery:journal:restore_audit",
    )
    _checkpoint(crash_checkpoint, "recovery:before_audit")
    _install_recovery_audit(
        root, journal, "restored_source", label, crash_checkpoint
    )
    _checkpoint(crash_checkpoint, "recovery:after_audit")
    _journal_update(
        state, next_path, journal, journal["stage"], crash_checkpoint,
        "recovery:journal:restore_cleanup",
    )
    for value in journal["quarantine"].values():
        if value is not None and os.path.lexists(root / value["path"]):
            _checkpoint(crash_checkpoint, "recovery:before_cleanup_quarantine")
            _unlink_verified(root / value["path"], value["identity"])
            _checkpoint(crash_checkpoint, "recovery:after_cleanup_quarantine")
    for key, failed_path in failed_paths.items():
        if os.path.lexists(failed_path):
            _checkpoint(crash_checkpoint, f"recovery:before_cleanup_failed:{key}")
            _unlink_existing(failed_path)
            _checkpoint(crash_checkpoint, f"recovery:after_cleanup_failed:{key}")
    if os.path.lexists(restoring):
        _checkpoint(crash_checkpoint, "recovery:before_cleanup_restoring")
        _unlink_existing(restoring)
        _checkpoint(crash_checkpoint, "recovery:after_cleanup_restoring")
    _unlink_existing(state)
    _unlink_existing(next_path)


def recover_database_reset(
    database_path: Path | str, *, expected_plan_token: str | None,
    action: str | None, confirm_reset_recovery: bool,
    repository_root: Path | str = PROJECT_ROOT,
    crash_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not confirm_reset_recovery or not expected_plan_token or action not in {"restore-source", "complete-fresh"}:
        raise _error("reset_confirmation_required")
    if not isinstance(expected_plan_token, str) or re.fullmatch(r"[0-9a-f]{64}", expected_plan_token) is None:
        raise _error("reset_recovery_invalid")
    root = Path(repository_root).resolve(strict=True)
    _safe_directory(root)
    _safe_directory(root / "data")
    database = root / "data" / "helios.db"
    initial_head, _ = _git_safety(
        root, _prospective_paths("20000101T000000000Z", expected_plan_token)
    )
    try:
        lease = acquire_database_lease(database, shared=False, allow_recovery_state=True)
    except MaintenanceLockError as exception:
        raise _error("database_maintenance_in_progress") from exception
    try:
        _lock_path, state, next_path = coordination_paths(database)
        if not os.path.lexists(state) and not os.path.lexists(next_path):
            raise _error("reset_recovery_invalid")
        try:
            head, _ = _git_safety(
                root, _prospective_paths("20000101T000000000Z", expected_plan_token)
            )
            if head != initial_head:
                raise _error("reset_recovery_invalid")
            state_exists = os.path.lexists(state)
            journal_path = state if state_exists else next_path
            journal = _validate_journal(
                _read_control_json(journal_path), expected_token=expected_plan_token,
                implementation_commit=head,
            )
            if not state_exists and (
                journal["journal_sequence"] != 1
                or journal["stage"] != "ready_to_quarantine"
            ):
                raise _error("reset_recovery_invalid")
            reviewed_stamp = _reviewed_output_paths(
                journal["backup"]["path"], journal["audit_path"]
            )
            exact_head, _ = _git_safety(
                root, _prospective_paths(reviewed_stamp, expected_plan_token)
            )
            if exact_head != head:
                raise _error("reset_recovery_invalid")
            if not state_exists:
                _move_verified(next_path, state, _path_identity(next_path))
            if os.path.lexists(next_path):
                try:
                    next_value = _validate_journal(
                        _read_control_json(next_path),
                        expected_token=expected_plan_token,
                        implementation_commit=head,
                    )
                except DatabaseResetError:
                    _unlink_existing(next_path)
                else:
                    if next_value["journal_sequence"] != journal["journal_sequence"] + 1:
                        raise _error("reset_recovery_invalid")
                    _replace_verified(
                        next_path, state, _path_identity(next_path),
                        _path_identity(state),
                    )
                    journal = next_value
            backup = root / journal["backup"]["path"]
            if (
                _path_identity(backup) != journal["backup"]["identity"]
                or _stable_sha256(backup) != journal["backup"]["sha256"]
            ):
                raise _error("reset_recovery_invalid")
            audit_path = root / journal["audit_path"]
            existing_audit = (
                _validate_audit(_read_control_json(audit_path), journal)
                if os.path.lexists(audit_path)
                else None
            )
        except DatabaseResetError as exception:
            if exception.code == "reset_git_safety_invalid":
                raise
            raise _error("reset_recovery_invalid") from exception
        except Exception as exception:
            raise _error("reset_recovery_invalid") from exception
        if action == "restore-source":
            if existing_audit is not None and existing_audit["outcome"] != "restored_source":
                raise _error("reset_recovery_invalid")
            _restore_from_journal(
                root, journal, state, next_path, crash_checkpoint
            )
            return {
                "audit_path": journal["audit_path"], "database_path": journal["database_path"],
                "implementation_commit": journal["implementation_commit"], "old_schema_label": journal["source_schema_label"],
                "plan_token": journal["plan_token"], "recovery_commit": journal["recovery_commit"],
                "reset_protocol_version": RESET_PROTOCOL_VERSION, "schema_label": journal["source_schema_label"],
                "status": "restored_source",
            }
        if journal.get("stage") not in {
            "fresh_validated", "cleaning_quarantine", "finalizing",
        }:
            raise _error("reset_recovery_invalid")
        if existing_audit is not None and existing_audit["outcome"] != "reset":
            raise _error("reset_recovery_invalid")
        _journal_update(
            state, next_path, journal, journal["stage"], crash_checkpoint,
            "recovery:journal:fresh_validate",
        )
        fresh = _open_direct(database, read_only=True)
        try:
            validate_v14_foundation(fresh); validate_database_integrity(fresh)
        finally:
            fresh.rollback(); fresh.close()
        _journal_update(
            state, next_path, journal, journal["stage"], crash_checkpoint,
            "recovery:journal:fresh_cleanup",
        )
        for value in journal["quarantine"].values():
            if value is not None and os.path.lexists(root / value["path"]):
                _unlink_verified(root / value["path"], value["identity"])
        _journal_update(
            state, next_path, journal, journal["stage"], crash_checkpoint,
            "recovery:journal:fresh_audit",
        )
        _install_recovery_audit(
            root, journal, "reset", "1.4", crash_checkpoint
        )
        _unlink_existing(state)
        _unlink_existing(next_path)
        return _reset_report(journal)
    except DatabaseResetError:
        raise
    except (SchemaValidationError, sqlite3.Error, OSError, TypeError, ValueError) as exception:
        raise _error("reset_recovery_invalid") from exception
    finally:
        lease.close()


# Revision 2.4 replaced only the reset-control protocol.  Keeping the reviewed
# Revision 2.3 helpers above provides the closed legacy journal validator and
# the already-audited SQLite backup/reset primitives, while every public reset
# surface is routed through the Windows-only immutable implementation.
from .reset_protocol_v2 import (  # noqa: E402
    IGNORE_BLOCK as IGNORE_BLOCK,
    execute_database_reset as execute_database_reset,
    plan_database_reset as plan_database_reset,
    recover_database_reset as recover_database_reset,
)

RESET_PROTOCOL_VERSION = "room_shared_reset_v2"
