"""Sanitized schema-readiness preflight for the ``serve`` command."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .read_snapshot import (
    ReadSnapshotError,
    ReadSnapshotMissingError,
    run_read_snapshot,
)
from .schema_validation import (
    SchemaValidationError,
    V12_HISTORY,
    V13_HISTORY,
    schema_history,
    validate_database_integrity,
    validate_v12_source,
    validate_v13_foundation,
)


@dataclass
class DatabasePreflightError(RuntimeError):
    code: str
    message: str

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class DatabasePreflightReport:
    schema_label: str
    snapshot_branch: str


class _MigrationRequired(RuntimeError):
    pass


def _missing() -> DatabasePreflightError:
    return DatabasePreflightError(
        "database_not_initialized",
        "The Helios Room database is not initialized.",
    )


def _migration_required() -> DatabasePreflightError:
    return DatabasePreflightError(
        "database_migration_required",
        "Database schema 1.2 must be migrated to 1.3 before the server can start.",
    )


def _incompatible() -> DatabasePreflightError:
    return DatabasePreflightError(
        "database_schema_incompatible",
        "The Helios Room database schema is incompatible with this application.",
    )


def preflight_database(database_path: Path | str) -> DatabasePreflightReport:
    """Accept only one complete, unchanged schema-v1.3 read snapshot."""

    def validate(connection: sqlite3.Connection) -> str:
        history = schema_history(connection)
        if history == V12_HISTORY:
            validate_v12_source(connection)
            raise _MigrationRequired()
        if history != V13_HISTORY:
            raise SchemaValidationError("unsupported schema history")
        validate_v13_foundation(connection)
        validate_database_integrity(connection)
        return "1.3"

    try:
        result = run_read_snapshot(
            database_path,
            validate,
            allow_wal_retry=False,
        )
    except ReadSnapshotMissingError as error:
        raise _missing() from error
    except _MigrationRequired as error:
        raise _migration_required() from error
    except (
        OSError,
        sqlite3.Error,
        SchemaValidationError,
        ReadSnapshotError,
        TypeError,
        ValueError,
    ) as error:
        raise _incompatible() from error
    return DatabasePreflightReport(
        schema_label=result.value,
        snapshot_branch=result.branch,
    )
