"""Transactional Gemini identity installation and Helios welcome publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .database import (
    MessageVisibilityGuardError,
    connect_database,
    create_turn,
    store_message,
)
from .identity_service import current_alias
from .maintenance_lock import ResetRecoveryRequiredError
from .schema_validation import (
    VisibilityPolicyValidationError,
    validate_database_integrity,
    validate_v14_foundation,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WELCOME_SOURCE_PATH = (
    PROJECT_ROOT / "docs" / "Helios Statements" / "helios-room-new-participant-welcome.md"
)
WELCOME_SOURCE_RELATIVE = "docs/Helios Statements/helios-room-new-participant-welcome.md"
WELCOME_GIT_BLOB = "e771597f78f933358985b3c7300606742b08d96b"
WELCOME_MESSAGE_SHA256 = "6ff63e9af7ebf4a42fe200833ab63904e6f20f643cae7a40f13c612c3e9e0421"
WELCOME_CONFIG_LABEL = "new-participant-welcome-v1"


class GeminiIdentityError(RuntimeError):
    """A stable, non-sensitive installer or welcome failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _install_incompatible() -> GeminiIdentityError:
    return GeminiIdentityError(
        "gemini_install_incompatible",
        "The existing database state is not compatible with Gemini installation.",
    )


def _install_unavailable() -> GeminiIdentityError:
    return GeminiIdentityError(
        "gemini_install_unavailable", "Gemini could not be installed safely."
    )


def install_gemini(database_path: Path | str) -> dict[str, str]:
    """Install or verify Gemini under one caller-owned immediate transaction."""

    path = Path(database_path)
    if not path.is_file():
        raise _install_unavailable()
    try:
        connection = connect_database(path)
    except ResetRecoveryRequiredError as exception:
        raise GeminiIdentityError(exception.code, exception.message) from None
    except (OSError, sqlite3.Error) as exception:
        raise _install_unavailable() from exception
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
        except Exception as exception:
            raise _install_incompatible() from exception

        room = connection.execute(
            "SELECT id, name FROM rooms WHERE room_key='main'"
        ).fetchall()
        participants = connection.execute(
            """SELECT id, participant_key, name, participant_type
               FROM participants WHERE participant_key='gemini'"""
        ).fetchall()
        alias_collision = connection.execute(
            "SELECT participant_id FROM participant_aliases WHERE alias_key='gemini'"
        ).fetchall()

        if not participants:
            name_collision = connection.execute(
                "SELECT id FROM participants WHERE name='Gemini' COLLATE NOCASE"
            ).fetchall()
            if len(room) != 1 or room[0]["name"] != "The Room" or alias_collision or name_collision:
                raise _install_incompatible()
            participant_id = connection.execute(
                """INSERT INTO participants (participant_key, name, participant_type)
                   VALUES ('gemini', 'Gemini', 'ai')"""
            ).lastrowid
            alias_id = connection.execute(
                """INSERT INTO participant_aliases
                   (participant_id, display_alias, alias_key)
                   VALUES (?, 'Gemini', 'gemini')""",
                (participant_id,),
            ).lastrowid
            connection.execute(
                """INSERT INTO participant_primary_aliases (participant_id, alias_id)
                   VALUES (?, ?)""",
                (participant_id, alias_id),
            )
            connection.execute(
                """INSERT INTO participant_name_events (
                       event_type, room_id, actor_participant_id,
                       subject_participant_id, previous_alias_id,
                       new_alias_id, canonical_message_id
                   ) VALUES ('bootstrap', NULL, ?, ?, NULL, ?, NULL)""",
                (participant_id, participant_id, alias_id),
            )
            connection.execute(
                "INSERT INTO room_participants (room_id, participant_id) VALUES (?, ?)",
                (room[0]["id"], participant_id),
            )
            status = "installed"
        elif len(participants) == 1:
            participant = participants[0]
            if (
                participant["participant_key"] != "gemini"
                or participant["name"] != "Gemini"
                or participant["participant_type"] != "ai"
                or len(alias_collision) != 1
                or alias_collision[0]["participant_id"] != participant["id"]
                or len(room) != 1
                or room[0]["name"] != "The Room"
            ):
                raise _install_incompatible()
            bootstrap = connection.execute(
                """SELECT pne.event_type, pne.room_id, pne.actor_participant_id,
                          pne.subject_participant_id, pne.previous_alias_id,
                          pne.new_alias_id, pne.canonical_message_id,
                          pa.display_alias, pa.alias_key, pa.participant_id
                   FROM participant_name_events AS pne
                   JOIN participant_aliases AS pa ON pa.id=pne.new_alias_id
                   WHERE pne.subject_participant_id=? AND pne.event_type='bootstrap'""",
                (participant["id"],),
            ).fetchall()
            if len(bootstrap) != 1 or dict(bootstrap[0]) != {
                "event_type": "bootstrap",
                "room_id": None,
                "actor_participant_id": participant["id"],
                "subject_participant_id": participant["id"],
                "previous_alias_id": None,
                "new_alias_id": bootstrap[0]["new_alias_id"],
                "canonical_message_id": None,
                "display_alias": "Gemini",
                "alias_key": "gemini",
                "participant_id": participant["id"],
            }:
                raise _install_incompatible()
            active = connection.execute(
                """SELECT count(*) FROM room_participants
                   WHERE room_id=? AND participant_id=? AND left_at IS NULL""",
                (room[0]["id"], participant["id"]),
            ).fetchone()[0]
            if active not in {0, 1}:
                raise _install_incompatible()
            if active == 0:
                connection.execute(
                    "INSERT INTO room_participants (room_id, participant_id) VALUES (?, ?)",
                    (room[0]["id"], participant["id"]),
                )
            status = "already_installed"
        else:
            raise _install_incompatible()

        try:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
        except Exception as exception:
            raise _install_incompatible() from exception
        connection.commit()
        return {
            "status": status,
            "participant_key": "gemini",
            "schema_label": "1.4",
        }
    except MessageVisibilityGuardError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except GeminiIdentityError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.IntegrityError as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _install_incompatible() from exception
    except ResetRecoveryRequiredError as exception:
        raise GeminiIdentityError(exception.code, exception.message) from None
    except (OSError, sqlite3.Error) as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _install_unavailable() from exception
    except Exception as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _install_incompatible() from exception
    finally:
        connection.close()


