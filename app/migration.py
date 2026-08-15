"""Closed schema-v1.4 migration dispatch.

Schema 1.2 and 1.3 are eligible only for the separately authorized guarded
reset protocol.  This ordinary command never upgrades either database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .database import connect_database
from .maintenance_lock import ResetRecoveryRequiredError
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


class DatabaseMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class MigrationReport:
    status: str
    schema_label: str
    migrated_message_count: int
    integrity_check: str
    foreign_key_violations: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def migrate_database(database_path: Path | str) -> MigrationReport:
    """Validate v1.4 or reject an old exact database without writing it."""

    try:
        connection = connect_database(database_path)
    except ResetRecoveryRequiredError as error:
        raise DatabaseMigrationError(error.code, error.message) from None
    except (OSError, sqlite3.Error) as error:
        raise DatabaseMigrationError(
            "migration_state_invalid", "The database migration state is invalid."
        ) from error
    try:
        connection.execute("BEGIN IMMEDIATE")
        history = schema_history(connection)
        if history == V14_HISTORY:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
            connection.rollback()
            return MigrationReport("already_current", "1.4", 0, "ok", 0)
        if history != V12_HISTORY and history != V13_HISTORY:
            raise DatabaseMigrationError(
                "migration_state_invalid", "The database migration state is invalid."
            )
        validate_legacy_reset_dispatch(connection, history)
        raise DatabaseMigrationError(
            "database_reset_required",
            "The existing Helios Room database must be retired and reinitialized before this version can run.",
        )
    except DatabaseMigrationError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (SchemaValidationError, sqlite3.Error, TypeError, ValueError) as error:
        if connection.in_transaction:
            connection.rollback()
        raise DatabaseMigrationError(
            "migration_state_invalid", "The database migration state is invalid."
        ) from error
    finally:
        connection.close()
