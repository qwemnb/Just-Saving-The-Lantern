"""SQLite connection, schema initialization, and milestone seed data."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schema" / "helios_room_schema_v1_2.sql"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "helios.db"

EXPECTED_SCHEMA_MIGRATION = (1, "1.2")
BUSY_TIMEOUT_MS = 5_000


class DatabaseInitializationError(RuntimeError):
    """Raised when an existing database is incompatible or incomplete."""


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
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1_000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def initialize_database(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    schema_path: Path | str = DEFAULT_SCHEMA_PATH,
) -> InitializationReport:
    """Install schema v1.2 when needed and ensure milestone seed records exist.

    The schema is executed only for a database with no user-defined objects.
    Seed creation is idempotent and runs under ``BEGIN IMMEDIATE`` so two local
    processes cannot create duplicate active memberships or initial configs.
    """

    database_path = Path(database_path)
    schema_path = Path(schema_path)
    schema_sql = schema_path.read_text(encoding="utf-8")

    connection = connect_database(database_path)
    try:
        _install_or_validate_schema(connection, schema_sql)
        _seed_initial_room(connection)
        return _build_report(connection, database_path)
    finally:
        connection.close()


def _install_or_validate_schema(
    connection: sqlite3.Connection,
    schema_sql: str,
) -> None:
    if _table_exists(connection, "schema_migrations"):
        _validate_schema_version(connection)
        return

    object_count = connection.execute(
        """
        SELECT count(*)
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        """
    ).fetchone()[0]
    if object_count:
        raise DatabaseInitializationError(
            "Database contains objects but has no schema_migrations table; "
            "refusing to guess its state."
        )

    connection.executescript(schema_sql)
    _validate_schema_version(connection)


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


def _validate_schema_version(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT migration_no, schema_label
        FROM schema_migrations
        ORDER BY migration_no
        """
    ).fetchall()
    versions = [(row["migration_no"], row["schema_label"]) for row in rows]
    if versions != [EXPECTED_SCHEMA_MIGRATION]:
        raise DatabaseInitializationError(
            "Expected only Helios Room schema migration "
            f"{EXPECTED_SCHEMA_MIGRATION!r}, found {versions!r}."
        )


def _seed_initial_room(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        room_id = _ensure_room(connection)
        peter_id = _ensure_participant(connection, "peter", "Peter", "human")
        helios_id = _ensure_participant(connection, "helios", "Helios", "ai")

        _ensure_active_membership(connection, room_id, peter_id)
        _ensure_active_membership(connection, room_id, helios_id)
        _ensure_initial_helios_config(connection, helios_id)
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


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
        schema_label=EXPECTED_SCHEMA_MIGRATION[1],
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
) -> int:
    """Store a message and return its ID.

    Allocates room_sequence_no and turn_sequence_no as needed.
    For human messages, participant_config_id should be None.
    For AI messages, participant_config_id is required by schema triggers.

    This function does not manage transactions - the caller should wrap
    calls in an appropriate transaction.
    """
    if is_blank_message(message_text):
        raise ValueError("message_text must contain non-whitespace text")

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
    return cursor.lastrowid

