"""Closed direct-response routing contracts shared by providers and Trace."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


TURN_ROUTING_VERSION = "explicit_response_destination_v1"
PROVIDER_HISTORY_V2 = "provider_history_v2"
PROVIDER_HISTORY_V3 = "provider_history_v3"
ROOM_RESPONSE_DESTINATION = "ROOM_RESPONSE_DESTINATION"


class ResponseDestinationUnavailable(ValueError):
    """The requested response destination cannot be authorized."""


@dataclass(frozen=True)
class BoundResponseDestination:
    """The exact immutable route accepted before provider execution."""

    kind: str
    destination_alias_id: int
    display_name: str
    recipient_participant_id: int | None = None
    participant_key: str | None = None

    def evidence(self) -> dict[str, str]:
        if self.kind == "room":
            return {"display_name": self.display_name, "kind": "room"}
        if self.kind == "participant" and self.participant_key is not None:
            return {
                "display_name": self.display_name,
                "kind": "participant",
                "participant_key": self.participant_key,
            }
        raise ValueError("invalid bound response destination")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_response_destination_evidence(value: Any) -> dict[str, str]:
    """Validate the exact provider-visible response-destination object."""

    if not isinstance(value, dict):
        raise ValueError("invalid response destination evidence")
    if set(value) == {"display_name", "kind"}:
        if value != {"display_name": "Room", "kind": "room"}:
            raise ValueError("invalid response destination evidence")
        return value
    if set(value) == {"display_name", "kind", "participant_key"}:
        if (
            value.get("kind") != "participant"
            or not isinstance(value.get("participant_key"), str)
            or not value["participant_key"].strip()
            or not isinstance(value.get("display_name"), str)
            or not value["display_name"].strip()
        ):
            raise ValueError("invalid response destination evidence")
        return value
    raise ValueError("invalid response destination evidence")


def routing_context_text(response_destination: Mapping[str, Any]) -> str:
    evidence = validate_response_destination_evidence(dict(response_destination))
    return f"{ROOM_RESPONSE_DESTINATION}\n{canonical_json(evidence)}"


def build_openai_provider_input(
    canonical_history: Sequence[Mapping[str, Any]],
    *,
    inherited_memory_context: str | None,
    response_destination: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in canonical_history]
    if not result or result[-1].get("role") != "user":
        raise ValueError("provider history has no Peter trigger")
    insertion = len(result) - 1
    if inherited_memory_context is not None:
        result.insert(insertion, {"role": "user", "content": inherited_memory_context})
        insertion += 1
    result.insert(
        insertion,
        {"role": "user", "content": routing_context_text(response_destination)},
    )
    return result


def build_gemini_provider_contents(
    canonical_history: Sequence[Mapping[str, Any]],
    *,
    inherited_memory_context: str | None,
    response_destination: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in canonical_history]
    if not result or result[-1].get("role") != "user":
        raise ValueError("provider history has no Peter trigger")
    insertion = len(result) - 1
    if inherited_memory_context is not None:
        result.insert(
            insertion,
            {"role": "user", "parts": [{"text": inherited_memory_context}]},
        )
        insertion += 1
    result.insert(
        insertion,
        {
            "role": "user",
            "parts": [{"text": routing_context_text(response_destination)}],
        },
    )
    return result


def resolve_response_destination(
    connection: sqlite3.Connection,
    *,
    room_id: int,
    responding_participant_id: int,
    requested: Mapping[str, Any] | None,
) -> BoundResponseDestination:
    """Resolve one request to immutable participant/alias evidence in Phase A."""

    normalized: Mapping[str, Any] = (
        {"kind": "participant", "participant_key": "peter"}
        if requested is None
        else requested
    )
    if not isinstance(normalized, Mapping):
        raise ResponseDestinationUnavailable
    if set(normalized) == {"kind"} and normalized.get("kind") == "room":
        rows = connection.execute(
            """SELECT pa.id, pa.display_alias, pa.alias_key, p.id AS participant_id,
                      p.participant_key, p.participant_type, p.name
               FROM participants AS p
               JOIN participant_primary_aliases AS ppa ON ppa.participant_id=p.id
               JOIN participant_aliases AS pa
                 ON pa.id=ppa.alias_id AND pa.participant_id=p.id
               WHERE p.participant_key='room-system'"""
        ).fetchall()
        if (
            len(rows) != 1
            or rows[0]["participant_type"] != "system"
            or rows[0]["name"] != "Room"
            or rows[0]["display_alias"] != "Room"
            or rows[0]["alias_key"] != "room"
        ):
            raise ResponseDestinationUnavailable
        return BoundResponseDestination(
            kind="room",
            destination_alias_id=rows[0]["id"],
            display_name="Room",
        )
    if set(normalized) != {"kind", "participant_key"}:
        raise ResponseDestinationUnavailable
    participant_key = normalized.get("participant_key")
    if normalized.get("kind") != "participant" or not isinstance(participant_key, str):
        raise ResponseDestinationUnavailable
    rows = connection.execute(
        """SELECT p.id, p.participant_key, p.participant_type,
                  pa.id AS alias_id, pa.display_alias, pa.alias_key
           FROM participants AS p
           JOIN room_participants AS rp
             ON rp.participant_id=p.id AND rp.room_id=? AND rp.left_at IS NULL
           JOIN participant_primary_aliases AS ppa ON ppa.participant_id=p.id
           JOIN participant_aliases AS pa
             ON pa.id=ppa.alias_id AND pa.participant_id=p.id
           WHERE p.participant_key=?""",
        (room_id, participant_key),
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0]["participant_type"] not in {"human", "ai"}
        or rows[0]["id"] == responding_participant_id
    ):
        raise ResponseDestinationUnavailable
    row = rows[0]
    return BoundResponseDestination(
        kind="participant",
        recipient_participant_id=row["id"],
        participant_key=row["participant_key"],
        destination_alias_id=row["alias_id"],
        display_name=row["display_alias"],
    )


def validate_bound_response_destination(
    connection: sqlite3.Connection, bound: BoundResponseDestination
) -> None:
    """Reprove immutable identity/alias evidence without re-resolving membership."""

    evidence = bound.evidence()
    validate_response_destination_evidence(evidence)
    if bound.kind == "room":
        rows = connection.execute(
            """SELECT pa.display_alias, pa.alias_key, p.participant_key,
                      p.participant_type, p.name
               FROM participant_aliases AS pa
               JOIN participants AS p ON p.id=pa.participant_id
               WHERE pa.id=?""",
            (bound.destination_alias_id,),
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (
            "Room", "room", "room-system", "system", "Room"
        ):
            raise ValueError("response route evidence changed")
        return
    rows = connection.execute(
        """SELECT pa.participant_id, pa.display_alias, p.participant_key,
                  p.participant_type
           FROM participant_aliases AS pa
           JOIN participants AS p ON p.id=pa.participant_id
           WHERE pa.id=?""",
        (bound.destination_alias_id,),
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0]["participant_id"] != bound.recipient_participant_id
        or rows[0]["display_alias"] != bound.display_name
        or rows[0]["participant_key"] != bound.participant_key
        or rows[0]["participant_type"] not in {"human", "ai"}
    ):
        raise ValueError("response route evidence changed")
