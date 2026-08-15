"""Read-only, historical Trace v3 snapshots for the main Helios room."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .identity_service import ParticipantNameError, validate_display_alias
from .gemini_client import (
    FAILURE_KINDS as GEMINI_FAILURE_KINDS,
    GEMINI_SYSTEM_INSTRUCTIONS,
    MAX_SAFE_INTEGER,
    TOKEN_COUNT_FIELDS as GEMINI_TOKEN_COUNT_FIELDS,
    content_from_recorded,
    recorded_settings as gemini_recorded_settings,
    validate_bounded_stored_response,
    validate_recorded_google_shared_request_payload,
    validate_stored_success_response,
)
from .read_snapshot import ReadSnapshotError, run_read_snapshot
from .maintenance_lock import MaintenanceLockError, ResetRecoveryRequiredError
from .provider_history import ProviderHistoryError, load_provider_history
from .request_validation import validate_recorded_openai_shared_request_payload
from .request_validation import (
    OPENAI_RESPONSE_SETTINGS,
    OPENAI_RESPONSE_TOOLS,
    OPENAI_SYSTEM_INSTRUCTIONS,
)
from .schema_validation import SchemaValidationError, validate_v14_foundation
from .seed_memory import (
    INHERITED_MEMORY_HEADER,
    INHERITED_MEMORY_PROVENANCE,
    RESULT_LIMIT,
    RETRIEVER_VERSION,
    TEXT_BUDGET_CHARS,
    build_fts_query,
    tokenize_memory_query,
)


ROOM_KEY = "main"
REQUEST_EVENT = "openai.responses.request"
RESPONSE_EVENT = "openai.responses.response"
ERROR_EVENT = "openai.responses.error"
GOOGLE_REQUEST_EVENT = "google.generate_content.request"
GOOGLE_RESPONSE_EVENT = "google.generate_content.response"
GOOGLE_ERROR_EVENT = "google.generate_content.error"
REQUEST_EVENTS = frozenset((REQUEST_EVENT, GOOGLE_REQUEST_EVENT))
RESPONSE_EVENTS = frozenset((RESPONSE_EVENT, GOOGLE_RESPONSE_EVENT))
ERROR_EVENTS = frozenset((ERROR_EVENT, GOOGLE_ERROR_EVENT))
TERMINAL_EVENTS = RESPONSE_EVENTS | ERROR_EVENTS
RECOGNIZED_EVENTS = REQUEST_EVENTS | TERMINAL_EVENTS
MEMORY_RETRIEVAL_FIELDS = frozenset(
    {
        "retriever_version",
        "owner_participant_id",
        "query_source_message_id",
        "query_terms",
        "fts_query",
        "result_limit",
        "text_budget_chars",
        "omitted_for_budget",
        "selected",
    }
)
MEMORY_SELECTION_FIELDS = frozenset(
    {
        "rank",
        "seed_memory_id",
        "stable_id",
        "seed_batch_id",
        "source_content_sha256",
        "source_label",
        "source_locator",
        "memory_text_sha256",
        "exact_topic_match",
        "topic_match_weight_sum",
        "fts_bm25",
        "importance",
        "confidence",
    }
)


@dataclass(frozen=True)
class TraceServiceError(RuntimeError):
    """A stable trace failure safe to return to the browser."""

    status_code: int
    code: str
    message: str

    def as_payload(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _database_unavailable() -> TraceServiceError:
    return TraceServiceError(
        503,
        "trace_database_unavailable",
        "The configured Helios trace database is unavailable or incompatible.",
    )


def _data_invalid() -> TraceServiceError:
    return TraceServiceError(
        500,
        "trace_data_invalid",
        "The recorded trace data is invalid.",
    )


def load_trace(database_path: Path | str, turn_id: int | None = None) -> dict[str, Any]:
    """Load one deterministic Trace v3 document from a single snapshot."""

    def load(connection: sqlite3.Connection) -> dict[str, Any]:
        validate_v14_foundation(connection)
        return _load_snapshot(connection, turn_id)

    try:
        return run_read_snapshot(
            database_path,
            load,
            allow_wal_retry=True,
        ).value
    except TraceServiceError:
        raise
    except ResetRecoveryRequiredError as exception:
        raise TraceServiceError(503, exception.code, exception.message) from None
    except MaintenanceLockError as exception:
        raise TraceServiceError(
            503, "database_maintenance_in_progress",
            "The Helios Room database is unavailable during maintenance.",
        ) from exception
    except ReadSnapshotError as exception:
        raise _database_unavailable() from exception
    except SchemaValidationError as exception:
        raise _data_invalid() from exception
    except (
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        TypeError,
    ) as exception:
        raise _data_invalid() from exception
    except sqlite3.Error as exception:
        raise _database_unavailable() from exception


def _load_snapshot(
    connection: sqlite3.Connection,
    turn_id: int | None,
) -> dict[str, Any]:
    turn = _load_turn(connection, turn_id)
    selected_turn_id = turn["id"]
    room_id = turn["room_id"]

    messages, message_config_ids = _load_messages(
        connection, selected_turn_id, room_id
    )
    events, event_config_ids = _load_events(
        connection, selected_turn_id, room_id
    )
    request_events = [event for event in events if event["event_type"] in REQUEST_EVENTS]
    terminal_events = [
        event for event in events if event["event_type"] in TERMINAL_EVENTS
    ]
    if len(request_events) > 1 or len(terminal_events) > 1:
        raise _data_invalid()

    if request_events:
        _validate_raw_inherited_memory(request_events[0], messages)

    configurations = _load_configurations(
        connection, message_config_ids | event_config_ids
    )
    configurations_by_id = {item["id"]: item for item in configurations}
    _validate_configuration_references(
        messages, events, configurations_by_id
    )
    _validate_event_family(connection, turn, messages, events, configurations_by_id)

    projected_events = [_project_event(event) for event in events]
    request_events = [event for event in projected_events if event["event_type"] in REQUEST_EVENTS]
    terminal_events = [
        event for event in projected_events if event["event_type"] in TERMINAL_EVENTS
    ]
    recorded_request = (
        _build_recorded_request(request_events[0]) if request_events else None
    )
    inherited_memory = _build_inherited_memory(
        recorded_request,
        messages,
        request_events[0] if request_events else None,
    )
    provider_outcome = (
        _build_provider_outcome(
            terminal_events[0],
            _requested_model(recorded_request),
        )
        if terminal_events
        else None
    )

    initiated_by: dict[str, Any] | None = None
    if turn["initiated_by_participant_id"] is not None:
        if turn["initiator_id"] is None:
            raise _data_invalid()
        initiated_by = {
            "id": turn["initiator_id"],
            "participant_key": turn["initiator_key"],
            "name": turn["initiator_name"],
            "participant_type": turn["initiator_type"],
        }

    return {
        "trace_version": 3,
        "turn": {
            "id": selected_turn_id,
            "status": turn["status"],
            "created_at": turn["created_at"],
            "completed_at": turn["completed_at"],
            "room": {
                "id": room_id,
                "room_key": turn["room_key"],
                "name": turn["room_name"],
            },
            "initiated_by": initiated_by,
        },
        "messages": messages,
        "configurations": configurations,
        "recorded_request": recorded_request,
        "inherited_memory": inherited_memory,
        "provider_outcome": provider_outcome,
        "api_events": projected_events,
    }


def _load_turn(
    connection: sqlite3.Connection,
    turn_id: int | None,
) -> sqlite3.Row:
    where = "t.id = ? AND r.room_key = ?" if turn_id is not None else "r.room_key = ?"
    parameters: tuple[Any, ...] = (
        (turn_id, ROOM_KEY) if turn_id is not None else (ROOM_KEY,)
    )
    row = connection.execute(
        f"""
        SELECT
            t.id,
            t.room_id,
            t.initiated_by_participant_id,
            t.status,
            t.created_at,
            t.completed_at,
            r.room_key,
            r.name AS room_name,
            initiator.id AS initiator_id,
            initiator.participant_key AS initiator_key,
            initiator.name AS initiator_name,
            initiator.participant_type AS initiator_type
        FROM turns AS t
        JOIN rooms AS r ON r.id = t.room_id
        LEFT JOIN participants AS initiator
          ON initiator.id = t.initiated_by_participant_id
        WHERE {where}
        ORDER BY t.id DESC
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    if row is None:
        raise TraceServiceError(
            404,
            "trace_not_found",
            "No matching Helios turn was found.",
        )
    return row


