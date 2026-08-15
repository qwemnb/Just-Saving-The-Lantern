"""SQLite connection, schema initialization, and milestone seed data."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .schema_validation import (
    SchemaValidationError,
    VisibilityPolicyValidationError,
    V12_HISTORY,
    V13_HISTORY,
    V14_HISTORY,
    schema_history,
    validate_database_integrity,
    validate_legacy_reset_dispatch,
    validate_v14_foundation,
    _validate_policy_timestamp,
)
from .maintenance_lock import ResetRecoveryRequiredError, acquire_database_lease


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schema" / "helios_room_schema_v1_4.sql"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "helios.db"

EXPECTED_SCHEMA_MIGRATIONS = ((1, "1.2"), (2, "1.3"), (3, "1.4"))
# Kept as the current marker for modules that need only the current label.
EXPECTED_SCHEMA_MIGRATION = EXPECTED_SCHEMA_MIGRATIONS[-1]
BUSY_TIMEOUT_MS = 5_000


class DatabaseInitializationError(RuntimeError):
    """Raised when an existing database is incompatible or incomplete."""

    def __init__(self, message: str, code: str = "database_initialization_failed") -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class MessageVisibilityGuardError(RuntimeError):
    code = "message_visibility_policy_unavailable"
    status_code = 409
    message = "The room history visibility policy does not permit this message to be stored."

    def __init__(self) -> None:
        super().__init__(self.message)

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class LockedConnection(sqlite3.Connection):
    _maintenance_lease: object | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            lease = self._maintenance_lease
            self._maintenance_lease = None
            if lease is not None:
                lease.close()


def has_exact_room_visibility_policy(connection: sqlite3.Connection) -> bool:
    """Return whether every current room has its sole shared sequence-1 event."""
    try:
        if schema_history(connection) != V14_HISTORY:
            return False
        if connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='room_history_visibility_events'"
        ).fetchone()[0] != 1:
            return False
        rooms = connection.execute("SELECT id FROM rooms").fetchall()
        for room in rooms:
            rows = connection.execute(
                """SELECT policy_version,effective_from_room_sequence_no,created_at
                   FROM room_history_visibility_events WHERE room_id=?""",
                (room[0],),
            ).fetchall()
            if len(rows) != 1 or tuple(rows[0][:2]) != ("room_shared_v1", 1):
                return False
            _validate_policy_timestamp(connection, rows[0][2])
        return True
    except (sqlite3.Error, SchemaValidationError):
        return False


@dataclass(frozen=True)
class InitializationReport:
    """A concise record of a successful database initialization."""

    database_path: str
    schema_label: str
    room_count: int
    participant_count: int
    active_membership_count: int
    helios_config_count: int
    integrity_check: str
    foreign_key_violations: int


def connect_database(database_path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    SQLite foreign-key enforcement is connection-local, so every application
    connection must be created through this function.
    """

    path = Path(database_path)
    lease = acquire_database_lease(path, shared=True)
    try:
        connection = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            factory=LockedConnection,
        )
    except Exception:
        lease.close()
        raise
    connection._maintenance_lease = lease
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def initialize_database(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    schema_path: Path | str = DEFAULT_SCHEMA_PATH,
) -> InitializationReport:
    """Install schema v1.4 when needed and ensure milestone seed records exist.

    The schema is executed only for a database with no user-defined objects.
    Seed creation is idempotent and runs under ``BEGIN IMMEDIATE`` so two local
    processes cannot create duplicate active memberships or initial configs.
    """

    database_path = Path(database_path)
    schema_path = Path(schema_path)
    try:
        connection = connect_database(database_path)
    except ResetRecoveryRequiredError as error:
        raise DatabaseInitializationError(error.message, code=error.code) from None
    try:
        if _table_exists(connection, "schema_migrations"):
            connection.execute("BEGIN")
            try:
                history = schema_history(connection)
                if history == V12_HISTORY or history == V13_HISTORY:
                    validate_legacy_reset_dispatch(connection, history)
                    raise DatabaseInitializationError(
                        "The existing Helios Room database must be retired and reinitialized before this version can run.",
                        code="database_reset_required",
                    )
                if history != V14_HISTORY:
                    raise DatabaseInitializationError(
                        "The existing database schema is incompatible."
                    )
                validate_v14_foundation(connection)
                validate_database_integrity(connection)
                report = _build_report(connection, database_path)
            except (SchemaValidationError, sqlite3.Error, TypeError, ValueError) as error:
                raise DatabaseInitializationError(
                    "The existing database schema or foundation data is incompatible."
                ) from error
            finally:
                if connection.in_transaction:
                    connection.rollback()
            return report

        object_count = connection.execute(
            """
            SELECT count(*) FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            """
        ).fetchone()[0]
        if object_count:
            raise DatabaseInitializationError(
                "Database contains objects but has no schema_migrations table; "
                "refusing to guess its state."
            )
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        _seed_initial_room(connection)
        connection.execute("BEGIN")
        try:
            validate_v14_foundation(connection)
            validate_database_integrity(connection)
            report = _build_report(connection, database_path)
        except (SchemaValidationError, sqlite3.Error, TypeError, ValueError) as error:
            raise DatabaseInitializationError(
                "The initialized database failed schema validation."
            ) from error
        finally:
            if connection.in_transaction:
                connection.rollback()
        return report
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _seed_initial_room(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        room_id = _ensure_room(connection)
        _ensure_room_history_visibility(connection, room_id)
        peter_id = _ensure_participant(connection, "peter", "Peter", "human")
        helios_id = _ensure_participant(connection, "helios", "Helios", "ai")
        gemini_id = _ensure_participant(connection, "gemini", "Gemini", "ai")
        room_system_id = _ensure_participant(
            connection, "room-system", "Room", "system"
        )

        _ensure_identity_bootstrap(connection, peter_id, "Peter")
        _ensure_identity_bootstrap(connection, helios_id, "Helios")
        _ensure_identity_bootstrap(connection, gemini_id, "Gemini")
        _ensure_identity_bootstrap(connection, room_system_id, "Room")

        _ensure_active_membership(connection, room_id, peter_id)
        _ensure_active_membership(connection, room_id, helios_id)
        _ensure_active_membership(connection, room_id, gemini_id)
        _ensure_initial_helios_config(connection, helios_id)
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _ensure_room_history_visibility(connection: sqlite3.Connection, room_id: int) -> None:
    rows = connection.execute(
        """SELECT policy_version, effective_from_room_sequence_no
           FROM room_history_visibility_events WHERE room_id=? ORDER BY id""",
        (room_id,),
    ).fetchall()
    if not rows:
        connection.execute(
            """INSERT INTO room_history_visibility_events
               (room_id, policy_version, effective_from_room_sequence_no)
               VALUES (?, 'room_shared_v1', 1)""",
            (room_id,),
        )
        return
    if len(rows) != 1 or tuple(rows[0]) != ("room_shared_v1", 1):
        raise DatabaseInitializationError("Room history visibility foundation is invalid.")


def _ensure_room(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        INSERT INTO rooms (room_key, name)
        VALUES (?, ?)
        ON CONFLICT(room_key) DO NOTHING
        """,
        ("main", "The Room"),
    )
    row = connection.execute(
        "SELECT id, name FROM rooms WHERE room_key = ?",
        ("main",),
    ).fetchone()
    if row is None or row["name"] != "The Room":
        raise DatabaseInitializationError(
            "The room key 'main' already exists with unexpected data."
        )
    return row["id"]


def create_room(connection: sqlite3.Connection, room_key: str, name: str) -> int:
    """Create a future room and its sequence-1 shared policy atomically."""
    if not connection.in_transaction:
        raise RuntimeError("room creation requires an active transaction")
    room_id = connection.execute(
        "INSERT INTO rooms (room_key,name) VALUES (?,?)", (room_key, name)
    ).lastrowid
    _ensure_room_history_visibility(connection, room_id)
    return room_id


def _ensure_participant(
    connection: sqlite3.Connection,
    participant_key: str,
    name: str,
    participant_type: str,
) -> int:
    connection.execute(
        """
        INSERT INTO participants (participant_key, name, participant_type)
        VALUES (?, ?, ?)
        ON CONFLICT(participant_key) DO NOTHING
        """,
        (participant_key, name, participant_type),
    )
    row = connection.execute(
        """
        SELECT id, name, participant_type
        FROM participants
        WHERE participant_key = ?
        """,
        (participant_key,),
    ).fetchone()
    if (
        row is None
        or row["name"] != name
        or row["participant_type"] != participant_type
    ):
        raise DatabaseInitializationError(
            f"Participant key {participant_key!r} already exists with unexpected data."
        )
    return row["id"]


def _ensure_active_membership(
    connection: sqlite3.Connection,
    room_id: int,
    participant_id: int,
) -> None:
    connection.execute(
        """
        INSERT INTO room_participants (room_id, participant_id)
        SELECT ?, ?
        WHERE NOT EXISTS (
            SELECT 1
            FROM room_participants
            WHERE room_id = ?
              AND participant_id = ?
              AND left_at IS NULL
        )
        """,
        (room_id, participant_id, room_id, participant_id),
    )


def _ensure_initial_helios_config(
    connection: sqlite3.Connection,
    helios_id: int,
) -> None:
    rows = connection.execute(
        """
        SELECT
            id,
            provider,
            model,
            system_instructions,
            settings_json,
            tools_json
        FROM participant_configs
        WHERE participant_id = ? AND config_label = ?
        ORDER BY id
        """,
        (helios_id, "initial"),
    ).fetchall()

    if not rows:
        connection.execute(
            """
            INSERT INTO participant_configs (
                participant_id,
                provider,
                model,
                config_label,
                system_instructions,
                settings_json,
                tools_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (helios_id, "openai", None, "initial", None, "{}", "[]"),
        )
        return

    if len(rows) != 1:
        raise DatabaseInitializationError(
            "Helios has multiple participant configs labeled 'initial'."
        )

    row = rows[0]
    expected = {
        "provider": "openai",
        "model": None,
        "system_instructions": None,
        "settings_json": "{}",
        "tools_json": "[]",
    }
    actual = {key: row[key] for key in expected}
    if actual != expected:
        raise DatabaseInitializationError(
            "Helios's existing 'initial' config differs from the milestone config."
        )


def _ensure_identity_bootstrap(
    connection: sqlite3.Connection,
    participant_id: int,
    display_alias: str,
) -> None:
    """Create the one immutable bootstrap alias graph for a participant."""

    alias_key = unicodedata.normalize("NFKC", display_alias).casefold()
    rows = connection.execute(
        """
        SELECT id, display_alias, alias_key
        FROM participant_aliases
        WHERE participant_id = ?
        ORDER BY id
        """,
        (participant_id,),
    ).fetchall()
    if not rows:
        cursor = connection.execute(
            """
            INSERT INTO participant_aliases (
                participant_id, display_alias, alias_key
            ) VALUES (?, ?, ?)
            """,
            (participant_id, display_alias, alias_key),
        )
        alias_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO participant_primary_aliases (participant_id, alias_id)
            VALUES (?, ?)
            """,
            (participant_id, alias_id),
        )
        connection.execute(
            """
            INSERT INTO participant_name_events (
                event_type, room_id, actor_participant_id,
                subject_participant_id, previous_alias_id,
                new_alias_id, canonical_message_id
            ) VALUES ('bootstrap', NULL, ?, ?, NULL, ?, NULL)
            """,
            (participant_id, participant_id, alias_id),
        )
        return

    if len(rows) != 1 or dict(rows[0]) != {
        "id": rows[0]["id"],
        "display_alias": display_alias,
        "alias_key": alias_key,
    }:
        raise DatabaseInitializationError("Participant alias bootstrap data is invalid.")
    alias_id = rows[0]["id"]
    primary = connection.execute(
        """SELECT alias_id FROM participant_primary_aliases
           WHERE participant_id = ?""",
        (participant_id,),
    ).fetchall()
    events = connection.execute(
        """SELECT event_type, room_id, actor_participant_id,
                  subject_participant_id, previous_alias_id, new_alias_id,
                  canonical_message_id
           FROM participant_name_events WHERE subject_participant_id = ?""",
        (participant_id,),
    ).fetchall()
    if len(primary) != 1 or primary[0]["alias_id"] != alias_id or len(events) != 1:
        raise DatabaseInitializationError("Participant identity bootstrap data is invalid.")
    event = events[0]
    if (
        event["event_type"] != "bootstrap"
        or event["room_id"] is not None
        or event["actor_participant_id"] != participant_id
        or event["subject_participant_id"] != participant_id
        or event["previous_alias_id"] is not None
        or event["new_alias_id"] != alias_id
        or event["canonical_message_id"] is not None
    ):
        raise DatabaseInitializationError("Participant bootstrap event is invalid.")


def _build_report(
    connection: sqlite3.Connection,
    database_path: Path,
) -> InitializationReport:
    integrity_rows = [
        row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]
    integrity_result = ", ".join(integrity_rows)
    if integrity_rows != ["ok"]:
        raise DatabaseInitializationError(
            f"SQLite integrity check failed: {integrity_result}"
        )

    foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        raise DatabaseInitializationError(
            f"SQLite foreign-key check found {len(foreign_key_rows)} violation(s)."
        )

    def count(query: str, parameters: tuple[object, ...] = ()) -> int:
        return connection.execute(query, parameters).fetchone()[0]

    return InitializationReport(
        database_path=str(database_path.resolve()),
        schema_label=EXPECTED_SCHEMA_MIGRATIONS[-1][1],
        room_count=count("SELECT count(*) FROM rooms"),
        participant_count=count("SELECT count(*) FROM participants"),
        active_membership_count=count(
            "SELECT count(*) FROM current_room_participants"
        ),
        helios_config_count=count(
            """
            SELECT count(*)
            FROM participant_configs AS pc
            JOIN participants AS p ON p.id = pc.participant_id
            WHERE p.participant_key = ?
            """,
            ("helios",),
        ),
        integrity_check=integrity_result,
        foreign_key_violations=len(foreign_key_rows),
    )


def create_turn(
    connection: sqlite3.Connection,
    room_id: int,
    initiated_by_participant_id: int,
) -> int:
    """Create a new turn and return its ID.

    The turn is created with status 'open'. The caller is responsible for
    updating the status to 'completed', 'failed', or 'cancelled' when done.
    """
    cursor = connection.execute(
        """
        INSERT INTO turns (room_id, initiated_by_participant_id, status)
        VALUES (?, ?, 'open')
        """,
        (room_id, initiated_by_participant_id),
    )
    return cursor.lastrowid


def allocate_next_room_sequence_no(
    connection: sqlite3.Connection,
    room_id: int,
) -> int:
    """Allocate the next room_sequence_no for a message.

    This must be called inside a write transaction to prevent race conditions.
    """
    row = connection.execute(
        """
        SELECT COALESCE(MAX(room_sequence_no), 0) + 1
        FROM messages
        WHERE room_id = ?
        """,
        (room_id,),
    ).fetchone()
    return row[0]


def is_blank_message(message_text: str) -> bool:
    """Return whether a message contains no non-whitespace text."""

    return not message_text.strip()


def store_message(
    connection: sqlite3.Connection,
    room_id: int,
    participant_id: int,
    message_text: str,
    turn_id: int | None = None,
    participant_config_id: int | None = None,
    reply_to_id: int | None = None,
    message_type: str = "chat",
    *,
    sender_alias_id: int | None = None,
    destination_kind: str | None = None,
    destination_alias_id: int | None = None,
    recipient_participant_id: int | None = None,
    routing_mode: str = "explicit",
) -> int:
    """Atomically stage a canonical message and its immutable route.

    Allocates room_sequence_no and turn_sequence_no as needed.
    For human messages, participant_config_id should be None.
    For AI messages, participant_config_id is required by schema triggers.

    This function does not manage transactions - the caller should wrap
    calls in an appropriate transaction.
    """
    if is_blank_message(message_text):
        raise ValueError("message_text must contain non-whitespace text")

    if sender_alias_id is None or destination_kind is None or destination_alias_id is None:
        sender_alias_row = connection.execute(
            """SELECT ppa.alias_id, p.participant_key
               FROM participant_primary_aliases AS ppa
               JOIN participants AS p ON p.id=ppa.participant_id
               WHERE ppa.participant_id=?""",
            (participant_id,),
        ).fetchone()
        if sender_alias_row is None:
            raise ValueError("sender routing identity is required")
        sender_alias_id = sender_alias_row["alias_id"]
        if message_type == "chat" and sender_alias_row["participant_key"] in {"peter", "helios"}:
            recipient_key = "helios" if sender_alias_row["participant_key"] == "peter" else "peter"
            recipient = connection.execute(
                """SELECT p.id, ppa.alias_id FROM participants AS p
                   JOIN participant_primary_aliases AS ppa ON ppa.participant_id=p.id
                   WHERE p.participant_key=?""",
                (recipient_key,),
            ).fetchone()
            if recipient is None:
                raise ValueError("recipient routing identity is required")
            destination_kind = "participant"
            recipient_participant_id = recipient["id"]
            destination_alias_id = recipient["alias_id"]
        else:
            room_alias = connection.execute(
                """SELECT ppa.alias_id FROM participants AS p
                   JOIN participant_primary_aliases AS ppa ON ppa.participant_id=p.id
                   WHERE p.participant_key='room-system'"""
            ).fetchone()
            if room_alias is None:
                raise ValueError("Room routing identity is required")
            destination_kind = "room"
            recipient_participant_id = None
            destination_alias_id = room_alias["alias_id"]

    try:
        validate_v14_foundation(connection)
        policy_rows = connection.execute(
            """SELECT policy_version, effective_from_room_sequence_no
               FROM room_history_visibility_events WHERE room_id=?""",
            (room_id,),
        ).fetchall()
        if len(policy_rows) != 1 or tuple(policy_rows[0]) != ("room_shared_v1", 1):
            raise MessageVisibilityGuardError()
    except VisibilityPolicyValidationError:
        raise MessageVisibilityGuardError() from None

    room_sequence_no = allocate_next_room_sequence_no(connection, room_id)

    turn_sequence_no = None
    if turn_id is not None:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(turn_sequence_no), 0) + 1
            FROM messages
            WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
        turn_sequence_no = row[0]

    try:
        cursor = connection.execute(
            """
            INSERT INTO messages (
                turn_id,
                room_id,
                room_sequence_no,
                turn_sequence_no,
                participant_id,
                participant_config_id,
                reply_to_id,
                message_type,
                message_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                room_id,
                room_sequence_no,
                turn_sequence_no,
                participant_id,
                participant_config_id,
                reply_to_id,
                message_type,
                message_text,
            ),
        )
        message_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO message_routes (
                message_id, room_id, sender_participant_id, sender_alias_id,
                destination_kind, recipient_participant_id,
                destination_alias_id, routing_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                room_id,
                participant_id,
                sender_alias_id,
                destination_kind,
                recipient_participant_id,
                destination_alias_id,
                routing_mode,
            ),
        )
    except sqlite3.IntegrityError as error:
        if str(error) == "message_visibility_guard":
            raise MessageVisibilityGuardError() from None
        raise
    return message_id

