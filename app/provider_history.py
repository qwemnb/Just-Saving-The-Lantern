"""Strict provider-specific projections of canonical routed room history."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .gemini_client import (
    GEMINI_SYSTEM_INSTRUCTIONS,
    model_content_from_stored_response,
    recorded_settings,
    validate_recorded_google_request_payload,
)


class ProviderHistoryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _route_error() -> ProviderHistoryError:
    return ProviderHistoryError(
        "unsupported_history_route", "Canonical history contains invalid routing data."
    )


def _message_type_error() -> ProviderHistoryError:
    return ProviderHistoryError(
        "unsupported_history_message_type",
        "Canonical history contains an unsupported message type.",
    )


def _participant_error() -> ProviderHistoryError:
    return ProviderHistoryError(
        "unsupported_history_participant",
        "Canonical history contains an unsupported participant.",
    )


def _gemini_history_error() -> ProviderHistoryError:
    return ProviderHistoryError(
        "unsupported_gemini_provider_history",
        "Gemini provider history cannot be reconstructed safely.",
    )


def load_provider_history(
    connection: sqlite3.Connection,
    *,
    room_id: int,
    boundary: int,
    provider_participant_key: str,
) -> list[dict[str, Any]]:
    """Project one validated canonical prefix for Helios or Gemini."""

    if provider_participant_key not in {"helios", "gemini"}:
        raise _participant_error()
    rows = connection.execute(
        """
        SELECT m.id, m.turn_id, m.message_type, m.message_text,
               m.participant_id, m.participant_config_id,
               p.participant_key, p.participant_type,
               mr.routing_mode, mr.destination_kind,
               mr.recipient_participant_id,
               recipient.participant_key AS recipient_key,
               sa.participant_id AS sender_alias_owner,
               sa.display_alias AS sender_display,
               da.participant_id AS destination_alias_owner,
               da.display_alias AS destination_display,
               da.alias_key AS destination_alias_key,
               destination_owner.participant_key AS destination_alias_owner_key
        FROM messages AS m
        JOIN participants AS p ON p.id=m.participant_id
        JOIN message_routes AS mr ON mr.message_id=m.id
          AND mr.room_id=m.room_id AND mr.sender_participant_id=m.participant_id
        JOIN participant_aliases AS sa ON sa.id=mr.sender_alias_id
          AND sa.participant_id=m.participant_id
        JOIN participant_aliases AS da ON da.id=mr.destination_alias_id
        JOIN participants AS destination_owner ON destination_owner.id=da.participant_id
        LEFT JOIN participants AS recipient ON recipient.id=mr.recipient_participant_id
        WHERE m.room_id=? AND m.room_sequence_no<=?
        ORDER BY m.room_sequence_no
        """,
        (room_id, boundary),
    ).fetchall()
    expected = connection.execute(
        "SELECT count(*) FROM messages WHERE room_id=? AND room_sequence_no<=?",
        (room_id, boundary),
    ).fetchone()[0]
    if len(rows) != expected:
        raise _route_error()

    result: list[dict[str, Any]] = []
    for row in rows:
        destination = _validate_route(row)
        if row["message_type"] == "system":
            _validate_system_message(connection, row, room_id, destination)
            continue
        if row["message_type"] != "chat":
            raise _message_type_error()
        sender_key = row["participant_key"]
        recipient_key = row["recipient_key"]

        if provider_participant_key == "helios":
            if destination == "room":
                if sender_key == "peter":
                    result.append({"role": "user", "content": row["message_text"]})
                    continue
                raise _participant_error()
            if sender_key == "peter" and recipient_key == "helios":
                result.append({"role": "user", "content": row["message_text"]})
            elif sender_key == "helios" and recipient_key == "peter":
                result.append({"role": "assistant", "content": row["message_text"]})
            else:
                # Every route was validated first; direct exchanges outside the
                # selected provider's Peter dialogue are intentionally invisible.
                continue
            continue

        # Gemini projection.
        if destination == "room":
            if sender_key == "peter":
                result.append(_text_content("user", row["message_text"]))
            elif sender_key == "gemini":
                raise _participant_error()
            else:
                result.append(_external_content(row, destination_kind="room"))
            continue
        if sender_key == "peter" and recipient_key == "gemini":
            result.append(_text_content("user", row["message_text"]))
        elif sender_key == "gemini" and recipient_key == "peter":
            result.append(
                _load_gemini_replay(
                    connection, row, room_id, expected_contents=list(result)
                )
            )
        elif recipient_key == "gemini" and sender_key != "gemini":
            result.append(_external_content(row, destination_kind="participant"))
        else:
            continue
    return result


def _validate_route(row: sqlite3.Row) -> str:
    if (
        row["routing_mode"] not in {"legacy_implicit", "explicit"}
        or row["sender_alias_owner"] != row["participant_id"]
    ):
        raise _route_error()
    if row["destination_kind"] == "participant":
        if (
            row["recipient_participant_id"] is None
            or row["recipient_key"] is None
            or row["destination_alias_owner"] != row["recipient_participant_id"]
        ):
            raise _route_error()
        return "participant"
    if row["destination_kind"] == "room":
        if (
            row["recipient_participant_id"] is not None
            or row["destination_alias_owner_key"] != "room-system"
            or row["destination_alias_key"] != "room"
            or row["destination_display"] != "Room"
        ):
            raise _route_error()
        return "room"
    raise _route_error()


def _validate_system_message(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    room_id: int,
    destination: str,
) -> None:
    if destination != "room":
        raise _message_type_error()
    if row["routing_mode"] == "legacy_implicit":
        bootstrap_count = connection.execute(
            """SELECT count(*)
               FROM participant_name_events AS pne
               JOIN message_routes AS mr ON mr.message_id=?
               WHERE pne.event_type='bootstrap'
                 AND pne.subject_participant_id=mr.sender_participant_id
                 AND pne.new_alias_id=mr.sender_alias_id
                 AND pne.room_id IS NULL
                 AND pne.previous_alias_id IS NULL
                 AND pne.canonical_message_id IS NULL""",
            (row["id"],),
        ).fetchone()[0]
        if bootstrap_count == 1:
            return
    if row["routing_mode"] == "explicit" and row["participant_key"] == "room-system":
        adopted = connection.execute(
            """SELECT count(*) FROM participant_name_events
               WHERE event_type='adopted' AND canonical_message_id=? AND room_id=?""",
            (row["id"], room_id),
        ).fetchone()[0]
        if adopted == 1:
            return
    raise _message_type_error()


def _text_content(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "parts": [{"text": text}]}


def _external_content(row: sqlite3.Row, *, destination_kind: str) -> dict[str, Any]:
    if destination_kind == "room":
        destination: dict[str, Any] = {"display_name": "Room", "kind": "room"}
    else:
        destination = {
            "display_name": row["destination_display"],
            "kind": "participant",
            "participant_key": row["recipient_key"],
        }
    envelope = {
        "destination": destination,
        "kind": "room_participant_message",
        "message_id": row["id"],
        "message_text": row["message_text"],
        "sender": {
            "display_name": row["sender_display"],
            "participant_key": row["participant_key"],
        },
    }
    rendered = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _text_content("user", "ROOM_PARTICIPANT_MESSAGE\n" + rendered)


def _load_gemini_replay(
    connection: sqlite3.Connection,
    message: sqlite3.Row,
    room_id: int,
    *,
    expected_contents: list[dict[str, Any]],
) -> dict[str, Any]:
    events = connection.execute(
        """SELECT ae.sequence_no, ae.event_type, ae.participant_id,
                  ae.participant_config_id, ae.related_message_id,
                  ae.payload_json, ae.is_redacted, ae.turn_id, ae.room_id,
                  ae.tool_invocation_id, t.status
           FROM api_events AS ae
           JOIN turns AS t ON t.id=ae.turn_id
           WHERE ae.turn_id=? ORDER BY ae.sequence_no""",
        (message["turn_id"],),
    ).fetchall()
    requests = [row for row in events if row["event_type"] == "google.generate_content.request"]
    responses = [row for row in events if row["event_type"] == "google.generate_content.response"]
    other = [
        row for row in events
        if row["event_type"] not in {
            "google.generate_content.request", "google.generate_content.response"
        }
    ]
    if (
        len(requests) != 1
        or len(responses) != 1
        or other
        or requests[0]["sequence_no"] != 1
        or responses[0]["sequence_no"] != 2
        or requests[0]["participant_id"] != message["participant_id"]
        or requests[0]["participant_config_id"] != message["participant_config_id"]
        or requests[0]["turn_id"] != message["turn_id"]
        or requests[0]["room_id"] != room_id
        or requests[0]["status"] != "completed"
        or requests[0]["is_redacted"]
        or requests[0]["tool_invocation_id"] is not None
        or responses[0]["related_message_id"] != message["id"]
        or responses[0]["participant_id"] != message["participant_id"]
        or responses[0]["participant_config_id"] != message["participant_config_id"]
        or responses[0]["turn_id"] != message["turn_id"]
        or responses[0]["room_id"] != room_id
        or responses[0]["status"] != "completed"
        or responses[0]["is_redacted"]
        or responses[0]["tool_invocation_id"] is not None
    ):
        raise _gemini_history_error()
    try:
        trigger_rows = connection.execute(
            """SELECT m.id, m.turn_id, m.room_id, m.room_sequence_no,
                      m.message_type, m.message_text, m.participant_config_id,
                      p.id AS participant_id, p.participant_key,
                      mr.sender_participant_id, mr.destination_kind,
                      mr.recipient_participant_id, mr.routing_mode,
                      sa.participant_id AS sender_alias_owner,
                      da.participant_id AS destination_alias_owner,
                      recipient.participant_key AS recipient_key
               FROM messages AS m
               JOIN participants AS p ON p.id=m.participant_id
               JOIN message_routes AS mr ON mr.message_id=m.id
                 AND mr.room_id=m.room_id
               JOIN participant_aliases AS sa ON sa.id=mr.sender_alias_id
               JOIN participant_aliases AS da ON da.id=mr.destination_alias_id
               LEFT JOIN participants AS recipient ON recipient.id=mr.recipient_participant_id
               WHERE m.id=?""",
            (requests[0]["related_message_id"],),
        ).fetchall()
        trigger = trigger_rows[0] if len(trigger_rows) == 1 else None
        if (
            trigger is None
            or trigger["turn_id"] != message["turn_id"]
            or trigger["room_id"] != room_id
            or trigger["message_type"] != "chat"
            or trigger["participant_key"] != "peter"
            or trigger["participant_config_id"] is not None
            or trigger["sender_participant_id"] != trigger["participant_id"]
            or trigger["sender_alias_owner"] != trigger["participant_id"]
            or trigger["destination_kind"] != "participant"
            or trigger["recipient_key"] != "gemini"
            or trigger["recipient_participant_id"] != message["participant_id"]
            or trigger["destination_alias_owner"] != message["participant_id"]
            or trigger["routing_mode"] != "explicit"
        ):
            raise ValueError("invalid request correlation")
        request_payload = json.loads(requests[0]["payload_json"])
        validate_recorded_google_request_payload(request_payload)
        config_rows = connection.execute(
            """SELECT pc.id, pc.participant_id, pc.provider, pc.model,
                      pc.config_label, pc.system_instructions,
                      pc.settings_json, pc.tools_json, p.participant_key
               FROM participant_configs AS pc
               JOIN participants AS p ON p.id=pc.participant_id
               WHERE pc.id=?""",
            (requests[0]["participant_config_id"],),
        ).fetchall()
        if len(config_rows) != 1:
            raise ValueError("invalid Gemini configuration correlation")
        config = config_rows[0]
        requested_model = request_payload["request"]["model"]
        slug = re.sub(r"[^a-z0-9]+", "-", requested_model.lower()).strip("-") or "model"
        prefix = f"seed-memory-google-{slug}-v"
        label = config["config_label"]
        if (
            config["participant_id"] != message["participant_id"]
            or config["participant_key"] != "gemini"
            or config["provider"] != "google"
            or config["model"] != requested_model
            or config["system_instructions"] != GEMINI_SYSTEM_INSTRUCTIONS
            or json.loads(config["settings_json"]) != recorded_settings()
            or json.loads(config["tools_json"]) != []
            or not isinstance(label, str)
            or not label.startswith(prefix)
            or not label[len(prefix):].isdigit()
            or int(label[len(prefix):]) <= 0
        ):
            raise ValueError("invalid Gemini configuration")
        event_evidence = {
            "participant_id": requests[0]["participant_id"],
            "participant_key": "gemini",
            "related_message_id": requests[0]["related_message_id"],
        }
        message_evidence = [{
            "id": trigger["id"],
            "participant_key": trigger["participant_key"],
            "message_text": trigger["message_text"],
            "room_sequence_no": trigger["room_sequence_no"],
        }]
        # Import lazily to keep canonical message projection independent while
        # sharing Trace's exact inherited-memory evidence validator.
        from .trace_service import _validate_inherited_memory_payload

        try:
            retrieval, _context = _validate_inherited_memory_payload(
                request_payload["request"],
                request_payload["local_context"],
                message_evidence,
                event_evidence,
            )
        except Exception as exception:
            raise ValueError("invalid Gemini memory evidence") from exception
        recorded_contents = request_payload["request"]["contents"]
        comparable_contents = list(recorded_contents)
        if retrieval["selected"]:
            del comparable_contents[-2]
        if comparable_contents != expected_contents:
            raise ValueError("recorded Gemini contents do not match canonical history")
        payload = json.loads(responses[0]["payload_json"])
        if not isinstance(payload, dict) or set(payload) != {"response"}:
            raise ValueError("invalid response envelope")
        visible = model_content_from_stored_response(payload["response"])
        if _visible_text(visible) != message["message_text"]:
            raise ValueError("canonical text mismatch")
        return visible
    except (ValueError, TypeError, json.JSONDecodeError) as exception:
        raise _gemini_history_error() from exception


def _visible_text(content: dict[str, Any]) -> str:
    return "".join(
        part["text"]
        for part in content["parts"]
        if part.get("thought") is not True
    )
