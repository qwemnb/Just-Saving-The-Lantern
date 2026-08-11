"""Three-phase orchestration for the first API-backed Helios turn."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .database import (
    DEFAULT_DATABASE_PATH,
    connect_database,
    create_turn,
    is_blank_message,
    store_message,
)
from .openai_client import (
    OPENAI_MAX_RETRIES,
    OPENAI_TIMEOUT_SECONDS,
    close_client,
    create_openai_client,
    create_response,
    load_openai_environment,
    safe_exception_diagnostics,
    serialize_provider_response,
)


ROOM_KEY = "main"
ROOM_NAME = "The Room"
PETER_KEY = "peter"
HELIOS_KEY = "helios"
PROVIDER = "openai"
SYSTEM_INSTRUCTIONS = (
    "You are Helios, an AI participant in a private, persistent conversation "
    "room with Peter. Respond directly and naturally to Peter's latest message, "
    "using only the canonical room history supplied in this request. Do not claim "
    "access to memories, tools, files, or events that are not present in that "
    "history."
)
RESPONSE_SETTINGS: dict[str, Any] = {
    "store": False,
    "reasoning": {"effort": "low", "context": "current_turn"},
    "max_output_tokens": 2048,
}
RESPONSE_TOOLS: list[dict[str, Any]] = []


class TurnServiceError(RuntimeError):
    """A stable, user-facing turn failure."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        turn_id: int | None = None,
        peter_message_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.turn_id = turn_id
        self.peter_message_id = peter_message_id

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.code,
            "message": self.message,
        }
        if self.turn_id is not None:
            payload["turn_id"] = self.turn_id
        if self.peter_message_id is not None:
            payload["peter_message_id"] = self.peter_message_id
        return payload


@dataclass(frozen=True)
class RoomIdentity:
    room_id: int
    peter_id: int
    helios_id: int


@dataclass(frozen=True)
class AcceptedTurn:
    turn_id: int
    room_id: int
    peter_message_id: int
    helios_id: int
    helios_config_id: int
    provider_request: dict[str, Any]


def canonical_json(value: Any) -> str:
    """Serialize a JSON value in the canonical configuration/event form."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


async def run_helios_turn(
    message_text: str,
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    client_factory: Callable[[str], Any] | None = None,
    dotenv_path: Path | str | None = None,
) -> dict[str, Any]:
    """Accept Peter's message, call OpenAI once, and finalize the turn."""

    if is_blank_message(message_text):
        return {"ignored": True, "reason": "empty_message"}

    environment = load_openai_environment(dotenv_path)
    if environment.api_key is None or not environment.api_key.strip():
        raise TurnServiceError(
            status_code=503,
            code="missing_openai_api_key",
            message="OPENAI_API_KEY is missing or blank.",
        )
    if environment.model is None or not environment.model.strip():
        raise TurnServiceError(
            status_code=503,
            code="missing_openai_model",
            message="HELIOS_OPENAI_MODEL is missing or blank.",
        )

    identity = await asyncio.to_thread(_preflight_database, database_path)
    accepted = await asyncio.to_thread(
        _accept_turn,
        database_path,
        identity,
        message_text,
        environment.model,
    )

    factory = client_factory or create_openai_client
    client: Any | None = None
    try:
        client = factory(environment.api_key)
        response = await create_response(client, accepted.provider_request)
    except asyncio.CancelledError as exception:
        raise _stranded_turn_error(accepted, cause=exception) from exception
    except Exception as exception:
        reason = (
            "provider_timeout" if _is_timeout_exception(exception) else "provider_failure"
        )
        error_payload = {
            "error": safe_exception_diagnostics(
                exception,
                reason=reason,
                secrets=(environment.api_key,),
            ),
        }
        await _finalize_failure_or_raise(
            database_path,
            accepted,
            error_payload,
        )
        raise TurnServiceError(
            status_code=504 if reason == "provider_timeout" else 502,
            code=reason,
            message=(
                "The Helios provider request timed out."
                if reason == "provider_timeout"
                else "The Helios provider request failed."
            ),
            turn_id=accepted.turn_id,
            peter_message_id=accepted.peter_message_id,
        ) from exception
    finally:
        if client is not None:
            try:
                await close_client(client)
            except Exception:
                # Connection cleanup must not cause a second provider call or
                # replace the durable outcome of the turn.
                pass

    try:
        raw_response = serialize_provider_response(
            response,
            api_key=environment.api_key,
        )
    except Exception as exception:
        error_payload = {
            "error": safe_exception_diagnostics(
                exception,
                reason="provider_response_serialization_failed",
                secrets=(environment.api_key,),
            )
        }
        await _finalize_failure_or_raise(database_path, accepted, error_payload)
        raise TurnServiceError(
            status_code=502,
            code="provider_response_serialization_failed",
            message="The Helios provider response could not be recorded safely.",
            turn_id=accepted.turn_id,
            peter_message_id=accepted.peter_message_id,
        ) from exception

    status = _response_attribute(response, raw_response, "status")
    output_text = _response_attribute(response, raw_response, "output_text")

    if status == "completed" and isinstance(output_text, str) and output_text.strip():
        try:
            helios_message_id = await asyncio.to_thread(
                _finalize_success,
                database_path,
                accepted,
                output_text,
                raw_response,
            )
        except Exception as exception:
            raise _stranded_turn_error(accepted, cause=exception) from exception

        return {
            "turn_id": accepted.turn_id,
            "peter_message_id": accepted.peter_message_id,
            "helios_message_id": helios_message_id,
            "status": "completed",
        }

    failure_reason = _unusable_response_reason(status, output_text, raw_response)
    await _finalize_failure_or_raise(
        database_path,
        accepted,
        {"reason": failure_reason, "response": raw_response},
    )
    raise TurnServiceError(
        status_code=502,
        code=failure_reason,
        message="The Helios provider returned an unusable response.",
        turn_id=accepted.turn_id,
        peter_message_id=accepted.peter_message_id,
    )