def _load_messages(
    connection: sqlite3.Connection,
    turn_id: int,
    room_id: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    rows = connection.execute(
        """
        SELECT
            m.id,
            m.turn_id,
            m.room_id,
            m.room_sequence_no,
            m.turn_sequence_no,
            m.participant_id,
            m.participant_config_id,
            m.reply_to_id,
            m.message_type,
            m.message_text,
            m.created_at,
            participant.id AS found_participant_id,
            participant.participant_key,
            participant.name AS participant_name,
            participant.participant_type,
            reply.id AS found_reply_id,
            reply.turn_id AS reply_turn_id,
            reply.room_id AS reply_room_id,
            route.message_id AS found_route_id,
            route.room_id AS route_room_id,
            route.sender_participant_id,
            route.destination_kind,
            route.recipient_participant_id,
            route.routing_mode,
            sender_alias.participant_id AS sender_alias_owner,
            sender_alias.display_alias AS sender_display_name,
            sender_alias.alias_key AS sender_alias_key,
            destination_alias.participant_id AS destination_alias_owner,
            destination_alias.display_alias AS destination_display_name,
            destination_alias.alias_key AS destination_alias_key,
            recipient.participant_key AS recipient_key,
            destination_owner.participant_key AS destination_owner_key
        FROM messages AS m
        LEFT JOIN participants AS participant ON participant.id = m.participant_id
        LEFT JOIN messages AS reply ON reply.id = m.reply_to_id
        LEFT JOIN message_routes AS route ON route.message_id = m.id
        LEFT JOIN participant_aliases AS sender_alias ON sender_alias.id = route.sender_alias_id
        LEFT JOIN participant_aliases AS destination_alias ON destination_alias.id = route.destination_alias_id
        LEFT JOIN participants AS recipient ON recipient.id = route.recipient_participant_id
        LEFT JOIN participants AS destination_owner ON destination_owner.id = destination_alias.participant_id
        WHERE m.turn_id = ?
        ORDER BY m.turn_sequence_no, m.id
        """,
        (turn_id,),
    ).fetchall()

    seen_sequence_numbers: set[int] = set()
    seen_room_sequence_numbers: set[int] = set()
    config_ids: set[int] = set()
    messages: list[dict[str, Any]] = []
    for row in rows:
        sequence_no = row["turn_sequence_no"]
        room_sequence_no = row["room_sequence_no"]
        if (
            row["room_id"] != room_id
            or row["found_participant_id"] is None
            or sequence_no is None
            or sequence_no <= 0
            or sequence_no in seen_sequence_numbers
            or room_sequence_no <= 0
            or room_sequence_no in seen_room_sequence_numbers
            or row["found_route_id"] != row["id"]
            or row["route_room_id"] != room_id
            or row["sender_participant_id"] != row["participant_id"]
            or row["sender_alias_owner"] != row["participant_id"]
        ):
            raise _data_invalid()
        seen_sequence_numbers.add(sequence_no)
        seen_room_sequence_numbers.add(room_sequence_no)

        reply_to_id = row["reply_to_id"]
        reply_turn_id = row["reply_turn_id"]
        if reply_to_id is not None and (
            row["found_reply_id"] is None or row["reply_room_id"] != room_id
        ):
            raise _data_invalid()

        config_id = row["participant_config_id"]
        if config_id is not None:
            config_ids.add(config_id)
        if row["destination_kind"] == "participant":
            if (
                row["recipient_participant_id"] is None
                or row["recipient_key"] is None
                or row["destination_alias_owner"] != row["recipient_participant_id"]
            ):
                raise _data_invalid()
            _validate_trace_alias(
                row["destination_display_name"], row["destination_alias_key"]
            )
            destination = {
                "kind": "participant",
                "participant_key": row["recipient_key"],
                "display_name": row["destination_display_name"],
            }
        elif row["destination_kind"] == "room":
            if (
                row["recipient_participant_id"] is not None
                or row["destination_owner_key"] != "room-system"
                or row["destination_alias_key"] != "room"
                or row["destination_display_name"] != "Room"
            ):
                raise _data_invalid()
            destination = {
                "kind": "room",
                "display_name": row["destination_display_name"],
            }
        else:
            raise _data_invalid()
        _validate_trace_alias(
            row["sender_display_name"],
            row["sender_alias_key"],
            allow_room=row["participant_key"] == "room-system",
        )
        if row["routing_mode"] not in {"legacy_implicit", "explicit"}:
            raise _data_invalid()
        messages.append(
            {
                "id": row["id"],
                "room_sequence_no": room_sequence_no,
                "turn_sequence_no": sequence_no,
                "participant_id": row["participant_id"],
                "participant_key": row["participant_key"],
                "participant_name": row["participant_name"],
                "participant_config_id": config_id,
                "reply_to_id": reply_to_id,
                "reply_to_turn_id": reply_turn_id if reply_to_id is not None else None,
                "reply_to_outside_selected_turn": (
                    reply_to_id is not None and reply_turn_id != turn_id
                ),
                "message_type": row["message_type"],
                "message_text": row["message_text"],
                "created_at": row["created_at"],
                "routing": {
                    "routing_mode": row["routing_mode"],
                    "sender": {
                        "participant_key": row["participant_key"],
                        "display_name": row["sender_display_name"],
                    },
                    "destination": destination,
                },
            }
        )
    return messages, config_ids


def _validate_trace_alias(display: Any, stored_key: Any, *, allow_room: bool = False) -> None:
    if allow_room and display == "Room" and stored_key == "room":
        return
    try:
        accepted, normalized = validate_display_alias(display)
    except ParticipantNameError as exception:
        raise _data_invalid() from exception
    if accepted != display or normalized != stored_key:
        raise _data_invalid()


def _load_events(
    connection: sqlite3.Connection,
    turn_id: int,
    room_id: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    rows = connection.execute(
        """
        SELECT
            event.id,
            event.turn_id,
            event.room_id,
            event.participant_id,
            event.participant_config_id,
            event.tool_invocation_id,
            event.sequence_no,
            event.event_type,
            event.related_message_id,
            event.payload_json,
            event.is_redacted,
            event.redacted_at,
            event.redaction_reason,
            event.created_at,
            participant.id AS found_participant_id,
            participant.participant_key,
            tool.id AS found_tool_invocation_id,
            related.id AS found_related_message_id,
            related.turn_id AS related_message_turn_id,
            related.room_id AS related_message_room_id
        FROM api_events AS event
        LEFT JOIN participants AS participant
          ON participant.id = event.participant_id
        LEFT JOIN tool_invocations AS tool
          ON tool.id = event.tool_invocation_id
        LEFT JOIN messages AS related
          ON related.id = event.related_message_id
        WHERE event.turn_id = ?
        ORDER BY event.sequence_no, event.id
        """,
        (turn_id,),
    ).fetchall()

    seen_sequence_numbers: set[int] = set()
    config_ids: set[int] = set()
    events: list[dict[str, Any]] = []
    for row in rows:
        sequence_no = row["sequence_no"]
        if (
            row["room_id"] != room_id
            or sequence_no <= 0
            or sequence_no in seen_sequence_numbers
        ):
            raise _data_invalid()
        seen_sequence_numbers.add(sequence_no)
        if row["participant_id"] is not None and row["found_participant_id"] is None:
            raise _data_invalid()
        if (
            row["tool_invocation_id"] is not None
            and row["found_tool_invocation_id"] is None
        ):
            raise _data_invalid()

        related_id = row["related_message_id"]
        related_turn_id = row["related_message_turn_id"]
        if related_id is not None and (
            row["found_related_message_id"] is None
            or row["related_message_room_id"] != room_id
        ):
            raise _data_invalid()

        config_id = row["participant_config_id"]
        if config_id is not None:
            if row["participant_id"] is None:
                raise _data_invalid()
            config_ids.add(config_id)

        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exception:
            raise _data_invalid() from exception

        events.append(
            {
                "id": row["id"],
                "turn_id": row["turn_id"],
                "room_id": row["room_id"],
                "participant_id": row["participant_id"],
                "participant_key": row["participant_key"],
                "participant_config_id": config_id,
                "tool_invocation_id": row["tool_invocation_id"],
                "sequence_no": sequence_no,
                "event_type": row["event_type"],
                "related_message_id": related_id,
                "related_message_turn_id": (
                    related_turn_id if related_id is not None else None
                ),
                "related_message_outside_selected_turn": (
                    related_id is not None and related_turn_id != turn_id
                ),
                "raw_payload": payload,
                "is_redacted": bool(row["is_redacted"]),
                "redacted_at": row["redacted_at"],
                "redaction_reason": row["redaction_reason"],
                "created_at": row["created_at"],
            }
        )
    return events, config_ids


def _load_configurations(
    connection: sqlite3.Connection,
    config_ids: set[int],
) -> list[dict[str, Any]]:
    if not config_ids:
        return []
    placeholders = ",".join("?" for _ in config_ids)
    rows = connection.execute(
        f"""
        SELECT
            config.id,
            config.participant_id,
            participant.id AS found_participant_id,
            participant.participant_key,
            config.provider,
            config.model,
            config.config_label,
            config.system_instructions,
            config.settings_json,
            config.tools_json,
            config.created_at
        FROM participant_configs AS config
        LEFT JOIN participants AS participant
          ON participant.id = config.participant_id
        WHERE config.id IN ({placeholders})
        ORDER BY config.id
        """,
        tuple(sorted(config_ids)),
    ).fetchall()
    if len(rows) != len(config_ids):
        raise _data_invalid()

    configurations: list[dict[str, Any]] = []
    for row in rows:
        if row["found_participant_id"] is None:
            raise _data_invalid()
        settings = _parse_nullable_json(row["settings_json"])
        tools = _parse_nullable_json(row["tools_json"])
        projected_settings, settings_omissions = project_trace_json(
            settings, ("settings",)
        )
        projected_tools, tools_omissions = project_trace_json(tools, ("tools",))
        configurations.append(
            {
                "id": row["id"],
                "participant_id": row["participant_id"],
                "participant_key": row["participant_key"],
                "provider": row["provider"],
                "model": row["model"],
                "config_label": row["config_label"],
                "system_instructions": row["system_instructions"],
                "settings": projected_settings,
                "tools": projected_tools,
                "omitted_json_pointers": settings_omissions + tools_omissions,
                "created_at": row["created_at"],
            }
        )
    return configurations


def _parse_nullable_json(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exception:
        raise _data_invalid() from exception


def _validate_configuration_references(
    messages: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    configurations: dict[int, dict[str, Any]],
) -> None:
    for item in (*tuple(messages), *tuple(events)):
        config_id = item["participant_config_id"]
        if config_id is None:
            continue
        config = configurations.get(config_id)
        if config is None or config["participant_id"] != item["participant_id"]:
            raise _data_invalid()


def _validate_event_family(
    connection: sqlite3.Connection,
    turn: sqlite3.Row,
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    configurations: dict[int, dict[str, Any]],
) -> None:
    """Validate provider family, state cardinality, and correlations."""

    recognized = [event for event in events if event["event_type"] in RECOGNIZED_EVENTS]
    if not recognized:
        return
    families = {
        "google" if event["event_type"].startswith("google.") else "openai"
        for event in recognized
    }
    if len(families) != 1:
        raise _data_invalid()
    family = next(iter(families))
    request = [event for event in recognized if event["event_type"] in REQUEST_EVENTS]
    terminal = [event for event in recognized if event["event_type"] in TERMINAL_EVENTS]
    if len(request) != 1 or request[0]["sequence_no"] != 1:
        raise _data_invalid()
    status = turn["status"]
    if status == "open":
        if terminal:
            raise _data_invalid()
    elif status in {"completed", "failed", "cancelled"}:
        if len(terminal) != 1 or terminal[0]["sequence_no"] != 2:
            raise _data_invalid()
    else:
        raise _data_invalid()
    redacted = any(event["is_redacted"] for event in recognized)
    expected_key = "gemini" if family == "google" else "helios"
    config = configurations.get(request[0]["participant_config_id"])
    peter_messages = [
        message for message in messages
        if message["participant_key"] == "peter" and message["message_type"] == "chat"
    ]
    if (
        request[0]["participant_key"] != expected_key
        or config is None
        or config["participant_key"] != expected_key
        or config["provider"] != family
        or len(peter_messages) != 1
        or request[0]["related_message_id"] != peter_messages[0]["id"]
        or request[0]["related_message_turn_id"] != turn["id"]
    ):
        raise _data_invalid()
    peter_route = peter_messages[0]["routing"]
    peter_destination = peter_route["destination"]
    if (
        peter_messages[0]["turn_sequence_no"] != 1
        or peter_messages[0]["participant_config_id"] is not None
        or peter_route["routing_mode"] != "explicit"
        or peter_destination.get("kind") != "participant"
        or peter_destination.get("participant_key") != expected_key
    ):
        raise _data_invalid()
    if redacted:
        if status == "open":
            return
        if (
            terminal[0]["participant_key"] != expected_key
            or terminal[0]["participant_config_id"] != request[0]["participant_config_id"]
        ):
            raise _data_invalid()
        if status == "completed":
            ai_messages = [
                message for message in messages
                if message["participant_key"] == expected_key
                and message["message_type"] == "chat"
            ]
            if (
                terminal[0]["event_type"] not in RESPONSE_EVENTS
                or len(ai_messages) != 1
                or terminal[0]["related_message_id"] != ai_messages[0]["id"]
            ):
                raise _data_invalid()
            response = ai_messages[0]
            destination = response["routing"]["destination"]
            if (
                response["turn_sequence_no"] != 2
                or response["reply_to_id"] != peter_messages[0]["id"]
                or response["routing"]["routing_mode"] != "explicit"
                or destination.get("kind") != "participant"
                or destination.get("participant_key") != "peter"
                or response["participant_config_id"] != request[0]["participant_config_id"]
            ):
                raise _data_invalid()
        elif status == "failed" and (
            terminal[0]["event_type"] not in ERROR_EVENTS
            or terminal[0]["related_message_id"] != peter_messages[0]["id"]
        ):
            raise _data_invalid()
        return
    request_payload = request[0]["raw_payload"]
    try:
        if family == "google":
            validate_recorded_google_shared_request_payload(request_payload)
        else:
            validate_recorded_openai_shared_request_payload(request_payload)
        boundary = request_payload["local_context"]["room_sequence_boundary"]
        expected_history = load_provider_history(
            connection,
            room_id=turn["room_id"],
            boundary=boundary,
            provider_participant_key=expected_key,
        )
        actual_history = list(
            request_payload["request"]["contents" if family == "google" else "input"]
        )
        selected = request_payload["local_context"]["memory_retrieval"]["selected"]
        if selected:
            del actual_history[-2]
        if actual_history != expected_history:
            raise ValueError("recorded provider history mismatch")
    except (ProviderHistoryError, TypeError, ValueError, IndexError) as exception:
        raise _data_invalid() from exception
    if family == "google":
        model = request[0]["raw_payload"]["request"].get("model")
        slug = (
            re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
            if isinstance(model, str)
            else ""
        ) or "model"
        label = config["config_label"]
        prefix = f"seed-memory-google-{slug}-v"
        if (
            config["model"] != model
            or config["system_instructions"] != GEMINI_SYSTEM_INSTRUCTIONS
            or config["settings"] != gemini_recorded_settings()
            or config["tools"] != []
            or not isinstance(label, str)
            or not label.startswith(prefix)
            or not label[len(prefix):].isdigit()
            or int(label[len(prefix):]) <= 0
        ):
            raise _data_invalid()
    else:
        model = request[0]["raw_payload"]["request"].get("model")
        label = config["config_label"]
        role = model.removeprefix("gpt-5.6-") if isinstance(model, str) else ""
        role = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "model"
        prefix = f"seed-memory-openai-{role}-v"
        if (
            config["model"] != model
            or config["system_instructions"] != OPENAI_SYSTEM_INSTRUCTIONS
            or config["settings"] != OPENAI_RESPONSE_SETTINGS
            or config["tools"] != OPENAI_RESPONSE_TOOLS
            or not isinstance(label, str)
            or not label.startswith(prefix)
            or not label[len(prefix):].isdigit()
            or int(label[len(prefix):]) <= 0
        ):
            raise _data_invalid()
    if status == "open":
        return
    if (
        terminal[0]["participant_key"] != expected_key
        or terminal[0]["participant_config_id"] != request[0]["participant_config_id"]
    ):
        raise _data_invalid()
    if status == "completed":
        ai_messages = [
            message for message in messages
            if message["participant_key"] == expected_key and message["message_type"] == "chat"
        ]
        if (
            terminal[0]["event_type"] not in RESPONSE_EVENTS
            or len(ai_messages) != 1
            or terminal[0]["related_message_id"] != ai_messages[0]["id"]
        ):
            raise _data_invalid()
        response = ai_messages[0]
        destination = response["routing"]["destination"]
        if (
            response["turn_sequence_no"] != 2
            or response["reply_to_id"] != peter_messages[0]["id"]
            or response["routing"]["routing_mode"] != "explicit"
            or destination.get("kind") != "participant"
            or destination.get("participant_key") != "peter"
            or response["participant_config_id"] != request[0]["participant_config_id"]
        ):
            raise _data_invalid()
    elif status == "failed":
        if (
            terminal[0]["event_type"] not in ERROR_EVENTS
            or terminal[0]["related_message_id"] != peter_messages[0]["id"]
        ):
            raise _data_invalid()
    elif status != "cancelled":
        raise _data_invalid()


def _project_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_payload = event["raw_payload"]
    event_type = event["event_type"]
    redacted = event["is_redacted"]
    if redacted:
        payload, omissions = None, [""]
    else:
        _validate_recognized_payload(event_type, raw_payload)

    if event_type == GOOGLE_RESPONSE_EVENT and not redacted:
        payload, omissions = _project_google_response_envelope(raw_payload)
    elif event_type == GOOGLE_ERROR_EVENT and not redacted:
        payload, omissions = _project_google_error_payload(raw_payload)
    elif event_type in ERROR_EVENTS and not redacted:
        payload, omissions = _project_error_payload(raw_payload)
    elif not redacted:
        payload, omissions = project_trace_json(raw_payload)

    return {
        "id": event["id"],
        "sequence_no": event["sequence_no"],
        "event_type": event_type,
        "participant_id": event["participant_id"],
        "participant_key": event["participant_key"],
        "participant_config_id": event["participant_config_id"],
        "related_message_id": event["related_message_id"],
        "related_message_turn_id": event["related_message_turn_id"],
        "related_message_outside_selected_turn": event[
            "related_message_outside_selected_turn"
        ],
        "tool_invocation_id": event["tool_invocation_id"],
        "is_redacted": redacted,
        "redacted_at": event["redacted_at"],
        "redaction_reason": event["redaction_reason"],
        "created_at": event["created_at"],
        "payload": payload,
        "omitted_json_pointers": omissions,
    }


def _validate_recognized_payload(event_type: str, payload: Any) -> None:
    if event_type == REQUEST_EVENT:
        try:
            validate_recorded_openai_shared_request_payload(payload)
        except (TypeError, ValueError) as exception:
            raise _data_invalid() from exception
    elif event_type == RESPONSE_EVENT:
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
            raise _data_invalid()
    elif event_type == ERROR_EVENT:
        provider_error = (
            isinstance(payload, dict) and isinstance(payload.get("error"), dict)
        )
        unusable_response = (
            isinstance(payload, dict)
            and "reason" in payload
            and isinstance(payload.get("response"), dict)
        )
        if provider_error == unusable_response:
            raise _data_invalid()
    elif event_type == GOOGLE_REQUEST_EVENT:
        _validate_google_request_payload(payload)
    elif event_type == GOOGLE_RESPONSE_EVENT:
        if not isinstance(payload, dict) or set(payload) != {"response"}:
            raise _data_invalid()
        response = validate_bounded_stored_response(payload["response"])
        validate_stored_success_response(response)
    elif event_type == GOOGLE_ERROR_EVENT:
        _validate_google_error_payload(payload)


def _validate_google_request_payload(payload: Any) -> None:
    try:
        validate_recorded_google_shared_request_payload(payload)
    except (TypeError, ValueError) as exception:
        raise _data_invalid() from exception


def _validate_google_error_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise _data_invalid()
    if set(payload) == {"error"}:
        error = payload["error"]
        if not isinstance(error, dict):
            raise _data_invalid()
        reason = error.get("reason")
        if reason == "gemini_provider_failure":
            required = {
                "error_class": "ProviderError",
                "reason": reason,
                "summary": "The Gemini provider request failed.",
            }
            optional = {"http_status", "provider_error_code", "provider_request_id"}
        elif reason == "gemini_provider_timeout":
            required = {
                "error_class": "TimeoutError",
                "reason": reason,
                "summary": "The Gemini provider request timed out.",
            }
            optional = {"http_status", "provider_error_code", "provider_request_id"}
        elif reason == "gemini_provider_response_serialization_failed":
            required = {
                "error_class": "ResponseSerializationError",
                "reason": reason,
                "summary": "The Gemini provider response could not be recorded safely.",
            }
            optional = set()
        else:
            raise _data_invalid()
        if not set(error).issubset(set(required) | optional):
            raise _data_invalid()
        if any(error.get(key) != value for key, value in required.items()):
            raise _data_invalid()
        if "http_status" in error and (
            type(error["http_status"]) is not int or not 100 <= error["http_status"] <= 599
        ):
            raise _data_invalid()
        for key, limit in (("provider_error_code", 128), ("provider_request_id", 256)):
            if key in error and (
                not isinstance(error[key], str)
                or re.fullmatch(rf"[A-Za-z0-9_.:-]{{1,{limit}}}", error[key]) is None
            ):
                raise _data_invalid()
        return
    if set(payload) != {"error", "response"}:
        raise _data_invalid()
    error = payload["error"]
    if (
        not isinstance(error, dict)
        or set(error) != {"failure_kind", "reason", "summary"}
        or error.get("failure_kind") not in GEMINI_FAILURE_KINDS
        or error.get("reason") != "gemini_provider_unusable_response"
        or error.get("summary") != "The Gemini provider returned an unusable response."
    ):
        raise _data_invalid()
    validate_bounded_stored_response(payload["response"])


def _project_google_response_envelope(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    response, omissions = _project_google_response(
        payload["response"], ("response",), allow_visible_text=True
    )
    return {"response": response}, omissions


def _project_google_error_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    error = dict(payload["error"])
    if "response" not in payload:
        return {"error": error}, []
    response, omissions = _project_google_response(
        payload["response"], ("response",), allow_visible_text=False
    )
    return {"error": error, "response": response}, omissions


def _project_google_response(
    response: dict[str, Any],
    base: tuple[str | int, ...],
    *,
    allow_visible_text: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Project only the closed Gemini Trace surface; omit extensions wholesale."""

    result: dict[str, Any] = {}
    omissions: list[str] = []
    trace_top = {"candidates", "model_version", "prompt_feedback", "usage_metadata"}
    for key, value in response.items():
        path = (*base, key)
        if key not in trace_top:
            omissions.append(_json_pointer(path))
            continue
        if key == "candidates" and isinstance(value, list):
            candidates: list[Any] = []
            for index, candidate in enumerate(value):
                if not isinstance(candidate, dict):
                    omissions.append(_json_pointer((*path, index)))
                    continue
                candidate_result: dict[str, Any] = {}
                for candidate_key, candidate_value in candidate.items():
                    candidate_path = (*path, index, candidate_key)
                    if candidate_key in {"finish_reason", "index"}:
                        projected, child = project_trace_json(candidate_value, candidate_path)
                        candidate_result[candidate_key] = projected
                        omissions.extend(child)
                    elif candidate_key == "safety_ratings":
                        projected, child = _project_google_safety_ratings(
                            candidate_value, candidate_path
                        )
                        candidate_result[candidate_key] = projected
                        omissions.extend(child)
                    elif candidate_key == "content" and isinstance(candidate_value, dict):
                        content_result: dict[str, Any] = {}
                        for content_key in candidate_value:
                            if content_key not in {"role", "parts"}:
                                omissions.append(
                                    _json_pointer((*candidate_path, content_key))
                                )
                        if "role" in candidate_value:
                            content_result["role"] = candidate_value["role"]
                        parts_result: list[dict[str, Any]] = []
                        raw_parts = candidate_value.get("parts")
                        if not isinstance(raw_parts, list):
                            omissions.append(_json_pointer((*candidate_path, "parts")))
                            raw_parts = []
                        for part_index, part in enumerate(raw_parts):
                            part_path = (*candidate_path, "parts", part_index)
                            if not isinstance(part, dict):
                                omissions.append(_json_pointer(part_path))
                                continue
                            part_result: dict[str, Any] = {}
                            is_thought = part.get("thought") is True
                            for part_key, part_value in part.items():
                                item_path = (*part_path, part_key)
                                if part_key == "thought_signature_b64" or (
                                    part_key == "text"
                                    and (is_thought or not allow_visible_text)
                                ):
                                    omissions.append(_json_pointer(item_path))
                                elif part_key in {"text", "thought"}:
                                    part_result[part_key] = part_value
                                else:
                                    omissions.append(_json_pointer(item_path))
                            parts_result.append(part_result)
                        content_result["parts"] = parts_result
                        candidate_result["content"] = content_result
                    else:
                        omissions.append(_json_pointer(candidate_path))
                candidates.append(candidate_result)
            result[key] = candidates
        elif key == "usage_metadata" and isinstance(value, dict):
            usage: dict[str, Any] = {}
            for usage_key, usage_value in value.items():
                usage_path = (*path, usage_key)
                if usage_key in GEMINI_TOKEN_COUNT_FIELDS:
                    usage[usage_key] = usage_value
                else:
                    omissions.append(_json_pointer(usage_path))
            result[key] = usage
        elif key == "prompt_feedback" and isinstance(value, dict):
            feedback: dict[str, Any] = {}
            for feedback_key, feedback_value in value.items():
                feedback_path = (*path, feedback_key)
                if feedback_key == "block_reason":
                    projected, child = project_trace_json(feedback_value, feedback_path)
                    feedback[feedback_key] = projected
                    omissions.extend(child)
                elif feedback_key == "safety_ratings":
                    projected, child = _project_google_safety_ratings(
                        feedback_value, feedback_path
                    )
                    feedback[feedback_key] = projected
                    omissions.extend(child)
                else:
                    omissions.append(_json_pointer(feedback_path))
            result[key] = feedback
        elif key in {"model_version"}:
            result[key] = value
        else:
            omissions.append(_json_pointer(path))
    return result, sorted(set(omissions))


def _project_google_safety_ratings(
    value: Any,
    base: tuple[str | int, ...],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], [_json_pointer(base)]
    projected: list[dict[str, Any]] = []
    omissions: list[str] = []
    for index, rating in enumerate(value):
        path = (*base, index)
        if not isinstance(rating, dict):
            omissions.append(_json_pointer(path))
            continue
        item: dict[str, Any] = {}
        for key, child in rating.items():
            child_path = (*path, key)
            if key in {"category", "probability", "blocked"}:
                item[key] = child
            else:
                omissions.append(_json_pointer(child_path))
        projected.append(item)
    return projected, omissions


def _project_error_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if isinstance(payload.get("error"), dict):
        projected, omissions = _allowlisted_mapping(
            payload["error"],
            ("reason", "error_class", "http_status", "provider_error_code",
             "provider_request_id", "summary"),
            ("error",),
        )
        for key in payload:
            if key != "error":
                omissions.append(_json_pointer((key,)))
        return {"error": projected}, _unique(omissions)

    reason, reason_omissions = project_trace_json(payload["reason"], ("reason",))
    response, response_omissions = _allowlisted_mapping(
        payload["response"],
        ("id", "status", "model", "usage", "incomplete_details",
         "service_tier", "output_text"),
        ("response",),
    )
    omissions = reason_omissions + response_omissions
    for key in payload:
        if key not in {"reason", "response"}:
            omissions.append(_json_pointer((key,)))
    return {"reason": reason, "response": response}, _unique(omissions)


def _allowlisted_mapping(
    value: dict[str, Any],
    allowed: tuple[str, ...],
    base_path: tuple[str | int, ...],
) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {}
    omissions: list[str] = []
    allowed_set = set(allowed)
    for key, item in value.items():
        path = (*base_path, key)
        if key not in allowed_set:
            omissions.append(_json_pointer(path))
            continue
        projected, child_omissions = project_trace_json(item, path)
        result[key] = projected
        omissions.extend(child_omissions)
    return result, _unique(omissions)


def project_trace_json(
    value: Any,
    base_path: tuple[str | int, ...] = (),
) -> tuple[Any, list[str]]:
    """Recursively omit secrets and opaque reasoning for browser display."""

    omissions: list[str] = []

    def visit(item: Any, path: tuple[str | int, ...]) -> Any:
        if isinstance(item, list):
            return [visit(child, (*path, index)) for index, child in enumerate(item)]
        if not isinstance(item, dict):
            return item

        reasoning_item = any(
            _normalize_key(key) == "type" and _normalized_value(child) == "reasoning"
            for key, child in item.items()
        )
        gemini_thought_part = item.get("thought") is True
        projected: dict[str, Any] = {}
        summary_retained = False
        for key, child in item.items():
            normalized = _normalize_key(key)
            child_path = (*path, key)
            omit = (
                _is_secret_key(normalized)
                or normalized in {
                    "encryptedcontent", "reasoningtext", "thoughtsignature",
                    "thoughtsignatureb64",
                }
                or (gemini_thought_part and normalized == "text")
                or (reasoning_item and normalized not in {"id", "type", "status", "summary"})
            )
            if omit:
                omissions.append(_json_pointer(child_path))
                continue
            projected[key] = visit(child, child_path)
            if reasoning_item and normalized == "summary":
                summary_retained = True
        if reasoning_item and summary_retained:
            projected["summary_label"] = "Provider-generated reasoning summary"
        return projected

    return visit(value, base_path), _unique(omissions)


def _normalize_key(key: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", key).casefold()
        if character.isalnum()
    )


def _normalized_value(value: Any) -> str:
    return _normalize_key(value) if isinstance(value, str) else ""


def _is_secret_key(normalized: str) -> bool:
    exact = {
        "authorization",
        "proxyauthorization",
        "headers",
        "cookie",
        "setcookie",
        "apikey",
        "openaiapikey",
        "xapikey",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "password",
        "secret",
        "token",
    }
    return normalized in exact or normalized.endswith(
        ("apikey", "secret", "password", "token")
    )


def _json_pointer(path: tuple[str | int, ...]) -> str:
    def escape(part: str | int) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")

    return "" if not path else "/" + "/".join(escape(part) for part in path)


def _unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def _build_recorded_request(event: dict[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    redacted = event["is_redacted"]
    return {
        "event_id": event["id"],
        "event_sequence_no": event["sequence_no"],
        "is_redacted": redacted,
        "redaction_reason": event["redaction_reason"],
        "request": payload.get("request") if not redacted else None,
        "local_context": payload.get("local_context") if not redacted else None,
        "omitted_json_pointers": event["omitted_json_pointers"],
    }


def _requested_model(recorded_request: dict[str, Any] | None) -> str | None:
    if recorded_request is None or recorded_request["is_redacted"]:
        return None
    request = recorded_request.get("request")
    model = request.get("model") if isinstance(request, dict) else None
    return model if isinstance(model, str) else None


def _validate_raw_inherited_memory(
    request_event: dict[str, Any],
    messages: list[dict[str, Any]],
) -> None:
    """Validate inherited-memory evidence before privacy projection."""

    if request_event["is_redacted"]:
        return
    payload = request_event["raw_payload"]
    _validate_recognized_payload(request_event["event_type"], payload)
    local_context = payload["local_context"]
    if "memory_retrieval" not in local_context:
        return
    _validate_inherited_memory_payload(
        payload["request"],
        local_context,
        messages,
        request_event,
    )


def _build_inherited_memory(
    recorded_request: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    request_event: dict[str, Any] | None,
) -> dict[str, Any]:
    not_recorded = {
        "state": "not_recorded",
        "unavailable_reason": None,
        "retrieval": None,
        "context": None,
    }
    if recorded_request is None:
        return not_recorded
    if recorded_request["is_redacted"]:
        return {
            "state": "unavailable",
            "unavailable_reason": "request_redacted",
            "retrieval": None,
            "context": None,
        }

    request = recorded_request.get("request")
    omissions = recorded_request.get("omitted_json_pointers", [])
    input_omitted = any(
        pointer in {"/request/input", "/request/contents"}
        or pointer.startswith("/request/input/")
        or pointer.startswith("/request/contents/")
        for pointer in omissions
    )
    if input_omitted:
        return {
            "state": "unavailable",
            "unavailable_reason": "request_input_unavailable",
            "retrieval": None,
            "context": None,
        }

    local_context = recorded_request.get("local_context")
    if not isinstance(local_context, dict) or "memory_retrieval" not in local_context:
        return not_recorded
    retrieval, context = _validate_inherited_memory_payload(
        request,
        local_context,
        messages,
        request_event,
    )

    return {
        "state": "recorded",
        "unavailable_reason": None,
        "retrieval": retrieval,
        "context": context,
    }


def _validate_inherited_memory_payload(
    request: Any,
    local_context: dict[str, Any],
    messages: list[dict[str, Any]],
    request_event: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    retrieval = local_context["memory_retrieval"]
    if not isinstance(retrieval, dict):
        raise _data_invalid()
    if not isinstance(request, dict):
        raise _data_invalid()
    is_gemini = "contents" in request
    input_key = "contents" if is_gemini else "input"
    if input_key not in request:
        raise _data_invalid()
    provider_input = request[input_key]
    if not isinstance(provider_input, list) or not provider_input:
        raise _data_invalid()

    selected = _validate_memory_retrieval(retrieval)
    trigger_id = retrieval["query_source_message_id"]
    triggering = [message for message in messages if message["id"] == trigger_id]
    request_participant_key = (
        request_event.get("participant_key")
        if isinstance(request_event, dict)
        else None
    )
    triggering_participant_key = (
        triggering[0].get("participant_key") if len(triggering) == 1 else None
    )
    if (
        request_event is None
        or not isinstance(request_participant_key, str)
        or request_participant_key != ("gemini" if is_gemini else "helios")
        or retrieval["owner_participant_id"] != request_event["participant_id"]
        or trigger_id != request_event["related_message_id"]
        or len(triggering) != 1
        or not isinstance(triggering_participant_key, str)
        or triggering_participant_key.casefold() != "peter"
        or tokenize_memory_query(triggering[0]["message_text"])
        != retrieval["query_terms"]
        or local_context.get("trigger_message_id") != trigger_id
        or local_context.get("room_sequence_boundary")
        != triggering[0]["room_sequence_no"]
    ):
        raise _data_invalid()

    expected_trigger = (
        {"role": "user", "parts": [{"text": triggering[0]["message_text"]}]}
        if is_gemini
        else {"role": "user", "content": triggering[0]["message_text"]}
    )
    if provider_input[-1] != expected_trigger:
        raise _data_invalid()

    candidates = [
        (index, item)
        for index, item in enumerate(provider_input)
        if isinstance(item, dict) and (
            (
                not is_gemini
                and isinstance(item.get("content"), str)
                and item["content"].startswith(INHERITED_MEMORY_HEADER)
            )
            or (
                is_gemini
                and set(item) == {"role", "parts"}
                and isinstance(item.get("parts"), list)
                and len(item["parts"]) == 1
                and isinstance(item["parts"][0], dict)
                and set(item["parts"][0]) == {"text"}
                and isinstance(item["parts"][0].get("text"), str)
                and item["parts"][0]["text"].startswith(INHERITED_MEMORY_HEADER)
            )
        )
    ]
    context: dict[str, Any] | None = None
    if selected:
        expected_position = len(provider_input) - 2
        if (
            len(candidates) != 1
            or candidates[0][0] != expected_position
            or set(candidates[0][1]) != ({"role", "parts"} if is_gemini else {"role", "content"})
            or candidates[0][1].get("role") != "user"
        ):
            raise _data_invalid()
        context_content = (
            candidates[0][1]["parts"][0]["text"]
            if is_gemini
            else candidates[0][1]["content"]
        )
        encoded_context = context_content[len(INHERITED_MEMORY_HEADER) :]
        try:
            context = json.loads(encoded_context)
        except (json.JSONDecodeError, TypeError) as exception:
            raise _data_invalid() from exception
        if _canonical_json(context) != encoded_context:
            raise _data_invalid()
        _validate_inherited_context(context, selected, retrieval)
    elif candidates:
        raise _data_invalid()

    return retrieval, context


def _validate_memory_retrieval(retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    if (
        set(retrieval) != MEMORY_RETRIEVAL_FIELDS
        or retrieval.get("retriever_version") != RETRIEVER_VERSION
        or not _positive_int(retrieval.get("owner_participant_id"))
        or not _positive_int(retrieval.get("query_source_message_id"))
        or retrieval.get("result_limit") != RESULT_LIMIT
        or retrieval.get("text_budget_chars") != TEXT_BUDGET_CHARS
        or not _nonnegative_int(retrieval.get("omitted_for_budget"))
    ):
        raise _data_invalid()
    query_terms = retrieval.get("query_terms")
    fts_query = retrieval.get("fts_query")
    selected = retrieval.get("selected")
    if (
        not isinstance(query_terms, list)
        or any(not isinstance(term, str) for term in query_terms)
        or len(query_terms) > 24
        or (fts_query is not None and not isinstance(fts_query, str))
        or build_fts_query(query_terms) != fts_query
        or not isinstance(selected, list)
        or len(selected) > RESULT_LIMIT
        or (not query_terms and selected)
    ):
        raise _data_invalid()

    seen_ids: set[int] = set()
    seen_stable_ids: set[str] = set()
    for index, record in enumerate(selected, start=1):
        if not isinstance(record, dict) or set(record) != MEMORY_SELECTION_FIELDS:
            raise _data_invalid()
        memory_id = record.get("seed_memory_id")
        stable_id = record.get("stable_id")
        if (
            record.get("rank") != index
            or not _positive_int(memory_id)
            or not isinstance(stable_id, str)
            or not stable_id.strip()
            or memory_id in seen_ids
            or stable_id in seen_stable_ids
            or not _positive_int(record.get("seed_batch_id"))
            or not _lower_sha256(record.get("source_content_sha256"))
            or not isinstance(record.get("source_label"), str)
            or not record["source_label"].strip()
            or not isinstance(record.get("source_locator"), str)
            or not record["source_locator"].strip()
            or not _lower_sha256(record.get("memory_text_sha256"))
            or type(record.get("exact_topic_match")) is not bool
            or not _finite_number(record.get("topic_match_weight_sum"), minimum=0.0)
            or (
                not record.get("exact_topic_match")
                and record.get("topic_match_weight_sum") != 0.0
            )
            or (
                record.get("fts_bm25") is not None
                and not _finite_number(record.get("fts_bm25"))
            )
            or (
                not record.get("exact_topic_match")
                and record.get("fts_bm25") is None
            )
            or not _finite_number(record.get("importance"), minimum=0.0, maximum=1.0)
            or not _finite_number(record.get("confidence"), minimum=0.0, maximum=1.0)
        ):
            raise _data_invalid()
        seen_ids.add(memory_id)
        seen_stable_ids.add(stable_id)
    if selected != sorted(selected, key=_recorded_memory_sort_key):
        raise _data_invalid()
    return selected


def _recorded_memory_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    score = record["fts_bm25"]
    return (
        -int(record["exact_topic_match"]),
        -float(record["topic_match_weight_sum"]),
        score is None,
        float(score) if score is not None else 0.0,
        -float(record["importance"]),
        -float(record["confidence"]),
        record["seed_memory_id"],
    )


def _validate_inherited_context(
    context: Any,
    selected: list[dict[str, Any]],
    retrieval: dict[str, Any],
) -> None:
    if (
        not isinstance(context, dict)
        or set(context) != {"kind", "provenance_notice", "retriever_version", "records"}
        or context.get("kind") != "inherited_seed_memory_context"
        or context.get("provenance_notice") != INHERITED_MEMORY_PROVENANCE
        or context.get("retriever_version") != retrieval["retriever_version"]
        or not isinstance(context.get("records"), list)
        or len(context["records"]) != len(selected)
    ):
        raise _data_invalid()
    total_chars = 0
    for audit, record in zip(selected, context["records"]):
        if (
            not isinstance(record, dict)
            or set(record) != {
                "seed_memory_id",
                "stable_id",
                "source_label",
                "memory_text",
            }
            or record.get("seed_memory_id") != audit["seed_memory_id"]
            or record.get("stable_id") != audit["stable_id"]
            or record.get("source_label") != audit["source_label"]
            or not isinstance(record.get("memory_text"), str)
            or hashlib.sha256(record["memory_text"].encode("utf-8")).hexdigest()
            != audit["memory_text_sha256"]
        ):
            raise _data_invalid()
        total_chars += len(record["memory_text"])
    if total_chars > retrieval["text_budget_chars"]:
        raise _data_invalid()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _lower_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _finite_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return (
        math.isfinite(converted)
        and (minimum is None or converted >= minimum)
        and (maximum is None or converted <= maximum)
    )


def _build_provider_outcome(
    event: dict[str, Any],
    requested_model: str | None,
) -> dict[str, Any]:
    common = {
        "event_id": event["id"],
        "event_type": event["event_type"],
        "is_redacted": event["is_redacted"],
        "redaction_reason": event["redaction_reason"],
        "requested_model": requested_model,
        "omitted_json_pointers": event["omitted_json_pointers"],
    }
    if event["is_redacted"]:
        return common

    if event["event_type"] == RESPONSE_EVENT:
        response = event["payload"]["response"]
        return {
            **common,
            "response_id": response.get("id"),
            "status": response.get("status"),
            "resolved_model": response.get("model"),
            "usage": response.get("usage"),
            "incomplete_details": response.get("incomplete_details"),
            "service_tier": response.get("service_tier"),
            "output_text": _visible_output_text(response),
            "error": None,
        }

    if event["event_type"] == GOOGLE_RESPONSE_EVENT:
        response = event["payload"]["response"]
        candidate = response["candidates"][0]
        return {
            **common,
            "response_id": None,
            "status": candidate.get("finish_reason"),
            "resolved_model": response.get("model_version"),
            "usage": response.get("usage_metadata"),
            "safety_ratings": candidate.get("safety_ratings"),
            "output_text": _visible_gemini_output(candidate.get("content")),
            "error": None,
        }

    if event["event_type"] == GOOGLE_ERROR_EVENT:
        payload = event["payload"]
        if "response" not in payload:
            return {**common, "error": payload["error"]}
        response = payload["response"]
        candidate = (
            response.get("candidates", [None])[0]
            if isinstance(response.get("candidates"), list) and response["candidates"]
            else None
        )
        return {
            **common,
            "status": candidate.get("finish_reason") if isinstance(candidate, dict) else None,
            "resolved_model": response.get("model_version"),
            "usage": response.get("usage_metadata"),
            "error": payload["error"],
            "unusable_response": response,
        }

    payload = event["payload"]
    if "error" in payload:
        return {**common, "error": payload["error"]}
    response = payload["response"]
    return {
        **common,
        "status": response.get("status"),
        "resolved_model": response.get("model"),
        "usage": response.get("usage"),
        "incomplete_details": response.get("incomplete_details"),
        "service_tier": response.get("service_tier"),
        "error": {"reason": payload["reason"]},
        "unusable_response": response,
    }


def _visible_output_text(response: dict[str, Any]) -> str | None:
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    visible: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    visible.append(part["text"])
    return "\n".join(visible) if visible else None


def _visible_gemini_output(content: Any) -> str | None:
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        return None
    visible = [
        part["text"]
        for part in content["parts"]
        if isinstance(part, dict)
        and part.get("thought") is not True
        and isinstance(part.get("text"), str)
    ]
    return "".join(visible) if visible else None
