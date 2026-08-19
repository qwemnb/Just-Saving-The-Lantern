"""Closed Manual Participant Handoff v1 provider-context contract."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .turn_routing import canonical_json


HANDOFF_PROTOCOL_VERSION = "manual_participant_handoff_v1"
ROOM_HANDOFF_AUTHORIZATION = "ROOM_HANDOFF_AUTHORIZATION"

_IDENTITY_FIELDS = {"display_name", "participant_key"}
_AUTHORIZATION_FIELDS = {
    "authorized_by",
    "responder",
    "response_destination",
    "source_message_id",
    "source_room_sequence",
    "source_sender",
    "source_turn_id",
    "version",
}


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _validate_identity(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != _IDENTITY_FIELDS
        or not isinstance(value.get("participant_key"), str)
        or not value["participant_key"].strip()
        or not isinstance(value.get("display_name"), str)
        or not value["display_name"].strip()
    ):
        raise ValueError("invalid handoff participant identity")
    return value


def validate_handoff_authorization(value: Any) -> dict[str, Any]:
    """Validate the exact application-generated handoff authority object."""

    if not isinstance(value, dict) or set(value) != _AUTHORIZATION_FIELDS:
        raise ValueError("invalid handoff authorization")
    if (
        value.get("version") != HANDOFF_PROTOCOL_VERSION
        or not _positive(value.get("source_message_id"))
        or not _positive(value.get("source_room_sequence"))
        or not _positive(value.get("source_turn_id"))
    ):
        raise ValueError("invalid handoff authorization")
    source = _validate_identity(value.get("source_sender"))
    responder = _validate_identity(value.get("responder"))
    destination = _validate_identity(value.get("response_destination"))
    authorizer = _validate_identity(value.get("authorized_by"))
    if (
        source["participant_key"] != destination["participant_key"]
        or source["participant_key"] == responder["participant_key"]
        or authorizer["participant_key"] != "peter"
    ):
        raise ValueError("invalid handoff authorization")
    return value


def handoff_context_text(authorization: Mapping[str, Any]) -> str:
    evidence = validate_handoff_authorization(dict(authorization))
    return f"{ROOM_HANDOFF_AUTHORIZATION}\n{canonical_json(evidence)}"


def build_openai_handoff_input(
    canonical_history: Sequence[Mapping[str, Any]],
    *,
    inherited_memory_context: str | None,
    authorization: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in canonical_history]
    if not result:
        raise ValueError("provider history has no handoff source")
    if inherited_memory_context is not None:
        result.append({"role": "user", "content": inherited_memory_context})
    result.append(
        {"role": "user", "content": handoff_context_text(authorization)}
    )
    return result


def build_gemini_handoff_contents(
    canonical_history: Sequence[Mapping[str, Any]],
    *,
    inherited_memory_context: str | None,
    authorization: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in canonical_history]
    if not result:
        raise ValueError("provider history has no handoff source")
    if inherited_memory_context is not None:
        result.append(
            {"role": "user", "parts": [{"text": inherited_memory_context}]}
        )
    result.append(
        {
            "role": "user",
            "parts": [{"text": handoff_context_text(authorization)}],
        }
    )
    return result