def _preflight_database(database_path: Path | str) -> RoomIdentity:
    try:
        connection = connect_database(database_path)
    except (OSError, sqlite3.Error) as exception:
        raise TurnServiceError(
            status_code=503,
            code="invalid_database_configuration",
            message="The Helios database could not be opened for preflight.",
        ) from exception
    try:
        room = connection.execute(
            "SELECT id, name FROM rooms WHERE room_key = ?",
            (ROOM_KEY,),
        ).fetchone()
        if room is None or room["name"] != ROOM_NAME:
            raise TurnServiceError(
                status_code=503,
                code="invalid_room_configuration",
                message="The configured Helios room is missing or invalid.",
            )

        participants = connection.execute(
            """
            SELECT id, participant_key, name, participant_type
            FROM participants
            WHERE participant_key IN (?, ?)
            """,
            (PETER_KEY, HELIOS_KEY),
        ).fetchall()
        by_key = {row["participant_key"]: row for row in participants}
        peter = by_key.get(PETER_KEY)
        helios = by_key.get(HELIOS_KEY)
        if (
            peter is None
            or peter["name"] != "Peter"
            or peter["participant_type"] != "human"
            or helios is None
            or helios["name"] != "Helios"
            or helios["participant_type"] != "ai"
        ):
            raise TurnServiceError(
                status_code=503,
                code="invalid_participant_configuration",
                message="Peter or Helios is missing or has unexpected identity data.",
            )

        active_ids = {
            row["participant_id"]
            for row in connection.execute(
                """
                SELECT participant_id
                FROM room_participants
                WHERE room_id = ? AND left_at IS NULL
                """,
                (room["id"],),
            ).fetchall()
        }
        if peter["id"] not in active_ids or helios["id"] not in active_ids:
            raise TurnServiceError(
                status_code=503,
                code="invalid_participant_configuration",
                message="Peter and Helios must both be active in the configured room.",
            )

        return RoomIdentity(
            room_id=room["id"],
            peter_id=peter["id"],
            helios_id=helios["id"],
        )
    except TurnServiceError:
        raise
    except sqlite3.Error as exception:
        raise TurnServiceError(
            status_code=503,
            code="invalid_database_configuration",
            message="The Helios database schema is missing or invalid.",
        ) from exception
    finally:
        connection.close()


