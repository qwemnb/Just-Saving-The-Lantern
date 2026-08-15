"""Participant identity, routing, directory, and Room-post services."""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from .commands import classify_local_command, is_reserved_local_command
from .database import (
    MessageVisibilityGuardError,
    connect_database,
    create_turn,
    is_blank_message,
    store_message,
)
from .read_snapshot import ReadSnapshotError, run_read_snapshot
from .maintenance_lock import MaintenanceLockError, ResetRecoveryRequiredError
from .participant_registry import is_addressable_ai
from .schema_validation import (
    SchemaValidationError,
    VisibilityPolicyValidationError,
    validate_v14_foundation,
)


RESERVED_ALIAS_KEYS = frozenset({"all", "everyone", "system", "participants"})


class IdentityServiceError(RuntimeError):
    """A sanitized identity, directory, history, or Room-post failure."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class ParticipantNameError(RuntimeError):
    """A stable internal name-adoption rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def alias_key(display_alias: str) -> str:
    return unicodedata.normalize("NFKC", display_alias).casefold()


def validate_display_alias(requested_alias: str) -> tuple[str, str]:
    """Return the preserved trimmed display alias and its comparison key."""

    if not isinstance(requested_alias, str):
        raise ParticipantNameError("participant_alias_invalid", "The participant alias is invalid.")
    display_alias = requested_alias.strip()
    if not 1 <= len(display_alias) <= 64:
        raise ParticipantNameError("participant_alias_invalid", "The participant alias is invalid.")
    if display_alias[0] == "/" or "[" in display_alias or "]" in display_alias:
        raise ParticipantNameError("participant_alias_invalid", "The participant alias is invalid.")
    if any(unicodedata.category(character).startswith("C") for character in display_alias):
        raise ParticipantNameError("participant_alias_invalid", "The participant alias is invalid.")
    normalized = alias_key(display_alias)
    if normalized in RESERVED_ALIAS_KEYS or normalized == "room":
        raise ParticipantNameError("participant_alias_invalid", "The participant alias is invalid.")
    return display_alias, normalized


def _validate_stored_alias(display_alias: Any, stored_key: Any, *, allow_room: bool = False) -> None:
    if allow_room and display_alias == "Room" and stored_key == "room":
        return
    try:
        accepted, normalized = validate_display_alias(display_alias)
    except ParticipantNameError as error:
        raise ValueError("stored alias is invalid") from error
    if accepted != display_alias or normalized != stored_key:
        raise ValueError("stored alias is invalid")


