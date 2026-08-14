"""Explicit provider registrations for addressable AI participants."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParticipantRegistration:
    participant_key: str
    provider: str
    adapter: str


SUPPORTED_PARTICIPANTS = {
    "helios": ParticipantRegistration("helios", "openai", "openai.responses"),
    "gemini": ParticipantRegistration("gemini", "google", "gemini.generate_content"),
}


def registration_for(participant_key: str) -> ParticipantRegistration | None:
    """Return only a statically registered participant adapter."""

    return SUPPORTED_PARTICIPANTS.get(participant_key)


def is_addressable_ai(participant_key: str, participant_type: str) -> bool:
    return participant_type == "ai" and participant_key in SUPPORTED_PARTICIPANTS