def _accept_turn(
    database_path: Path | str,
    identity: RoomIdentity,
    message_text: str,
    model: str,
) -> AcceptedTurn:
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        config_id = find_or_create_helios_configuration(
            connection,
            helios_id=identity.helios_id,
            model=model,
            instructions=SYSTEM_INSTRUCTIONS,
            settings=RESPONSE_SETTINGS,
            tools=RESPONSE_TOOLS,
        )
        turn_id = create_turn(connection, identity.room_id, identity.peter_id)
        peter_message_id = store_message(
            connection,
            room_id=identity.room_id,
            participant_id=identity.peter_id,
            message_text=message_text,
            turn_id=turn_id,
            message_type="chat",
        )
        boundary = connection.execute(
            "SELECT room_sequence_no FROM messages WHERE id = ?",
            (peter_message_id,),
        ).fetchone()[0]
        provider_input = _load_and_validate_history(
            connection,
            room_id=identity.room_id,
            boundary=boundary,
        )
        provider_request = {
            "model": model,
            "instructions": SYSTEM_INSTRUCTIONS,
            "input": provider_input,
            "store": RESPONSE_SETTINGS["store"],
            "reasoning": dict(RESPONSE_SETTINGS["reasoning"]),
            "max_output_tokens": RESPONSE_SETTINGS["max_output_tokens"],
            "tools": list(RESPONSE_TOOLS),
        }
        request_event_payload = {
            "request": provider_request,
            "local_context": {
                "provider": PROVIDER,
                "operation": "responses.create",
                "trigger_message_id": peter_message_id,
                "room_sequence_boundary": boundary,
                "timeout_seconds": int(OPENAI_TIMEOUT_SECONDS),
                "max_retries": OPENAI_MAX_RETRIES,
            },
        }
        _insert_api_event(
            connection,
            turn_id=turn_id,
            room_id=identity.room_id,
            helios_id=identity.helios_id,
            config_id=config_id,
            sequence_no=1,
            event_type="openai.responses.request",
            related_message_id=peter_message_id,
            payload=request_event_payload,
        )
        connection.commit()
        return AcceptedTurn(
            turn_id=turn_id,
            room_id=identity.room_id,
            peter_message_id=peter_message_id,
            helios_id=identity.helios_id,
            helios_config_id=config_id,
            provider_request=provider_request,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def find_or_create_helios_configuration(
    connection: sqlite3.Connection,
    *,
    helios_id: int,
    model: str,
    instructions: str,
    settings: Any,
    tools: Any,
) -> int:
    """Return a semantically exact immutable configuration, creating if needed."""

    canonical_settings = canonical_json(settings)
    canonical_tools = canonical_json(tools)
    rows = connection.execute(
        """
        SELECT id, provider, model, config_label, system_instructions,
               settings_json, tools_json
        FROM participant_configs
        WHERE participant_id = ?
        ORDER BY id
        """,
        (helios_id,),
    ).fetchall()

    for row in rows:
        if (
            row["provider"] == PROVIDER
            and row["model"] == model
            and row["system_instructions"] == instructions
            and _canonical_stored_json(row["settings_json"]) == canonical_settings
            and _canonical_stored_json(row["tools_json"]) == canonical_tools
        ):
            return row["id"]

    label = _next_configuration_label(rows, model)
    cursor = connection.execute(
        """
        INSERT INTO participant_configs (
            participant_id,
            provider,
            model,
            config_label,
            system_instructions,
            settings_json,
            tools_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            helios_id,
            PROVIDER,
            model,
            label,
            instructions,
            canonical_settings,
            canonical_tools,
        ),
    )
    return cursor.lastrowid


def _canonical_stored_json(raw_json: str | None) -> str | None:
    if raw_json is None:
        return None
    try:
        return canonical_json(json.loads(raw_json))
    except (json.JSONDecodeError, TypeError):
        return None


def _next_configuration_label(rows: list[sqlite3.Row], model: str) -> str:
    role = model.removeprefix("gpt-5.6-")
    role = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "model"
    prefix = f"minimal-openai-{role}-v"
    versions: list[int] = []
    for row in rows:
        label = row["config_label"]
        if isinstance(label, str) and label.startswith(prefix):
            suffix = label[len(prefix) :]
            if suffix.isdigit():
                versions.append(int(suffix))
    return f"{prefix}{max(versions, default=0) + 1}"


def _load_and_validate_history(
    connection: sqlite3.Connection,
    *,
    room_id: int,
    boundary: int,
) -> list[dict[str, str]]:
    rows = connection.execute(
        """
        SELECT m.message_type, m.message_text, p.participant_key
        FROM messages AS m
        JOIN participants AS p ON p.id = m.participant_id
        WHERE m.room_id = ? AND m.room_sequence_no <= ?
        ORDER BY m.room_sequence_no
        """,
        (room_id, boundary),
    ).fetchall()

    provider_input: list[dict[str, str]] = []
    for row in rows:
        if row["message_type"] != "chat":
            raise TurnServiceError(
                status_code=409,
                code="unsupported_history_message_type",
                message="Canonical history contains an unsupported message type.",
            )
        if row["participant_key"] == PETER_KEY:
            role = "user"
        elif row["participant_key"] == HELIOS_KEY:
            role = "assistant"
        else:
            raise TurnServiceError(
                status_code=409,
                code="unsupported_history_participant",
                message="Canonical history contains an unsupported participant.",
            )
        provider_input.append({"role": role, "content": row["message_text"]})
    return provider_input


def _finalize_success(
    database_path: Path | str,
    accepted: AcceptedTurn,
    output_text: str,
    raw_response: dict[str, Any],
) -> int:
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _assert_turn_open(connection, accepted)
        helios_message_id = store_message(
            connection,
            room_id=accepted.room_id,
            participant_id=accepted.helios_id,
            participant_config_id=accepted.helios_config_id,
            message_text=output_text,
            turn_id=accepted.turn_id,
            reply_to_id=accepted.peter_message_id,
            message_type="chat",
        )
        _insert_api_event(
            connection,
            turn_id=accepted.turn_id,
            room_id=accepted.room_id,
            helios_id=accepted.helios_id,
            config_id=accepted.helios_config_id,
            sequence_no=2,
            event_type="openai.responses.response",
            related_message_id=helios_message_id,
            payload={"response": raw_response},
        )
        _set_turn_terminal(connection, accepted.turn_id, "completed")
        connection.commit()
        return helios_message_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finalize_failure(
    database_path: Path | str,
    accepted: AcceptedTurn,
    error_payload: dict[str, Any],
) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _assert_turn_open(connection, accepted)
        _insert_api_event(
            connection,
            turn_id=accepted.turn_id,
            room_id=accepted.room_id,
            helios_id=accepted.helios_id,
            config_id=accepted.helios_config_id,
            sequence_no=2,
            event_type="openai.responses.error",
            related_message_id=accepted.peter_message_id,
            payload=error_payload,
        )
        _set_turn_terminal(connection, accepted.turn_id, "failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


async def _finalize_failure_or_raise(
    database_path: Path | str,
    accepted: AcceptedTurn,
    error_payload: dict[str, Any],
) -> None:
    try:
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            error_payload,
        )
    except Exception as exception:
        raise _stranded_turn_error(accepted, cause=exception) from exception


def _assert_turn_open(
    connection: sqlite3.Connection,
    accepted: AcceptedTurn,
) -> None:
    row = connection.execute(
        "SELECT status, room_id FROM turns WHERE id = ?",
        (accepted.turn_id,),
    ).fetchone()
    if (
        row is None
        or row["room_id"] != accepted.room_id
        or row["status"] != "open"
    ):
        raise RuntimeError("Turn is no longer open for finalization")


def _insert_api_event(
    connection: sqlite3.Connection,
    *,
    turn_id: int,
    room_id: int,
    helios_id: int,
    config_id: int,
    sequence_no: int,
    event_type: str,
    related_message_id: int,
    payload: dict[str, Any],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO api_events (
            turn_id,
            room_id,
            participant_id,
            participant_config_id,
            sequence_no,
            event_type,
            related_message_id,
            payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            room_id,
            helios_id,
            config_id,
            sequence_no,
            event_type,
            related_message_id,
            canonical_json(payload),
        ),
    )
    return cursor.lastrowid


def _set_turn_terminal(
    connection: sqlite3.Connection,
    turn_id: int,
    status: str,
) -> None:
    cursor = connection.execute(
        """
        UPDATE turns
        SET status = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status = 'open'
        """,
        (status, turn_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Turn could not be finalized")


def _response_attribute(
    response: Any,
    raw_response: dict[str, Any],
    name: str,
) -> Any:
    if hasattr(response, name):
        return getattr(response, name)
    return raw_response.get(name)


def _unusable_response_reason(
    status: Any,
    output_text: Any,
    raw_response: dict[str, Any],
) -> str:
    if status == "incomplete":
        return "incomplete_response"
    if status == "completed":
        if _contains_structured_refusal(raw_response):
            return "refusal_without_text"
        if not isinstance(output_text, str) or not output_text.strip():
            return "blank_output"
    return "provider_status_failed"


def _contains_structured_refusal(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "refusal":
            return True
        refusal = value.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return True
        return any(_contains_structured_refusal(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_structured_refusal(item) for item in value)
    return False


def _is_timeout_exception(exception: BaseException) -> bool:
    return isinstance(exception, TimeoutError) or type(exception).__name__ in {
        "APITimeoutError",
        "TimeoutException",
    }


def _stranded_turn_error(
    accepted: AcceptedTurn,
    *,
    cause: BaseException,
) -> TurnServiceError:
    del cause  # The original text is intentionally never exposed or persisted.
    return TurnServiceError(
        status_code=500,
        code="turn_finalization_failed",
        message=(
            "The provider call may have completed, but the turn could not be "
            "finalized. Manual reconciliation is required."
        ),
        turn_id=accepted.turn_id,
        peter_message_id=accepted.peter_message_id,
    )

