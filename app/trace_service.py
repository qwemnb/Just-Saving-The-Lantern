"""Read-only, historical Trace v1 snapshots for the main Helios room."""

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

from .database import EXPECTED_SCHEMA_MIGRATION
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
TERMINAL_EVENTS = frozenset((RESPONSE_EVENT, ERROR_EVENT))
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


def open_trace_database(database_path: Path | str) -> sqlite3.Connection:
    """Open an existing SQLite file in enforced read-only private-cache mode."""

    connection: sqlite3.Connection | None = None
    try:
        path = Path(database_path).expanduser()
        if not path.parent.is_dir() or not path.is_file():
            raise _database_unavailable()
        uri = f"{path.resolve().as_uri()}?mode=ro&cache=private"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise _database_unavailable()
        return connection
    except TraceServiceError:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise
    except (OSError, sqlite3.Error) as exception:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise _database_unavailable() from exception


def load_trace(database_path: Path | str, turn_id: int | None = None) -> dict[str, Any]:
    """Load one deterministic Trace v1 document from a single snapshot."""

    connection = open_trace_database(database_path)
    try:
        connection.execute("BEGIN")
        _validate_schema_marker(connection)
        return _load_snapshot(connection, turn_id)
    except TraceServiceError:
        raise
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
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


def _validate_schema_marker(connection: sqlite3.Connection) -> None:
    try:
        rows = connection.execute(
            """
            SELECT migration_no, schema_label
            FROM schema_migrations
            ORDER BY migration_no
            """
        ).fetchall()
    except sqlite3.Error as exception:
        raise _database_unavailable() from exception
    versions = [(row["migration_no"], row["schema_label"]) for row in rows]
    if versions != [EXPECTED_SCHEMA_MIGRATION]:
        raise _database_unavailable()


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
    request_events = [
        event for event in events if event["event_type"] == REQUEST_EVENT
    ]
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

    projected_events = [_project_event(event) for event in events]
    request_events = [
        event for event in projected_events if event["event_type"] == REQUEST_EVENT
    ]
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
        "trace_version": 1,
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
            reply.room_id AS reply_room_id
        FROM messages AS m
        LEFT JOIN participants AS participant ON participant.id = m.participant_id
        LEFT JOIN messages AS reply ON reply.id = m.reply_to_id
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
            }
        )
    return messages, config_ids


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


def _project_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_payload = event["raw_payload"]
    event_type = event["event_type"]
    redacted = event["is_redacted"]
    if not redacted:
        _validate_recognized_payload(event_type, raw_payload)

    if event_type == ERROR_EVENT and not redacted:
        payload, omissions = _project_error_payload(raw_payload)
    else:
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
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("request"), dict)
            or not isinstance(payload.get("local_context"), dict)
        ):
            raise _data_invalid()
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
        projected: dict[str, Any] = {}
        summary_retained = False
        for key, child in item.items():
            normalized = _normalize_key(key)
            child_path = (*path, key)
            omit = (
                _is_secret_key(normalized)
                or normalized in {"encryptedcontent", "reasoningtext"}
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
    return list(dict.fromkeys(values))


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
    _validate_recognized_payload(REQUEST_EVENT, payload)
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
        pointer == "/request/input" or pointer.startswith("/request/input/")
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
    if not isinstance(request, dict) or "input" not in request:
        raise _data_invalid()
    provider_input = request["input"]
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
        or request_participant_key.casefold() != "helios"
        or retrieval["owner_participant_id"] != request_event["participant_id"]
        or trigger_id != request_event["related_message_id"]
        or len(triggering) != 1
        or not isinstance(triggering_participant_key, str)
        or triggering_participant_key.casefold() != "peter"
        or tokenize_memory_query(triggering[0]["message_text"])
        != retrieval["query_terms"]
        or provider_input[-1]
        != {"role": "user", "content": triggering[0]["message_text"]}
    ):
        raise _data_invalid()

    candidates = [
        (index, item)
        for index, item in enumerate(provider_input)
        if isinstance(item, dict)
        and isinstance(item.get("content"), str)
        and item["content"].startswith(INHERITED_MEMORY_HEADER)
    ]
    context: dict[str, Any] | None = None
    if selected:
        expected_position = len(provider_input) - 2
        if (
            len(candidates) != 1
            or candidates[0][0] != expected_position
            or set(candidates[0][1]) != {"role", "content"}
            or candidates[0][1].get("role") != "user"
        ):
            raise _data_invalid()
        context_content = candidates[0][1]["content"]
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
