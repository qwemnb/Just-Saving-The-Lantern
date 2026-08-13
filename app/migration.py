"""Explicit transactional migration from Helios Room schema v1.2 to v1.3."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .database import (
    EXPECTED_SCHEMA_MIGRATIONS,
    PROJECT_ROOT,
    connect_database,
)
from .identity_service import ParticipantNameError, validate_display_alias
from .schema_validation import (
    SchemaValidationError,
    validate_database_integrity,
    validate_v12_source,
    validate_v13_foundation,
)


MIGRATION_PATH = PROJECT_ROOT / "schema" / "migrations" / "helios_room_v1_2_to_v1_3.sql"


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


def _history(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    try:
        rows = connection.execute(
            "SELECT migration_no, schema_label FROM schema_migrations ORDER BY migration_no"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise DatabaseMigrationError(
            "migration_state_invalid", "The database migration state is invalid."
        ) from error
    return [(row[0], row[1]) for row in rows]


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise DatabaseMigrationError(
            "migration_script_invalid", "The database migration script is invalid."
        )


def _one(connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row:
    rows = connection.execute(query, parameters).fetchall()
    if len(rows) != 1:
        raise DatabaseMigrationError(
            "migration_data_invalid", "The v1.2 database data is not migration-compatible."
        )
    return rows[0]


def _bootstrap_alias(connection: sqlite3.Connection, participant_id: int, display: str) -> int:
    try:
        accepted_display, key = validate_display_alias(display)
    except ParticipantNameError as error:
        raise DatabaseMigrationError(
            "migration_alias_invalid", "A participant name cannot be migrated safely."
        ) from error
    if accepted_display != display:
        raise DatabaseMigrationError(
            "migration_alias_invalid", "A participant name cannot be migrated safely."
        )
    try:
        cursor = connection.execute(
            "INSERT INTO participant_aliases (participant_id, display_alias, alias_key) VALUES (?, ?, ?)",
            (participant_id, accepted_display, key),
        )
    except sqlite3.IntegrityError as error:
        raise DatabaseMigrationError(
            "migration_alias_collision", "Participant aliases collide during migration."
        ) from error
    alias_id = cursor.lastrowid
    connection.execute(
        "INSERT INTO participant_primary_aliases (participant_id, alias_id) VALUES (?, ?)",
        (participant_id, alias_id),
    )
    connection.execute(
        """INSERT INTO participant_name_events
           (event_type, room_id, actor_participant_id, subject_participant_id,
            previous_alias_id, new_alias_id, canonical_message_id)
           VALUES ('bootstrap', NULL, ?, ?, NULL, ?, NULL)""",
        (participant_id, participant_id, alias_id),
    )
    return alias_id


def migrate_database(database_path: Path | str) -> MigrationReport:
    """Migrate only an exact v1.2 database, entirely under BEGIN IMMEDIATE."""

    try:
        connection = connect_database(database_path)
    except (OSError, sqlite3.Error) as error:
        raise DatabaseMigrationError(
            "migration_unavailable", "The database could not be opened for migration."
        ) from error
    try:
        connection.execute("BEGIN IMMEDIATE")
        history = _history(connection)
        if history == list(EXPECTED_SCHEMA_MIGRATIONS):
            validate_v13_foundation(connection)
            validate_database_integrity(connection)
            connection.rollback()
            return MigrationReport("already_current", "1.3", 0, "ok", 0)
        if history != [EXPECTED_SCHEMA_MIGRATIONS[0]]:
            raise DatabaseMigrationError(
                "migration_state_invalid", "Only an exact schema v1.2 database can be migrated."
            )
        # This is the same complete, pure source validator used by startup
        # classification.  The migration owns BEGIN IMMEDIATE and has not made
        # its first write yet.
        validate_v12_source(connection)
        room = _one(
            connection, "SELECT id, name FROM rooms WHERE room_key='main'"
        )
        if room["name"] != "The Room":
            raise DatabaseMigrationError(
                "migration_data_invalid", "The main room is not migration-compatible."
            )
        peter = _one(
            connection,
            "SELECT id, name, participant_type FROM participants WHERE participant_key='peter'",
        )
        helios = _one(
            connection,
            "SELECT id, name, participant_type FROM participants WHERE participant_key='helios'",
        )
        if (
            peter["name"] != "Peter"
            or peter["participant_type"] != "human"
            or helios["name"] != "Helios"
            or helios["participant_type"] != "ai"
        ):
            raise DatabaseMigrationError(
                "migration_data_invalid", "Required participants are not migration-compatible."
            )
        if connection.execute(
            "SELECT count(*) FROM participants WHERE participant_key='room-system'"
        ).fetchone()[0]:
            raise DatabaseMigrationError(
                "migration_room_system_conflict", "A room-system participant already exists."
            )
        messages = connection.execute(
            """
            SELECT m.id, m.room_id, m.participant_id, m.message_type, m.turn_id,
                   p.participant_key,
                   t.initiated_by_participant_id
            FROM messages AS m
            JOIN participants AS p ON p.id=m.participant_id
            LEFT JOIN turns AS t ON t.id=m.turn_id
            ORDER BY m.id
            """
        ).fetchall()
        for message in messages:
            if message["room_id"] != room["id"]:
                raise DatabaseMigrationError(
                    "migration_message_shape_unsupported",
                    "A canonical message has an unsupported migration shape.",
                )
            if message["message_type"] == "chat" and message["participant_key"] == "peter":
                continue
            if (
                message["message_type"] == "chat"
                and message["participant_key"] == "helios"
                and message["initiated_by_participant_id"] == peter["id"]
            ):
                continue
            if message["message_type"] == "system":
                continue
            raise DatabaseMigrationError(
                "migration_message_shape_unsupported",
                "A canonical message has an unsupported migration shape.",
            )

        _execute_script(connection, MIGRATION_PATH.read_text(encoding="utf-8"))
        alias_ids: dict[int, int] = {}
        for participant in connection.execute(
            "SELECT id, name FROM participants ORDER BY id"
        ).fetchall():
            alias_ids[participant["id"]] = _bootstrap_alias(
                connection, participant["id"], participant["name"]
            )
        cursor = connection.execute(
            "INSERT INTO participants (participant_key, name, participant_type) VALUES ('room-system','Room','system')"
        )
        room_system_id = cursor.lastrowid
        room_alias_id = connection.execute(
            "INSERT INTO participant_aliases (participant_id, display_alias, alias_key) VALUES (?, 'Room', 'room')",
            (room_system_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO participant_primary_aliases (participant_id, alias_id) VALUES (?, ?)",
            (room_system_id, room_alias_id),
        )
        connection.execute(
            """INSERT INTO participant_name_events
               (event_type, room_id, actor_participant_id, subject_participant_id,
                previous_alias_id, new_alias_id, canonical_message_id)
               VALUES ('bootstrap',NULL,?,?,NULL,?,NULL)""",
            (room_system_id, room_system_id, room_alias_id),
        )
        for message in messages:
            sender_alias_id = alias_ids[message["participant_id"]]
            if message["message_type"] == "system":
                kind, recipient_id, destination_alias_id = "room", None, room_alias_id
            elif message["participant_key"] == "peter":
                kind, recipient_id, destination_alias_id = (
                    "participant", helios["id"], alias_ids[helios["id"]]
                )
            else:
                kind, recipient_id, destination_alias_id = (
                    "participant", peter["id"], alias_ids[peter["id"]]
                )
            connection.execute(
                """INSERT INTO message_routes
                   (message_id,room_id,sender_participant_id,sender_alias_id,
                    destination_kind,recipient_participant_id,destination_alias_id,routing_mode)
                   VALUES (?,?,?,?,?,?,?,'legacy_implicit')""",
                (
                    message["id"], message["room_id"], message["participant_id"],
                    sender_alias_id, kind, recipient_id, destination_alias_id,
                ),
            )
        connection.execute(
            """INSERT INTO schema_migrations (migration_no,schema_label,description)
               VALUES (2,'1.3','Participant identity, immutable aliases and name history, room-system identity, and immutable message routing.')"""
        )
        validate_v13_foundation(connection)
        validate_database_integrity(connection)
        connection.commit()
        return MigrationReport("migrated", "1.3", len(messages), "ok", 0)
    except SchemaValidationError as error:
        if connection.in_transaction:
            connection.rollback()
        raise DatabaseMigrationError(
            "migration_data_invalid",
            "The database is not compatible with the approved migration.",
        ) from error
    except DatabaseMigrationError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise DatabaseMigrationError(
            "migration_unavailable", "The database could not be migrated safely."
        ) from error
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
