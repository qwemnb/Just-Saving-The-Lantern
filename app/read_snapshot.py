"""Sidecar-aware read-only SQLite snapshot execution.

The resolved file identity stored in :class:`FileFingerprint` is the canonical
resolved path plus the operating system's device/inode identity when those
values are available.  Size, nanosecond modification time, and SHA-256 are
always included.  This lets a clean sidecar-free immutable read be accepted
only after the same main-file snapshot is observed again after close.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, TypeVar


BUSY_TIMEOUT_MS = 5_000
T = TypeVar("T")


class ReadSnapshotError(RuntimeError):
    """An unsafe or unavailable read-only SQLite snapshot."""


class ReadSnapshotMissingError(ReadSnapshotError):
    """The selected main database file does not exist."""


@dataclass(frozen=True)
class FileFingerprint:
    canonical_path: str
    device: int | None
    inode: int | None
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class ReadSnapshotResult(Generic[T]):
    value: T
    branch: str
    wal_existed: bool
    shm_existed: bool


def _canonical_file(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ReadSnapshotMissingError("database file is missing") from error
    if not resolved.is_file():
        raise ReadSnapshotMissingError("database file is missing")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file(path: Path) -> FileFingerprint:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    device = getattr(stat, "st_dev", None)
    inode = getattr(stat, "st_ino", None)
    if not device:
        device = None
    if not inode:
        inode = None
    return FileFingerprint(
        canonical_path=str(resolved),
        device=device,
        inode=inode,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=_sha256(resolved),
    )


def _has_wal_header(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(100)
    return (
        len(header) >= 100
        and header[:16] == b"SQLite format 3\x00"
        and header[18:20] == b"\x02\x02"
    )


def _open_connection(path: Path, immutable: bool) -> sqlite3.Connection:
    query = (
        "mode=ro&immutable=1&cache=private"
        if immutable
        else "mode=ro&cache=private"
    )
    connection = sqlite3.connect(
        f"{path.as_uri()}?{query}",
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    if (
        connection.execute("PRAGMA query_only").fetchone()[0] != 1
        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
    ):
        connection.close()
        raise ReadSnapshotError("query-only mode could not be enforced")
    connection.execute("BEGIN")
    return connection


def run_read_snapshot(
    database_path: Path | str,
    operation: Callable[[sqlite3.Connection], T],
    *,
    allow_wal_retry: bool = True,
) -> ReadSnapshotResult[T]:
    """Run ``operation`` in one read transaction under the four-state matrix.

    Runtime read surfaces may allow one retry when a legitimate writer creates
    both sidecars during an immutable read. Startup preflight passes
    ``allow_wal_retry=False`` so any such appearance discards the result and
    fails closed instead of accepting a different snapshot.
    """

    return _run_read_snapshot(
        database_path, operation, allow_wal_retry=allow_wal_retry
    )


def _run_read_snapshot(
    database_path: Path | str,
    operation: Callable[[sqlite3.Connection], T],
    *,
    allow_wal_retry: bool,
) -> ReadSnapshotResult[T]:

    path = _canonical_file(database_path)
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    wal_exists = wal_path.exists()
    shm_exists = shm_path.exists()
    if wal_exists != shm_exists:
        raise ReadSnapshotError("unsupported one-sided SQLite sidecar state")

    immutable = not wal_exists
    if immutable and not _has_wal_header(path):
        raise ReadSnapshotError("sidecar-free database is not a clean WAL database")

    try:
        main_before = fingerprint_file(path)
        wal_before = fingerprint_file(wal_path) if wal_exists else None
    except OSError as error:
        raise ReadSnapshotError("database fingerprint could not be captured") from error

    connection: sqlite3.Connection | None = None
    operation_error: BaseException | None = None
    value: T | None = None
    try:
        connection = _open_connection(path, immutable)
        value = operation(connection)
    except BaseException as error:
        operation_error = error
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()

    try:
        final_wal = wal_path.exists()
        final_shm = shm_path.exists()
        if (
            immutable
            and operation_error is None
            and allow_wal_retry
            and final_wal
            and final_shm
        ):
            # The immutable result observed an obsolete main-only snapshot.
            # Discard it and validate the new committed WAL snapshot once.
            return _run_read_snapshot(
                database_path, operation, allow_wal_retry=False
            )
        main_after = fingerprint_file(path)
        if main_after != main_before:
            raise ReadSnapshotError("database changed during read-only validation")
        if immutable:
            if final_wal or final_shm:
                raise ReadSnapshotError("SQLite sidecar appeared during immutable read")
        else:
            if not final_wal or not final_shm:
                raise ReadSnapshotError("SQLite sidecar state changed during read")
            if fingerprint_file(wal_path) != wal_before:
                raise ReadSnapshotError("WAL changed during read-only validation")
    except (OSError, ReadSnapshotError) as error:
        raise ReadSnapshotError("read-only snapshot post-check failed") from error

    if operation_error is not None:
        raise operation_error
    return ReadSnapshotResult(
        value=value,  # type: ignore[arg-type]
        branch="immutable" if immutable else "wal",
        wal_existed=wal_exists,
        shm_existed=shm_exists,
    )
