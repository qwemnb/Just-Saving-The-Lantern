"""HTTP request models for Helios Room."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ParticipantDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["participant"]
    participant_key: str


class RoomDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["room"]


MessageDestination = Annotated[
    Union[ParticipantDestination, RoomDestination],
    Field(discriminator="kind"),
]


class MessageRequest(BaseModel):
    """A browser-authored Peter message.

    Authorship is intentionally absent and extra fields are forbidden so a
    client cannot select Peter, Helios, or any other participant.
    """

    model_config = ConfigDict(extra="forbid")

    message_text: str
    destination: MessageDestination
    response_destination: MessageDestination | None = None