def _welcome_source_invalid() -> GeminiIdentityError:
    return GeminiIdentityError(
        "gemini_welcome_source_invalid", "The reviewed Gemini welcome source is invalid."
    )


def _welcome_incompatible() -> GeminiIdentityError:
    return GeminiIdentityError(
        "gemini_welcome_state_incompatible",
        "The Gemini welcome could not be published from the existing room state.",
    )


def _welcome_unavailable() -> GeminiIdentityError:
    return GeminiIdentityError(
        "gemini_welcome_unavailable", "The Gemini welcome could not be published safely."
    )


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def load_reviewed_welcome() -> str:
    """Extract the one reviewed welcome from its repository-fixed source."""

    try:
        raw = WELCOME_SOURCE_PATH.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exception:
        raise _welcome_source_invalid() from exception
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_bytes = normalized.encode("utf-8")
    marker = "## Exact welcome\n\n"
    if (
        normalized.count("## Exact welcome") != 1
        or marker not in normalized
        or _git_blob_sha1(normalized_bytes) != WELCOME_GIT_BLOB
        or normalized.endswith("\n\n")
    ):
        raise _welcome_source_invalid()
    welcome = normalized.split(marker, 1)[1]
    if welcome.endswith("\n"):
        welcome = welcome[:-1]
    if not welcome or hashlib.sha256(welcome.encode("utf-8")).hexdigest() != WELCOME_MESSAGE_SHA256:
        raise _welcome_source_invalid()
    return welcome


