"""Pure classification for browser-local Helios Room commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


_TRACE_COMMAND = re.compile(r"^/trace(?:\s+([1-9][0-9]*))?\s*$")
_PARTICIPANTS_COMMAND = re.compile(r"^/participants\s*$")


class LocalCommandKind(str, Enum):
    """The possible interpretations of user-authored text."""

    NON_COMMAND = "non_command"
    TRACE_LATEST = "trace_latest"
    TRACE_TURN = "trace_turn"
    MALFORMED_TRACE = "malformed_trace"
    PARTICIPANTS = "participants"
    MALFORMED_PARTICIPANTS = "malformed_participants"


@dataclass(frozen=True)
class LocalCommand:
    """A side-effect-free command classification result."""

    kind: LocalCommandKind
    turn_id_text: str | None = None


def classify_local_command(message_text: str) -> LocalCommand:
    """Classify text without reading configuration, storage, or the network.

    Only a first token exactly equal to the case-sensitive string ``/trace``
    reserves the text as a local command. Decimal turn text is intentionally
    retained verbatim for the browser to place in the route.
    """

    stripped = message_text.strip()
    if _PARTICIPANTS_COMMAND.fullmatch(stripped) is not None:
        return LocalCommand(LocalCommandKind.PARTICIPANTS)
    match = _TRACE_COMMAND.fullmatch(stripped)
    if match is not None:
        turn_id_text = match.group(1)
        if turn_id_text is None:
            return LocalCommand(LocalCommandKind.TRACE_LATEST)
        return LocalCommand(LocalCommandKind.TRACE_TURN, turn_id_text)

    first_token = stripped.split(maxsplit=1)[0] if stripped else ""
    if first_token == "/trace":
        return LocalCommand(LocalCommandKind.MALFORMED_TRACE)
    if first_token == "/participants":
        return LocalCommand(LocalCommandKind.MALFORMED_PARTICIPANTS)
    return LocalCommand(LocalCommandKind.NON_COMMAND)


def is_reserved_trace_command(command: LocalCommand) -> bool:
    """Return whether the classification must never enter canonical history."""

    return command.kind in {
        LocalCommandKind.TRACE_LATEST,
        LocalCommandKind.TRACE_TURN,
        LocalCommandKind.MALFORMED_TRACE,
    }


def is_reserved_local_command(command: LocalCommand) -> bool:
    """Return whether any browser-only command is reserved."""

    return command.kind is not LocalCommandKind.NON_COMMAND
