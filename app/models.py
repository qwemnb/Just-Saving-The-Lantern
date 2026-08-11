"""HTTP request models for Helios Room."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MessageRequest(BaseModel):
    """A browser-authored Peter message.

    Authorship is intentionally absent and extra fields are forbidden so a
    client cannot select Peter, Helios, or any other participant.
    """

    model_config = ConfigDict(extra="forbid")

    message_text: str