def _welcome_settings() -> dict[str, Any]:
    return {
        "message_sha256": WELCOME_MESSAGE_SHA256,
        "publication_version": 1,
        "reviewed_git_blob": WELCOME_GIT_BLOB,
        "source_path": WELCOME_SOURCE_RELATIVE,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def publish_gemini_welcome(database_path: Path | str) -> dict[str, Any]:
    """Publish the fixed Helios welcome without constructing a provider client."""

    welcome = load_reviewed_welcome()
    path = Path(database_path)
    if not path.is_file():
        raise _welcome_unavailable()
    try:
        connection = connect_database(path)
    except (OSError, sqlite3.Error) as exception:
        raise _welcome_unavailable() from exception
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
        except VisibilityPolicyValidationError as exception:
            raise MessageVisibilityGuardError() from exception
        except Exception as exception:
            raise _welcome_incompatible() from exception
        room = connection.execute(
            "SELECT id, name FROM rooms WHERE room_key='main'"
        ).fetchall()
        participants = connection.execute(
            """SELECT id, participant_key, name, participant_type
               FROM participants WHERE participant_key IN ('helios','gemini')"""
        ).fetchall()
        by_key = {row["participant_key"]: row for row in participants}
        helios = by_key.get("helios")
        gemini = by_key.get("gemini")
        if (
            len(room) != 1
            or room[0]["name"] != "The Room"
            or helios is None
            or gemini is None
            or helios["name"] != "Helios"
            or helios["participant_type"] != "ai"
            or gemini["name"] != "Gemini"
            or gemini["participant_type"] != "ai"
        ):
            raise _welcome_incompatible()
        for participant in (helios, gemini):
            if connection.execute(
                """SELECT count(*) FROM room_participants
                   WHERE room_id=? AND participant_id=? AND left_at IS NULL""",
                (room[0]["id"], participant["id"]),
            ).fetchone()[0] != 1:
                raise _welcome_incompatible()

        canonical_settings = _canonical_json(_welcome_settings())
        config_rows = connection.execute(
            """SELECT id, provider, model, config_label, system_instructions,
                      settings_json, tools_json
               FROM participant_configs
               WHERE participant_id=? AND (
                   config_label=? OR
                   (provider='local' AND model IS NULL AND system_instructions IS NULL
                    AND settings_json=? AND tools_json='[]')
               ) ORDER BY id""",
            (helios["id"], WELCOME_CONFIG_LABEL, canonical_settings),
        ).fetchall()
        semantic = [
            row for row in config_rows
            if row["provider"] == "local"
            and row["model"] is None
            and row["config_label"] == WELCOME_CONFIG_LABEL
            and row["system_instructions"] is None
            and row["settings_json"] == canonical_settings
            and row["tools_json"] == "[]"
        ]
        if len(semantic) > 1 or (config_rows and len(semantic) != len(config_rows)):
            raise _welcome_incompatible()
        if semantic:
            config_id = semantic[0]["id"]
        else:
            config_id = None

        candidates = connection.execute(
            """SELECT m.id, m.turn_id, m.room_id, m.participant_id,
                      m.participant_config_id, m.reply_to_id, m.message_type,
                      m.message_text, t.status, t.initiated_by_participant_id,
                      mr.sender_participant_id, mr.sender_alias_id,
                      mr.destination_kind, mr.recipient_participant_id,
                      mr.destination_alias_id, mr.routing_mode
               FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               JOIN message_routes AS mr ON mr.message_id=m.id
               WHERE (? IS NOT NULL AND m.participant_config_id=?) OR m.message_text=?
               ORDER BY m.id""",
            (config_id, config_id, welcome),
        ).fetchall()
        if candidates:
            if config_id is None or len(candidates) != 1:
                raise _welcome_incompatible()
            row = candidates[0]
            message_count = connection.execute(
                "SELECT count(*) FROM messages WHERE turn_id=?", (row["turn_id"],)
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT count(*) FROM api_events WHERE turn_id=?", (row["turn_id"],)
            ).fetchone()[0]
            if (
                row["room_id"] != room[0]["id"]
                or row["participant_id"] != helios["id"]
                or row["participant_config_id"] != config_id
                or row["reply_to_id"] is not None
                or row["message_type"] != "chat"
                or row["message_text"] != welcome
                or row["status"] != "completed"
                or row["initiated_by_participant_id"] != helios["id"]
                or row["sender_participant_id"] != helios["id"]
                or row["destination_kind"] != "participant"
                or row["recipient_participant_id"] != gemini["id"]
                or row["routing_mode"] != "explicit"
                or message_count != 1
                or event_count != 0
            ):
                raise _welcome_incompatible()
            connection.rollback()
            return {
                "status": "already_published",
                "sender": "helios",
                "recipient": "gemini",
                "message_id": row["id"],
            }

        # All identity, configuration, publication, and contradictory partial
        # state has now been classified. The first write may occur only here.
        if config_id is None:
            config_id = connection.execute(
                """INSERT INTO participant_configs (
                       participant_id, provider, model, config_label,
                       system_instructions, settings_json, tools_json
                   ) VALUES (?, 'local', NULL, ?, NULL, ?, '[]')""",
                (helios["id"], WELCOME_CONFIG_LABEL, canonical_settings),
            ).lastrowid

        turn_id = create_turn(connection, room[0]["id"], helios["id"])
        message_id = store_message(
            connection,
            room_id=room[0]["id"],
            participant_id=helios["id"],
            participant_config_id=config_id,
            message_text=welcome,
            turn_id=turn_id,
            message_type="chat",
            sender_alias_id=current_alias(connection, helios["id"])["id"],
            destination_kind="participant",
            destination_alias_id=current_alias(connection, gemini["id"])["id"],
            recipient_participant_id=gemini["id"],
        )
        connection.execute(
            """UPDATE turns SET status='completed',
                   completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE id=? AND status='open'""",
            (turn_id,),
        )
        validate_v14_foundation(connection)
        validate_database_integrity(connection)
        connection.commit()
        return {
            "status": "published",
            "sender": "helios",
            "recipient": "gemini",
            "message_id": message_id,
        }
    except MessageVisibilityGuardError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except GeminiIdentityError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.IntegrityError as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _welcome_incompatible() from exception
    except (OSError, sqlite3.Error) as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _welcome_unavailable() from exception
    except Exception as exception:
        if connection.in_transaction:
            connection.rollback()
        raise _welcome_incompatible() from exception
    finally:
        connection.close()
