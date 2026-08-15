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
    V14_HISTORY,
    schema_history,
    validate_database_integrity,
    validate_legacy_reset_dispatch,
    validate_v14_foundation,
)
from .maintenance_lock import MaintenanceLockError, ResetRecoveryRequiredError


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


def _reset_required() -> DatabasePreflightError:
    return DatabasePreflightError(
        "database_reset_required",
        "The existing Helios Room database must be retired and reinitialized before this version can run.",
    )


def _incompatible() -> DatabasePreflightError:
    return DatabasePreflightError(
        "database_schema_incompatible",
        "The Helios Room database schema is incompatible with this application.",
    )


def preflight_database(database_path: Path | str) -> DatabasePreflightReport:
    """Accept only one complete, unchanged schema-v1.4 read snapshot."""

    def validate(connection: sqlite3.Connection) -> str:
        history = schema_history(connection)
        if history == V12_HISTORY or history == V13_HISTORY:
            validate_legacy_reset_dispatch(connection, history)
            raise _MigrationRequired()
        if history != V14_HISTORY:
            raise SchemaValidationError("unsupported schema history")
        validate_v14_foundation(connection)
        validate_database_integrity(connection)
        return "1.4"

    try:
        result = run_read_snapshot(
            database_path,
            validate,
            allow_wal_retry=False,
        )
    except ResetRecoveryRequiredError as error:
        raise DatabasePreflightError(error.code, error.message) from None
    except MaintenanceLockError as error:
        raise DatabasePreflightError(
            "database_maintenance_in_progress",
            "The Helios Room database is unavailable during maintenance.",
        ) from error
    except ReadSnapshotMissingError as error:
        raise _missing() from error
    except _MigrationRequired as error:
        raise _reset_required() from error
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