def current_alias(connection: sqlite3.Connection, participant_id: int) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT pa.id, pa.display_alias, pa.alias_key
        FROM participant_primary_aliases AS ppa
        JOIN participant_aliases AS pa ON pa.id = ppa.alias_id
        WHERE ppa.participant_id = ? AND pa.participant_id = ?
        """,
        (participant_id, participant_id),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("invalid primary alias projection")
    return rows[0]


def resolve_room_context(connection: sqlite3.Connection) -> dict[str, Any]:
    room_rows = connection.execute(
        "SELECT id, name FROM rooms WHERE room_key = 'main'"
    ).fetchall()
    if len(room_rows) != 1 or room_rows[0]["name"] != "The Room":
        raise RuntimeError("invalid main room")
    room_id = room_rows[0]["id"]
    participants: dict[str, sqlite3.Row] = {}
    for key in ("peter", "helios", "room-system"):
        rows = connection.execute(
            """SELECT id, participant_key, participant_type, name
               FROM participants WHERE participant_key = ?""",
            (key,),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError("required participant missing")
        participants[key] = rows[0]
    peter = participants["peter"]
    helios = participants["helios"]
    system = participants["room-system"]
    if (
        peter["participant_type"] != "human"
        or peter["name"] != "Peter"
        or helios["participant_type"] != "ai"
        or helios["name"] != "Helios"
    ):
        raise RuntimeError("required participant type invalid")
    if system["participant_type"] != "system" or system["name"] != "Room":
        raise RuntimeError("room system invalid")
    for participant in (peter, helios):
        count = connection.execute(
            """SELECT count(*) FROM room_participants
               WHERE room_id = ? AND participant_id = ? AND left_at IS NULL""",
            (room_id, participant["id"]),
        ).fetchone()[0]
        if count != 1:
            raise RuntimeError("active membership invalid")
    if connection.execute(
        """SELECT count(*) FROM room_participants
           WHERE participant_id = ? AND left_at IS NULL""",
        (system["id"],),
    ).fetchone()[0] != 0:
        raise RuntimeError("room system must not have membership")
    return {
        "room_id": room_id,
        "peter": peter,
        "helios": helios,
        "room_system": system,
        "peter_alias": current_alias(connection, peter["id"]),
        "helios_alias": current_alias(connection, helios["id"]),
        "room_alias": current_alias(connection, system["id"]),
    }


def resolve_room_post_context(connection: sqlite3.Connection) -> dict[str, Any]:
    room = connection.execute(
        "SELECT id, name FROM rooms WHERE room_key='main'"
    ).fetchall()
    if len(room) != 1 or room[0]["name"] != "The Room":
        raise RuntimeError("invalid main room")
    participants: dict[str, sqlite3.Row] = {}
    for key, expected_type in (("peter", "human"), ("room-system", "system")):
        rows = connection.execute(
            """SELECT id, participant_key, participant_type, name
               FROM participants WHERE participant_key=?""",
            (key,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["participant_type"] != expected_type:
            raise RuntimeError("invalid Room post identity")
        participants[key] = rows[0]
    if participants["room-system"]["name"] != "Room":
        raise RuntimeError("invalid Room system identity")
    if participants["peter"]["name"] != "Peter":
        raise RuntimeError("invalid Peter identity")
    if connection.execute(
        """SELECT count(*) FROM room_participants
           WHERE room_id=? AND participant_id=? AND left_at IS NULL""",
        (room[0]["id"], participants["peter"]["id"]),
    ).fetchone()[0] != 1:
        raise RuntimeError("Peter is not an active room member")
    if connection.execute(
        """SELECT count(*) FROM room_participants
           WHERE participant_id=? AND left_at IS NULL""",
        (participants["room-system"]["id"],),
    ).fetchone()[0] != 0:
        raise RuntimeError("Room system must not have membership")
    return {
        "room_id": room[0]["id"],
        "peter": participants["peter"],
        "peter_alias": current_alias(connection, participants["peter"]["id"]),
        "room_alias": current_alias(connection, participants["room-system"]["id"]),
    }


def adopt_participant_name(
    connection: sqlite3.Connection,
    room_id: int,
    actor_id: int,
    subject_id: int,
    requested_alias: str,
) -> dict[str, Any]:
    """Adopt a self-name inside a caller-owned immediate transaction."""

    if not connection.in_transaction:
        raise RuntimeError("name adoption requires an active transaction")
    display_alias, normalized = validate_display_alias(requested_alias)
    if actor_id != subject_id:
        raise ParticipantNameError(
            "participant_name_self_only", "Participants may adopt only their own names."
        )
    if connection.execute(
        """SELECT count(*) FROM room_participants
           WHERE room_id = ? AND participant_id = ? AND left_at IS NULL""",
        (room_id, subject_id),
    ).fetchone()[0] != 1:
        raise ParticipantNameError(
            "participant_not_active", "The participant is not active in the room."
        )
    old_alias = current_alias(connection, subject_id)
    if old_alias["alias_key"] == normalized:
        return {
            "status": "unchanged",
            "participant_id": subject_id,
            "alias_id": old_alias["id"],
            "display_alias": old_alias["display_alias"],
        }
    collision = connection.execute(
        "SELECT id, participant_id FROM participant_aliases WHERE alias_key = ?",
        (normalized,),
    ).fetchone()
    if collision is not None and collision["participant_id"] != subject_id:
        raise ParticipantNameError(
            "participant_alias_conflict", "The participant alias is unavailable."
        )
    system_rows = connection.execute(
        """SELECT id FROM participants
           WHERE participant_key = 'room-system' AND participant_type = 'system'"""
    ).fetchall()
    if len(system_rows) != 1:
        raise RuntimeError("room system identity invalid")
    system_id = system_rows[0]["id"]
    system_alias = current_alias(connection, system_id)
    if system_alias["alias_key"] != "room":
        raise RuntimeError("room alias invalid")

    if collision is not None:
        display_alias = connection.execute(
            "SELECT display_alias FROM participant_aliases WHERE id = ?",
            (collision["id"],),
        ).fetchone()[0]

    # Publish while the mature pre-adoption foundation is still exact.  The
    # alias/event graph is then advanced atomically around this message ID.
    message_id = store_message(
        connection,
        room_id=room_id,
        participant_id=system_id,
        message_text=f"{old_alias['display_alias']} adopted the name {display_alias}.",
        message_type="system",
        sender_alias_id=system_alias["id"],
        destination_kind="room",
        destination_alias_id=system_alias["id"],
        recipient_participant_id=None,
    )
    if collision is None:
        cursor = connection.execute(
            """INSERT INTO participant_aliases
               (participant_id, display_alias, alias_key) VALUES (?, ?, ?)""",
            (subject_id, display_alias, normalized),
        )
        new_alias_id = cursor.lastrowid
    else:
        new_alias_id = collision["id"]
    connection.execute(
        "UPDATE participant_primary_aliases SET alias_id = ? WHERE participant_id = ?",
        (new_alias_id, subject_id),
    )
    connection.execute(
        """
        INSERT INTO participant_name_events (
            event_type, room_id, actor_participant_id,
            subject_participant_id, previous_alias_id,
            new_alias_id, canonical_message_id
        ) VALUES ('adopted', ?, ?, ?, ?, ?, ?)
        """,
        (room_id, actor_id, subject_id, old_alias["id"], new_alias_id, message_id),
    )
    return {
        "status": "adopted",
        "participant_id": subject_id,
        "alias_id": new_alias_id,
        "display_alias": display_alias,
        "message_id": message_id,
    }


def _load_participant_directory_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    validate_v14_foundation(connection)
    room_rows = connection.execute(
        "SELECT id, name FROM rooms WHERE room_key = 'main'"
    ).fetchall()
    if len(room_rows) != 1 or room_rows[0]["name"] != "The Room":
        raise ValueError("invalid room")
    room_id = room_rows[0]["id"]
    rows = connection.execute(
        """
            SELECT p.id, p.participant_key, p.participant_type,
                   pa.id AS alias_id, pa.display_alias, pa.alias_key,
                   CASE WHEN ppa.alias_id = pa.id THEN 1 ELSE 0 END AS is_primary
            FROM room_participants AS rp
            JOIN participants AS p ON p.id = rp.participant_id
            JOIN participant_aliases AS pa ON pa.participant_id = p.id
            JOIN participant_primary_aliases AS ppa ON ppa.participant_id = p.id
            WHERE rp.room_id = ? AND rp.left_at IS NULL
            ORDER BY p.id, is_primary DESC, pa.id
        """,
        (room_id,),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        _validate_stored_alias(row["display_alias"], row["alias_key"])
        grouped.setdefault(row["id"], []).append(row)
    destinations: list[dict[str, Any]] = [
        {"kind": "room", "label": "Room", "addressable": True}
    ]
    participants: list[tuple[str, str, dict[str, Any]]] = []
    for participant_id, aliases in grouped.items():
        del participant_id
        primary = [row for row in aliases if row["is_primary"] == 1]
        if len(primary) != 1 or aliases[0] is not primary[0]:
            raise ValueError("invalid primary alias")
        row = primary[0]
        if row["participant_key"] == "room-system":
            raise ValueError("room system cannot be a member")
        addressable = is_addressable_ai(
            row["participant_key"], row["participant_type"]
        )
        entry = {
            "kind": "participant",
            "participant_key": row["participant_key"],
            "primary_name": row["display_alias"],
            "aliases": [alias["display_alias"] for alias in aliases],
            "participant_type": row["participant_type"],
            "addressable": addressable,
        }
        participants.append((row["alias_key"], row["participant_key"], entry))
    if not any(entry[2]["participant_key"] == "peter" for entry in participants):
        raise ValueError("Peter missing")
    participants.sort(key=lambda item: (item[0], item[1]))
    destinations.extend(entry for _, _, entry in participants)
    return {
        "directory_version": 1,
        "room": {"room_key": "main", "name": "The Room"},
        "destinations": destinations,
    }


def load_participant_directory(database_path: Path | str) -> dict[str, Any]:
    try:
        return run_read_snapshot(
            database_path,
            _load_participant_directory_snapshot,
            allow_wal_retry=True,
        ).value
    except ResetRecoveryRequiredError as error:
        raise IdentityServiceError(503, error.code, error.message) from None
    except MaintenanceLockError as error:
        raise IdentityServiceError(
            503, "database_maintenance_in_progress",
            "The Helios Room database is unavailable during maintenance.",
        ) from error
    except (sqlite3.OperationalError, ReadSnapshotError) as error:
        raise IdentityServiceError(
            503,
            "participant_directory_unavailable",
            "The participant directory is unavailable.",
        ) from error
    except (
        sqlite3.DatabaseError,
        SchemaValidationError,
        ValueError,
        TypeError,
    ) as error:
        raise IdentityServiceError(
            500,
            "participant_directory_invalid",
            "The participant directory data is invalid.",
        ) from error


def _load_message_history_snapshot(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
        validate_v14_foundation(connection)
        rows = connection.execute(
            """
            SELECT m.id, m.message_text, m.created_at, m.room_sequence_no,
                   sp.participant_key AS sender_key,
                   mr.routing_mode, mr.destination_kind,
                   sa.display_alias AS sender_display,
                   sa.alias_key AS sender_alias_key,
                   rp.participant_key AS recipient_key,
                   da.display_alias AS destination_display,
                   da.alias_key AS destination_alias_key,
                   da.participant_id AS destination_alias_owner,
                   mr.recipient_participant_id
            FROM messages AS m
            JOIN rooms AS r ON r.id = m.room_id AND r.room_key = 'main'
            JOIN participants AS sp ON sp.id = m.participant_id
            JOIN message_routes AS mr ON mr.message_id = m.id
                AND mr.room_id = m.room_id
                AND mr.sender_participant_id = m.participant_id
            JOIN participant_aliases AS sa ON sa.id = mr.sender_alias_id
                AND sa.participant_id = m.participant_id
            JOIN participant_aliases AS da ON da.id = mr.destination_alias_id
            LEFT JOIN participants AS rp ON rp.id = mr.recipient_participant_id
            ORDER BY m.room_sequence_no
            """
        ).fetchall()
        if connection.execute(
            """SELECT count(*) FROM messages AS m JOIN rooms AS r ON r.id=m.room_id
               WHERE r.room_key='main'"""
        ).fetchone()[0] != len(rows):
            raise ValueError("message route missing")
        result: list[dict[str, Any]] = []
        for row in rows:
            _validate_stored_alias(
                row["sender_display"],
                row["sender_alias_key"],
                allow_room=row["sender_key"] == "room-system",
            )
            if row["routing_mode"] not in {"legacy_implicit", "explicit"}:
                raise ValueError("invalid routing mode")
            if row["destination_kind"] == "participant":
                if (
                    row["recipient_key"] is None
                    or row["destination_alias_owner"] != row["recipient_participant_id"]
                ):
                    raise ValueError("invalid participant route")
                _validate_stored_alias(
                    row["destination_display"], row["destination_alias_key"]
                )
                destination = {
                    "kind": "participant",
                    "participant_key": row["recipient_key"],
                    "display_name": row["destination_display"],
                }
            elif row["destination_kind"] == "room":
                room_owner = connection.execute(
                    "SELECT participant_key FROM participants WHERE id = ?",
                    (row["destination_alias_owner"],),
                ).fetchone()
                if (
                    row["recipient_participant_id"] is not None
                    or room_owner is None
                    or room_owner[0] != "room-system"
                    or row["destination_display"] != "Room"
                    or row["destination_alias_key"] != "room"
                ):
                    raise ValueError("invalid room route")
                destination = {
                    "kind": "room",
                    "display_name": row["destination_display"],
                }
            else:
                raise ValueError("invalid destination kind")
            result.append(
                {
                    "id": row["id"],
                    "message_text": row["message_text"],
                    "created_at": row["created_at"],
                    "participant_key": row["sender_key"],
                    "room_sequence_no": row["room_sequence_no"],
                    "routing": {
                        "routing_mode": row["routing_mode"],
                        "sender": {
                            "participant_key": row["sender_key"],
                            "display_name": row["sender_display"],
                        },
                        "destination": destination,
                    },
                }
            )
        return result


def load_message_history(database_path: Path | str) -> list[dict[str, Any]]:
    try:
        return run_read_snapshot(
            database_path,
            _load_message_history_snapshot,
            allow_wal_retry=True,
        ).value
    except ResetRecoveryRequiredError as error:
        raise IdentityServiceError(503, error.code, error.message) from None
    except MaintenanceLockError as error:
        raise IdentityServiceError(
            503, "database_maintenance_in_progress",
            "The Helios Room database is unavailable during maintenance.",
        ) from error
    except (sqlite3.OperationalError, ReadSnapshotError) as error:
        raise IdentityServiceError(
            503,
            "message_history_unavailable",
            "The message history is unavailable.",
        ) from error
    except (
        sqlite3.DatabaseError,
        SchemaValidationError,
        ValueError,
        TypeError,
    ) as error:
        raise IdentityServiceError(
            500,
            "message_history_invalid",
            "The message history data is invalid.",
        ) from error


def post_room_message(message_text: str, database_path: Path | str) -> dict[str, Any]:
    if is_blank_message(message_text):
        return {"ignored": True, "reason": "empty_message"}
    if is_reserved_local_command(classify_local_command(message_text)):
        raise IdentityServiceError(
            400,
            "local_command_only",
            "Local commands are available only in the browser interface.",
        )
    try:
        connection = connect_database(database_path)
    except ResetRecoveryRequiredError as error:
        raise IdentityServiceError(503, error.code, error.message) from None
    except MaintenanceLockError as error:
        raise IdentityServiceError(
            503, "database_maintenance_in_progress",
            "The Helios Room database is unavailable during maintenance.",
        ) from error
    except Exception as error:
        raise IdentityServiceError(
            500, "room_post_failed", "The Room message could not be saved."
        ) from error
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_v14_foundation(connection)
        except VisibilityPolicyValidationError:
            raise MessageVisibilityGuardError() from None
        context = resolve_room_post_context(connection)
        peter = context["peter"]
        turn_id = create_turn(connection, context["room_id"], peter["id"])
        message_id = store_message(
            connection,
            room_id=context["room_id"],
            participant_id=peter["id"],
            message_text=message_text,
            turn_id=turn_id,
            sender_alias_id=context["peter_alias"]["id"],
            destination_kind="room",
            destination_alias_id=context["room_alias"]["id"],
            recipient_participant_id=None,
        )
        connection.execute(
            """UPDATE turns SET status='completed',
                   completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE id=?""",
            (turn_id,),
        )
        connection.commit()
        return {
            "status": "completed",
            "turn_id": turn_id,
            "message_id": message_id,
            "destination": {"kind": "room"},
        }
    except MessageVisibilityGuardError as error:
        connection.rollback()
        raise IdentityServiceError(
            error.status_code, error.code, error.message
        ) from None
    except IdentityServiceError:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise IdentityServiceError(
            500, "room_post_failed", "The Room message could not be saved."
        ) from error
    finally:
        connection.close()
