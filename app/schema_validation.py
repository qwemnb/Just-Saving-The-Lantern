"""Pure read-only schema and identity-foundation validators.

The functions in this module accept a caller-owned SQLite connection.  They
never open, close, commit, roll back, initialize, migrate, repair, or otherwise
write a database.  Callers are responsible for placing all checks in the
appropriate read or write transaction.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V12_SCHEMA_PATH = PROJECT_ROOT / "schema" / "helios_room_schema_v1_2.sql"
V13_SCHEMA_PATH = PROJECT_ROOT / "schema" / "helios_room_schema_v1_3.sql"
V14_SCHEMA_PATH = PROJECT_ROOT / "schema" / "helios_room_schema_v1_4.sql"
V12_HISTORY = [(1, "1.2")]
V13_HISTORY = [(1, "1.2"), (2, "1.3")]
V14_HISTORY = [(1, "1.2"), (2, "1.3"), (3, "1.4")]
RESERVED_ALIAS_KEYS = frozenset(
    {"all", "everyone", "system", "participants", "room"}
)


class SchemaValidationError(RuntimeError):
    """Raised when an observed snapshot is not a supported Helios schema."""


class VisibilityPolicyValidationError(SchemaValidationError):
    """Raised after schema validation isolates shared-policy row corruption."""


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split()).casefold()


@lru_cache(maxsize=3)
def _required_schema_objects(schema_path: str) -> dict[tuple[str, str], str]:
    """Parse explicit CREATE statements without executing the schema.

    Exact normalized CREATE SQL covers the required columns, declared types,
    nullability, keys, constraints, view semantics, index order/uniqueness, and
    trigger bodies.  FTS shadow tables are deliberately excluded because they
    are SQLite-owned consequences of the required virtual-table definitions.
    """

    text = Path(schema_path).read_text(encoding="utf-8")
    text = re.sub(r"--[^\n]*", "", text)
    statement = ""
    objects: dict[tuple[str, str], str] = {}
    pattern = re.compile(
        r"\s*CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?"
        r"(TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)",
        re.IGNORECASE,
    )
    for line in text.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        match = pattern.match(statement)
        if match:
            key = (match.group(1).casefold(), match.group(2).casefold())
            objects[key] = _normalized_sql(statement)
        statement = ""
    return objects


def _validate_schema_objects(
    connection: sqlite3.Connection, schema_path: Path
) -> None:
    expected = _required_schema_objects(str(schema_path))
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_schema
        WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    actual = {
        (str(row[0]).casefold(), str(row[1]).casefold()): _normalized_sql(row[2])
        for row in rows
        if not (
            str(row[0]).casefold() == "table"
            and any(
                str(row[1]).casefold().startswith(f"{base}_")
                for base in (
                    "messages_fts",
                    "seed_memories_fts",
                    "room_memories_fts",
                )
            )
        )
    }
    if set(actual) != set(expected):
        raise SchemaValidationError("schema object set is missing or divergent")
    for key, sql in expected.items():
        if actual.get(key) != sql:
            raise SchemaValidationError("required schema object is missing or divergent")


def schema_history(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute(
        """
        SELECT migration_no, schema_label
        FROM schema_migrations
        ORDER BY migration_no
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def validate_database_integrity(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise SchemaValidationError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SchemaValidationError("database foreign-key check failed")


def _alias_parts(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise SchemaValidationError("invalid participant alias")
    display = value.strip()
    if display != value or not 1 <= len(display) <= 64:
        raise SchemaValidationError("invalid participant alias")
    if display[0] == "/" or "[" in display or "]" in display:
        raise SchemaValidationError("invalid participant alias")
    if any(unicodedata.category(character).startswith("C") for character in display):
        raise SchemaValidationError("invalid participant alias")
    key = unicodedata.normalize("NFKC", display).casefold()
    if key in RESERVED_ALIAS_KEYS:
        raise SchemaValidationError("reserved participant alias")
    return display, key


def _one(rows: list[sqlite3.Row] | list[tuple[Any, ...]]) -> Any:
    if len(rows) != 1:
        raise SchemaValidationError("required row cardinality is invalid")
    return rows[0]


def _main_room(connection: sqlite3.Connection) -> sqlite3.Row:
    row = _one(
        connection.execute(
            "SELECT id, room_key, name FROM rooms WHERE room_key = 'main'"
        ).fetchall()
    )
    if row[1] != "main" or row[2] != "The Room":
        raise SchemaValidationError("main room identity is invalid")
    return row


def _required_participant(
    connection: sqlite3.Connection,
    key: str,
    name: str,
    participant_type: str,
) -> sqlite3.Row:
    row = _one(
        connection.execute(
            """
            SELECT id, participant_key, name, participant_type
            FROM participants
            WHERE participant_key = ?
            """,
            (key,),
        ).fetchall()
    )
    if row[1] != key or row[2] != name or row[3] != participant_type:
        raise SchemaValidationError("required participant identity is invalid")
    return row


def _validate_core_memberships(
    connection: sqlite3.Connection,
    room_id: int,
    peter_id: int,
    helios_id: int,
    room_system_id: int | None = None,
) -> None:
    for participant_id in (peter_id, helios_id):
        count = connection.execute(
            """
            SELECT count(*) FROM room_participants
            WHERE room_id = ? AND participant_id = ? AND left_at IS NULL
            """,
            (room_id, participant_id),
        ).fetchone()[0]
        if count != 1:
            raise SchemaValidationError("required active membership is invalid")
    if room_system_id is not None:
        count = connection.execute(
            """
            SELECT count(*) FROM room_participants
            WHERE participant_id = ? AND left_at IS NULL
            """,
            (room_system_id,),
        ).fetchone()[0]
        if count:
            raise SchemaValidationError("room-system cannot have active membership")


def _validate_v12_source_contract(connection: sqlite3.Connection) -> None:
    """Validate the complete mature v1.2 migration source condition."""

    if schema_history(connection) != V12_HISTORY:
        raise SchemaValidationError("schema history is not exact v1.2")
    _validate_schema_objects(connection, V12_SCHEMA_PATH)
    validate_database_integrity(connection)

    room = _main_room(connection)
    peter = _required_participant(connection, "peter", "Peter", "human")
    helios = _required_participant(connection, "helios", "Helios", "ai")
    if connection.execute(
        "SELECT count(*) FROM participants WHERE participant_key = 'room-system'"
    ).fetchone()[0]:
        raise SchemaValidationError("v1.2 contains reserved room-system identity")
    _validate_core_memberships(connection, room[0], peter[0], helios[0])

    seen: set[str] = set()
    for participant in connection.execute(
        "SELECT id, participant_key, name, participant_type FROM participants ORDER BY id"
    ).fetchall():
        display, key = _alias_parts(participant[2])
        if display != participant[2] or key in seen:
            raise SchemaValidationError("participant aliases cannot be migrated safely")
        seen.add(key)
        if not isinstance(participant[1], str) or not participant[1].strip():
            raise SchemaValidationError("participant key is invalid")
        if participant[3] not in {"human", "ai", "system"}:
            raise SchemaValidationError("participant type is invalid")

    # These are the exact message shapes accepted by the immutable-route
    # backfill.  Mature turn state and provider completeness are intentionally
    # not projected here.
    messages = connection.execute(
        """
        SELECT m.room_id, m.message_type, p.participant_key,
               t.initiated_by_participant_id
        FROM messages AS m
        JOIN participants AS p ON p.id = m.participant_id
        LEFT JOIN turns AS t ON t.id = m.turn_id
        ORDER BY m.id
        """
    ).fetchall()
    for message in messages:
        if message[0] != room[0]:
            raise SchemaValidationError("message cannot be routed by v1.3 migration")
        if message[1] == "chat" and message[2] == "peter":
            continue
        if (
            message[1] == "chat"
            and message[2] == "helios"
            and message[3] == peter[0]
        ):
            continue
        if message[1] == "system":
            continue
        raise SchemaValidationError("message cannot be routed by v1.3 migration")


def _validated_aliases(
    connection: sqlite3.Connection,
    room_system_id: int,
) -> tuple[dict[int, sqlite3.Row], sqlite3.Row]:
    aliases: dict[int, sqlite3.Row] = {}
    room_aliases: list[sqlite3.Row] = []
    rows = connection.execute(
        """
        SELECT id, participant_id, display_alias, alias_key
        FROM participant_aliases
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        if row[1] == room_system_id:
            if row[2] != "Room" or row[3] != "room":
                raise SchemaValidationError("room-system alias graph is invalid")
            room_aliases.append(row)
        else:
            display, key = _alias_parts(row[2])
            if display != row[2] or key != row[3]:
                raise SchemaValidationError("stored participant alias is invalid")
        aliases[row[0]] = row
    room_alias = _one(room_aliases)
    return aliases, room_alias


def _validate_name_lineage(
    connection: sqlite3.Connection,
    main_room_id: int,
    room_system_id: int,
    room_alias: sqlite3.Row,
    aliases: dict[int, sqlite3.Row],
) -> None:
    participants = connection.execute(
        "SELECT id, name, participant_key FROM participants ORDER BY id"
    ).fetchall()
    primaries = connection.execute(
        "SELECT participant_id, alias_id FROM participant_primary_aliases"
    ).fetchall()
    primary_by_participant = {row[0]: row[1] for row in primaries}
    if len(primary_by_participant) != len(participants):
        raise SchemaValidationError("current-primary cardinality is invalid")

    for participant in participants:
        participant_id = participant[0]
        own_aliases = {
            alias_id: alias
            for alias_id, alias in aliases.items()
            if alias[1] == participant_id
        }
        if not own_aliases:
            raise SchemaValidationError("participant has no immutable alias")
        primary_id = primary_by_participant.get(participant_id)
        if primary_id not in own_aliases:
            raise SchemaValidationError("current-primary ownership is invalid")
        events = connection.execute(
            """
            SELECT id, event_type, room_id, actor_participant_id,
                   subject_participant_id, previous_alias_id, new_alias_id,
                   canonical_message_id
            FROM participant_name_events
            WHERE subject_participant_id = ?
            ORDER BY id
            """,
            (participant_id,),
        ).fetchall()
        if not events or sum(event[1] == "bootstrap" for event in events) != 1:
            raise SchemaValidationError("bootstrap event cardinality is invalid")
        bootstrap = events[0]
        if (
            bootstrap[1] != "bootstrap"
            or bootstrap[2] is not None
            or bootstrap[3] != participant_id
            or bootstrap[4] != participant_id
            or bootstrap[5] is not None
            or bootstrap[6] not in own_aliases
            or bootstrap[7] is not None
            or own_aliases[bootstrap[6]][2] != participant[1]
        ):
            raise SchemaValidationError("bootstrap event is invalid")

        previous_new_alias_id = bootstrap[6]
        for event in events[1:]:
            if (
                event[1] != "adopted"
                or event[2] != main_room_id
                or event[3] != participant_id
                or event[4] != participant_id
                or event[5] != previous_new_alias_id
                or event[5] not in own_aliases
                or event[6] not in own_aliases
                or event[7] is None
            ):
                raise SchemaValidationError("adopted name-event lineage is invalid")
            message_rows = connection.execute(
                """
                SELECT m.id, m.turn_id, m.room_id, m.turn_sequence_no,
                       m.participant_id, m.participant_config_id, m.reply_to_id,
                       m.message_type, m.message_text,
                       mr.sender_alias_id, mr.destination_kind,
                       mr.recipient_participant_id, mr.destination_alias_id,
                       mr.routing_mode
                FROM messages AS m
                JOIN message_routes AS mr ON mr.message_id = m.id
                WHERE m.id = ?
                """,
                (event[7],),
            ).fetchall()
            message = _one(message_rows)
            expected_text = (
                f"{own_aliases[event[5]][2]} adopted the name "
                f"{own_aliases[event[6]][2]}."
            )
            if (
                message[1] is not None
                or message[2] != main_room_id
                or message[3] is not None
                or message[4] != room_system_id
                or message[5] is not None
                or message[6] is not None
                or message[7] != "system"
                or message[8] != expected_text
                or message[9] != room_alias[0]
                or message[10] != "room"
                or message[11] is not None
                or message[12] != room_alias[0]
                or message[13] != "explicit"
            ):
                raise SchemaValidationError("adopted name-event message is invalid")
            previous_new_alias_id = event[6]

        if primary_id != previous_new_alias_id:
            raise SchemaValidationError("current-primary does not match latest name event")
        if participant_id == room_system_id and (
            len(own_aliases) != 1 or len(events) != 1 or primary_id != room_alias[0]
        ):
            raise SchemaValidationError("room-system identity graph is invalid")


def _validate_message_routes(
    connection: sqlite3.Connection,
    room_system_id: int,
    room_alias_id: int,
    aliases: dict[int, sqlite3.Row],
) -> None:
    message_count = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
    route_count = connection.execute("SELECT count(*) FROM message_routes").fetchone()[0]
    if message_count != route_count:
        raise SchemaValidationError("message-route cardinality is invalid")
    rows = connection.execute(
        """
        SELECT mr.message_id, mr.room_id, mr.sender_participant_id,
               mr.sender_alias_id, mr.destination_kind,
               mr.recipient_participant_id, mr.destination_alias_id,
               mr.routing_mode, m.room_id, m.participant_id
        FROM message_routes AS mr
        LEFT JOIN messages AS m ON m.id = mr.message_id
        ORDER BY mr.message_id
        """
    ).fetchall()
    for row in rows:
        sender_alias = aliases.get(row[3])
        destination_alias = aliases.get(row[6])
        if (
            row[8] is None
            or row[1] != row[8]
            or row[2] != row[9]
            or sender_alias is None
            or sender_alias[1] != row[2]
            or destination_alias is None
            or row[7] not in {"legacy_implicit", "explicit"}
        ):
            raise SchemaValidationError("message route identity is invalid")
        if row[4] == "participant":
            if row[5] is None or destination_alias[1] != row[5]:
                raise SchemaValidationError("participant destination route is invalid")
        elif row[4] == "room":
            if (
                row[5] is not None
                or row[6] != room_alias_id
                or destination_alias[1] != room_system_id
            ):
                raise SchemaValidationError("Room destination route is invalid")
        else:
            raise SchemaValidationError("route destination kind is invalid")


def _validate_v13_foundation_contract(connection: sqlite3.Connection) -> None:
    """Validate schema v1.3 and its mature identity/routing foundation only."""

    if schema_history(connection) != V13_HISTORY:
        raise SchemaValidationError("schema history is not exact v1.3")
    _validate_schema_objects(connection, V13_SCHEMA_PATH)
    room = _main_room(connection)
    peter = _required_participant(connection, "peter", "Peter", "human")
    helios = _required_participant(connection, "helios", "Helios", "ai")
    room_system = _required_participant(
        connection, "room-system", "Room", "system"
    )
    _validate_core_memberships(
        connection, room[0], peter[0], helios[0], room_system[0]
    )
    aliases, room_alias = _validated_aliases(connection, room_system[0])
    _validate_name_lineage(
        connection, room[0], room_system[0], room_alias, aliases
    )
    _validate_message_routes(
        connection, room_system[0], room_alias[0], aliases
    )


# The exported legacy names are reserved for the guarded reset source/backup
# path. Ordinary v1.4 dispatch uses the classifier below and never calls them.
validate_v12_source = _validate_v12_source_contract
validate_v13_foundation = _validate_v13_foundation_contract


def validate_legacy_reset_dispatch(
    connection: sqlite3.Connection, history: list[tuple[int, str]]
) -> None:
    """Classify an exact retired schema without invoking a reset operation."""

    if history == V12_HISTORY:
        _validate_v12_source_contract(connection)
    elif history == V13_HISTORY:
        _validate_v13_foundation_contract(connection)
        validate_database_integrity(connection)
    else:
        raise SchemaValidationError("schema history is not a retired exact schema")


def _validate_policy_timestamp(connection: sqlite3.Connection, value: Any) -> None:
    if not isinstance(value, str):
        raise VisibilityPolicyValidationError("visibility policy timestamp is invalid")
    try:
        canonical = (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except ValueError as error:
        raise VisibilityPolicyValidationError("visibility policy timestamp is invalid") from error
    if canonical != value:
        raise VisibilityPolicyValidationError("visibility policy timestamp is invalid")
    round_trip = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', ?)", (value,)
    ).fetchone()[0]
    if round_trip != value:
        raise VisibilityPolicyValidationError("visibility policy timestamp is invalid")


def validate_v14_foundation(connection: sqlite3.Connection) -> None:
    """Validate the complete mature room-shared schema v1.4 foundation."""

    if schema_history(connection) != V14_HISTORY:
        raise SchemaValidationError("schema history is not exact v1.4")
    _validate_schema_objects(connection, V14_SCHEMA_PATH)
    room = _main_room(connection)
    peter = _required_participant(connection, "peter", "Peter", "human")
    helios = _required_participant(connection, "helios", "Helios", "ai")
    gemini = _required_participant(connection, "gemini", "Gemini", "ai")
    room_system = _required_participant(
        connection, "room-system", "Room", "system"
    )
    _validate_core_memberships(
        connection, room[0], peter[0], helios[0], room_system[0]
    )
    gemini_membership = connection.execute(
        """SELECT count(*) FROM room_participants
           WHERE room_id=? AND participant_id=? AND left_at IS NULL""",
        (room[0], gemini[0]),
    ).fetchone()[0]
    if gemini_membership != 1:
        raise SchemaValidationError("required Gemini membership is invalid")
    aliases, room_alias = _validated_aliases(connection, room_system[0])
    _validate_name_lineage(
        connection, room[0], room_system[0], room_alias, aliases
    )
    _validate_message_routes(connection, room_system[0], room_alias[0], aliases)

    initial = connection.execute(
        """SELECT pc.provider, pc.model, pc.system_instructions,
                  pc.settings_json, pc.tools_json
           FROM participant_configs AS pc
           WHERE pc.participant_id=? AND pc.config_label='initial'""",
        (helios[0],),
    ).fetchall()
    if len(initial) != 1 or tuple(initial[0]) != ("openai", None, None, "{}", "[]"):
        raise SchemaValidationError("initial Helios configuration is invalid")

    rooms = connection.execute("SELECT id FROM rooms ORDER BY id").fetchall()
    events = connection.execute(
        """SELECT room_id, policy_version, effective_from_room_sequence_no, created_at
           FROM room_history_visibility_events ORDER BY room_id, id"""
    ).fetchall()
    by_room: dict[int, list[sqlite3.Row]] = {row[0]: [] for row in rooms}
    for event in events:
        if event[0] not in by_room:
            raise VisibilityPolicyValidationError("visibility policy room is invalid")
        by_room[event[0]].append(event)
    for room_id, room_events in by_room.items():
        if len(room_events) != 1:
            raise VisibilityPolicyValidationError("visibility policy cardinality is invalid")
        event = room_events[0]
        if event[1] != "room_shared_v1" or event[2] != 1:
            raise VisibilityPolicyValidationError("visibility policy value is invalid")
        _validate_policy_timestamp(connection, event[3])
        uncovered = connection.execute(
            """SELECT count(*) FROM messages
               WHERE room_id=? AND room_sequence_no < 1""",
            (room_id,),
        ).fetchone()[0]
        if uncovered:
            raise VisibilityPolicyValidationError("message visibility coverage is invalid")
