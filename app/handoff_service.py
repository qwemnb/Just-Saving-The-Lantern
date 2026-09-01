"""One-click, one-provider Manual Participant Handoff v1 orchestration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .database import (
    DEFAULT_DATABASE_PATH,
    MessageVisibilityGuardError,
    connect_database,
    create_turn,
    store_message,
)
from .gemini_client import (
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_SYSTEM_INSTRUCTIONS_V3,
    GEMINI_TIMEOUT_SECONDS,
    GEMINI_TOTAL_ATTEMPTS,
    GeminiResponseSerializationError,
    close_gemini_client,
    contents_from_recorded,
    create_gemini_client,
    create_gemini_response,
    is_timeout_exception as is_gemini_timeout,
    load_gemini_environment,
    recorded_request_config,
    recorded_settings,
    safe_gemini_exception_diagnostics,
    serialize_and_evaluate_response,
    validate_recorded_google_shared_request_payload,
)
from .gemini_service import find_or_create_gemini_configuration
from .handoff_contract import (
    HANDOFF_PROTOCOL_VERSION,
    build_gemini_handoff_contents,
    build_openai_handoff_input,
    validate_handoff_authorization,
)
from .identity_service import current_alias, resolve_room_post_context
from .maintenance_lock import MaintenanceLockError, ResetRecoveryRequiredError
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
from .participant_registry import registration_for
from .provider_history import ProviderHistoryError, load_provider_history
from .request_validation import (
    HISTORY_VISIBILITY_V4,
    OPENAI_RESPONSE_SETTINGS,
    OPENAI_RESPONSE_TOOLS,
    OPENAI_SYSTEM_INSTRUCTIONS_V3,
    validate_recorded_openai_shared_request_payload,
)
from .room_service import (
    TurnServiceError,
    canonical_json,
    find_or_create_helios_configuration,
    validate_phase_a_foundation,
)
from .schema_validation import validate_database_integrity, validate_v14_foundation
from .seed_memory import (
    RESULT_LIMIT as MEMORY_RESULT_LIMIT,
    TEXT_BUDGET_CHARS as MEMORY_TEXT_BUDGET_CHARS,
    search_seeded_memories,
    serialize_inherited_memory_context,
)
from .turn_routing import (
    BoundResponseDestination,
    PROVIDER_HISTORY_V4,
    TURN_ROUTING_VERSION,
    validate_bound_response_destination,
)


OPENAI_REQUEST_EVENT = "openai.responses.request"
OPENAI_RESPONSE_EVENT = "openai.responses.response"
OPENAI_ERROR_EVENT = "openai.responses.error"
GOOGLE_REQUEST_EVENT = "google.generate_content.request"
GOOGLE_RESPONSE_EVENT = "google.generate_content.response"
GOOGLE_ERROR_EVENT = "google.generate_content.error"


@dataclass(frozen=True)
class AcceptedHandoff:
    turn_id: int
    room_id: int
    peter_id: int
    source_message_id: int
    source_turn_id: int
    source_room_sequence: int
    source_message_text: str
    source_sender_id: int
    source_sender_key: str
    source_sender_alias_id: int
    source_sender_display: str
    source_destination_alias_id: int
    source_destination_display: str
    responder_id: int
    responder_key: str
    responder_alias_id: int
    responder_display: str
    config_id: int
    provider: str
    model: str
    response_destination: BoundResponseDestination
    authorization: dict[str, Any]
    request_event_id: int
    request_payload_json: str
    provider_input: list[dict[str, Any]]
    api_key: str


async def run_manual_handoff(
    source_message_id: int,
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    openai_client_factory: Callable[[str], Any] | None = None,
    gemini_client_factory: Callable[[str], Any] | None = None,
    dotenv_path: Path | str | None = None,
) -> dict[str, Any]:
    """Accept one terminal AI-to-AI source and invoke only its recipient."""

    accepted = await asyncio.to_thread(
        _accept_handoff, database_path, source_message_id, dotenv_path
    )
    if accepted.provider == "openai":
        message_id = await _run_openai(
            database_path,
            accepted,
            openai_client_factory or create_openai_client,
        )
    elif accepted.provider == "google":
        message_id = await _run_gemini(
            database_path,
            accepted,
            gemini_client_factory or create_gemini_client,
        )
    else:  # Phase A permits only registered provider families.
        raise _stranded(accepted)
    return {
        "handoff_message_id": message_id,
        "source_message_id": accepted.source_message_id,
        "status": "completed",
        "turn_id": accepted.turn_id,
    }


def _open_connection(database_path: Path | str) -> sqlite3.Connection:
    try:
        return connect_database(database_path)
    except ResetRecoveryRequiredError as exception:
        raise TurnServiceError(
            status_code=503, code=exception.code, message=exception.message
        ) from None
    except MaintenanceLockError as exception:
        raise TurnServiceError(
            status_code=503,
            code="database_maintenance_in_progress",
            message="The Helios Room database is unavailable during maintenance.",
        ) from exception
    except (OSError, sqlite3.Error) as exception:
        raise TurnServiceError(
            status_code=503,
            code="invalid_database_configuration",
            message="The Helios database configuration is invalid.",
        ) from exception


def _accept_handoff(
    database_path: Path | str,
    source_message_id: int,
    dotenv_path: Path | str | None,
) -> AcceptedHandoff:
    connection = _open_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_phase_a_foundation(connection)
            context = resolve_room_post_context(connection)
        except TurnServiceError:
            raise
        except Exception as exception:
            raise TurnServiceError(
                status_code=503,
                code="invalid_database_configuration",
                message="The Helios database configuration is invalid.",
            ) from exception
        source = _load_source(connection, source_message_id)
        if source is None:
            raise TurnServiceError(
                status_code=404,
                code="handoff_source_not_found",
                message="The requested handoff source was not found.",
            )
        latest = connection.execute(
            "SELECT max(room_sequence_no) FROM messages WHERE room_id=?",
            (context["room_id"],),
        ).fetchone()[0]
        if source["room_id"] != context["room_id"] or source["room_sequence_no"] != latest:
            raise TurnServiceError(
                status_code=409,
                code="handoff_source_stale",
                message="The requested handoff source is no longer the latest Room message.",
            )
        _reject_active_duplicate(connection, source_message_id)
        _validate_eligible_source(connection, source, context["room_id"])

        source_alias = current_alias(connection, source["participant_id"])
        responder_alias = current_alias(connection, source["recipient_participant_id"])
        peter_alias = context["peter_alias"]
        response_route = BoundResponseDestination(
            kind="participant",
            destination_alias_id=source_alias["id"],
            display_name=source_alias["display_alias"],
            recipient_participant_id=source["participant_id"],
            participant_key=source["participant_key"],
        )
        authorization = {
            "authorized_by": {
                "display_name": peter_alias["display_alias"],
                "participant_key": "peter",
            },
            "responder": {
                "display_name": responder_alias["display_alias"],
                "participant_key": source["recipient_key"],
            },
            "response_destination": {
                "display_name": response_route.display_name,
                "participant_key": source["participant_key"],
            },
            "source_message_id": source["id"],
            "source_room_sequence": source["room_sequence_no"],
            "source_sender": {
                "display_name": source["sender_display"],
                "participant_key": source["participant_key"],
            },
            "source_turn_id": source["turn_id"],
            "version": HANDOFF_PROTOCOL_VERSION,
        }
        validate_handoff_authorization(authorization)

        try:
            history = load_provider_history(
                connection,
                room_id=context["room_id"],
                boundary=source["room_sequence_no"],
                provider_participant_key=source["recipient_key"],
                projection_version=PROVIDER_HISTORY_V4,
            )
        except ProviderHistoryError as error:
            raise TurnServiceError(
                status_code=409, code=error.code, message=error.message
            ) from error
        try:
            memory = search_seeded_memories(
                connection,
                source["recipient_participant_id"],
                source["message_text"],
                result_limit=MEMORY_RESULT_LIMIT,
                text_budget_chars=MEMORY_TEXT_BUDGET_CHARS,
            )
            inherited = (
                serialize_inherited_memory_context(memory.selected)
                if memory.selected
                else None
            )
            memory_evidence = memory.audit_envelope(
                owner_participant_id=source["recipient_participant_id"],
                query_source_message_id=source["id"],
                result_limit=MEMORY_RESULT_LIMIT,
                text_budget_chars=MEMORY_TEXT_BUDGET_CHARS,
            )
        except Exception as exception:
            raise TurnServiceError(
                status_code=500,
                code="memory_retrieval_failed",
                message="Seeded memory retrieval failed before the provider call.",
            ) from exception

        responder_key = source["recipient_key"]
        registration = registration_for(responder_key)
        if registration is None:
            raise _recipient_unavailable()
        turn_id = create_turn(
            connection, context["room_id"], context["peter"]["id"]
        )
        if registration.provider == "openai":
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
            model = environment.model
            provider_input = build_openai_handoff_input(
                history,
                inherited_memory_context=inherited,
                authorization=authorization,
            )
            config_id = find_or_create_helios_configuration(
                connection,
                helios_id=source["recipient_participant_id"],
                model=model,
                instructions=OPENAI_SYSTEM_INSTRUCTIONS_V3,
                settings=OPENAI_RESPONSE_SETTINGS,
                tools=OPENAI_RESPONSE_TOOLS,
                label_family="manual-handoff-openai",
            )
            request = {
                "model": model,
                "instructions": OPENAI_SYSTEM_INSTRUCTIONS_V3,
                "input": provider_input,
                "store": OPENAI_RESPONSE_SETTINGS["store"],
                "reasoning": dict(OPENAI_RESPONSE_SETTINGS["reasoning"]),
                "max_output_tokens": OPENAI_RESPONSE_SETTINGS["max_output_tokens"],
                "tools": list(OPENAI_RESPONSE_TOOLS),
            }
            local = {
                "provider": "openai",
                "operation": "responses.create",
                "trigger_message_id": source["id"],
                "room_sequence_boundary": source["room_sequence_no"],
                "timeout_seconds": int(OPENAI_TIMEOUT_SECONDS),
                "max_retries": OPENAI_MAX_RETRIES,
                "memory_retrieval": memory_evidence,
                "history_visibility": dict(HISTORY_VISIBILITY_V4),
                "turn_routing_version": TURN_ROUTING_VERSION,
                "response_destination": response_route.evidence(),
                "handoff_version": HANDOFF_PROTOCOL_VERSION,
                "handoff_authorization": authorization,
            }
            payload = {"request": request, "local_context": local}
            validate_recorded_openai_shared_request_payload(payload)
            request_type = OPENAI_REQUEST_EVENT
            api_key = environment.api_key
        elif registration.provider == "google":
            environment = load_gemini_environment(dotenv_path)
            if environment.api_key is None or not environment.api_key.strip():
                raise TurnServiceError(
                    status_code=503,
                    code="missing_gemini_api_key",
                    message="GEMINI_API_KEY is missing or blank.",
                )
            if environment.model is None or not environment.model.strip():
                raise TurnServiceError(
                    status_code=503,
                    code="missing_gemini_model",
                    message="HELIOS_GEMINI_MODEL is missing or blank.",
                )
            model = environment.model
            provider_input = build_gemini_handoff_contents(
                history,
                inherited_memory_context=inherited,
                authorization=authorization,
            )
            contents_from_recorded(provider_input)
            config_id = find_or_create_gemini_configuration(
                connection,
                gemini_id=source["recipient_participant_id"],
                model=model,
            )
            request = {
                "config": recorded_request_config(
                    GEMINI_SYSTEM_INSTRUCTIONS_V3,
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                ),
                "contents": provider_input,
                "model": model,
            }
            local = {
                "api_version": "v1beta",
                "memory_retrieval": memory_evidence,
                "operation": "models.generate_content",
                "provider": "google",
                "room_sequence_boundary": source["room_sequence_no"],
                "safety_settings": "provider_default",
                "sdk_policy": {"automatic_function_calling": {"disable": True}},
                "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
                "total_attempts": GEMINI_TOTAL_ATTEMPTS,
                "trigger_message_id": source["id"],
                "history_visibility": dict(HISTORY_VISIBILITY_V4),
                "turn_routing_version": TURN_ROUTING_VERSION,
                "response_destination": response_route.evidence(),
                "handoff_version": HANDOFF_PROTOCOL_VERSION,
                "handoff_authorization": authorization,
            }
            payload = {"local_context": local, "request": request}
            validate_recorded_google_shared_request_payload(payload)
            request_type = GOOGLE_REQUEST_EVENT
            api_key = environment.api_key
        else:
            raise _recipient_unavailable()

        request_event_id = _insert_event(
            connection,
            turn_id=turn_id,
            room_id=context["room_id"],
            participant_id=source["recipient_participant_id"],
            config_id=config_id,
            sequence_no=1,
            event_type=request_type,
            related_message_id=source["id"],
            payload=payload,
        )
        connection.commit()
        return AcceptedHandoff(
            turn_id=turn_id,
            room_id=context["room_id"],
            peter_id=context["peter"]["id"],
            source_message_id=source["id"],
            source_turn_id=source["turn_id"],
            source_room_sequence=source["room_sequence_no"],
            source_message_text=source["message_text"],
            source_sender_id=source["participant_id"],
            source_sender_key=source["participant_key"],
            source_sender_alias_id=source["sender_alias_id"],
            source_sender_display=source["sender_display"],
            source_destination_alias_id=source["destination_alias_id"],
            source_destination_display=source["destination_display"],
            responder_id=source["recipient_participant_id"],
            responder_key=responder_key,
            responder_alias_id=responder_alias["id"],
            responder_display=responder_alias["display_alias"],
            config_id=config_id,
            provider=registration.provider,
            model=model,
            response_destination=response_route,
            authorization=authorization,
            request_event_id=request_event_id,
            request_payload_json=canonical_json(payload),
            provider_input=provider_input,
            api_key=api_key,
        )
    except MessageVisibilityGuardError:
        connection.rollback()
        raise TurnServiceError(
            status_code=409,
            code="unsupported_history_visibility_policy",
            message="Canonical history visibility cannot be reconstructed safely.",
        ) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_source(
    connection: sqlite3.Connection, source_message_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT m.id, m.turn_id, m.room_id, m.room_sequence_no,
                  m.participant_id, m.participant_config_id, m.message_type,
                  m.message_text, p.participant_key, p.participant_type,
                  mr.routing_mode, mr.destination_kind,
                  mr.recipient_participant_id,
                  recipient.participant_key AS recipient_key,
                  recipient.participant_type AS recipient_type,
                  mr.sender_alias_id, sa.participant_id AS sender_alias_owner,
                  sa.display_alias AS sender_display,
                  mr.destination_alias_id,
                  da.participant_id AS destination_alias_owner,
                  da.display_alias AS destination_display,
                  pc.participant_id AS config_owner, pc.provider AS config_provider
           FROM messages AS m
           JOIN participants AS p ON p.id=m.participant_id
           JOIN message_routes AS mr ON mr.message_id=m.id AND mr.room_id=m.room_id
           JOIN participant_aliases AS sa ON sa.id=mr.sender_alias_id
           JOIN participant_aliases AS da ON da.id=mr.destination_alias_id
           LEFT JOIN participants AS recipient ON recipient.id=mr.recipient_participant_id
           LEFT JOIN participant_configs AS pc ON pc.id=m.participant_config_id
           WHERE m.id=?""",
        (source_message_id,),
    ).fetchone()


def _validate_eligible_source(
    connection: sqlite3.Connection, source: sqlite3.Row, room_id: int
) -> None:
    sender_registration = registration_for(source["participant_key"])
    responder_registration = registration_for(source["recipient_key"] or "")
    active = connection.execute(
        """SELECT participant_id FROM room_participants
           WHERE room_id=? AND left_at IS NULL
             AND participant_id IN (?, ?)""",
        (room_id, source["participant_id"], source["recipient_participant_id"]),
    ).fetchall()
    active_ids = {row[0] for row in active}
    if (
        source["turn_id"] is None
        or source["message_type"] != "chat"
        or source["participant_type"] != "ai"
        or source["participant_config_id"] is None
        or source["config_owner"] != source["participant_id"]
        or source["config_provider"] != (
            sender_registration.provider if sender_registration is not None else None
        )
        or source["routing_mode"] != "explicit"
        or source["destination_kind"] != "participant"
        or source["recipient_participant_id"] is None
        or source["recipient_type"] != "ai"
        or source["participant_id"] == source["recipient_participant_id"]
        or source["sender_alias_owner"] != source["participant_id"]
        or source["destination_alias_owner"] != source["recipient_participant_id"]
        or sender_registration is None
        or responder_registration is None
        or source["participant_id"] not in active_ids
        or source["recipient_participant_id"] not in active_ids
    ):
        if source["destination_kind"] != "participant" or source["recipient_type"] != "ai":
            raise TurnServiceError(
                status_code=409,
                code="handoff_not_ai_to_ai",
                message="The latest message is not eligible for participant handoff.",
            )
        raise _recipient_unavailable()


def _recipient_unavailable() -> TurnServiceError:
    return TurnServiceError(
        status_code=409,
        code="handoff_recipient_unavailable",
        message="The addressed participant is unavailable for handoff.",
    )


def _reject_active_duplicate(
    connection: sqlite3.Connection, source_message_id: int
) -> None:
    rows = connection.execute(
        """SELECT ae.event_type FROM turns AS t
           JOIN api_events AS ae ON ae.turn_id=t.id AND ae.sequence_no=1
           WHERE t.status='open' AND ae.related_message_id=?
             AND ae.event_type IN (?, ?)""",
        (source_message_id, OPENAI_REQUEST_EVENT, GOOGLE_REQUEST_EVENT),
    ).fetchall()
    if rows:
        raise TurnServiceError(
            status_code=409,
            code="handoff_in_progress",
            message="A handoff for this source is already in progress.",
        )


async def _run_openai(
    database_path: Path | str,
    accepted: AcceptedHandoff,
    factory: Callable[[str], Any],
) -> int:
    client: Any | None = None
    try:
        client = factory(accepted.api_key)
        response = await create_response(
            client, json.loads(accepted.request_payload_json)["request"]
        )
    except asyncio.CancelledError as exception:
        raise _stranded(accepted) from exception
    except Exception as exception:
        timeout = _is_openai_timeout(exception)
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {
                "error": safe_exception_diagnostics(
                    exception,
                    reason="provider_timeout" if timeout else "provider_failure",
                    secrets=(accepted.api_key,),
                )
            },
        )
        raise TurnServiceError(
            status_code=504 if timeout else 502,
            code="provider_timeout" if timeout else "provider_failure",
            message=(
                "The Helios provider request timed out."
                if timeout
                else "The Helios provider request failed."
            ),
            turn_id=accepted.turn_id,
        ) from exception
    finally:
        if client is not None:
            try:
                await close_client(client)
            except Exception:
                pass
    try:
        raw = serialize_provider_response(response, api_key=accepted.api_key)
    except Exception as exception:
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {
                "error": safe_exception_diagnostics(
                    exception,
                    reason="provider_response_serialization_failed",
                    secrets=(accepted.api_key,),
                )
            },
        )
        raise TurnServiceError(
            status_code=502,
            code="provider_response_serialization_failed",
            message="The Helios provider response could not be recorded safely.",
            turn_id=accepted.turn_id,
        ) from exception
    status = getattr(response, "status", raw.get("status"))
    output = getattr(response, "output_text", raw.get("output_text"))
    if status != "completed" or not isinstance(output, str) or not output.strip():
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {"reason": "provider_unusable_response", "response": raw},
        )
        raise TurnServiceError(
            status_code=502,
            code="provider_unusable_response",
            message="The Helios provider returned an unusable response.",
            turn_id=accepted.turn_id,
        )
    try:
        return await asyncio.to_thread(
            _finalize_success, database_path, accepted, output, {"response": raw}
        )
    except Exception as exception:
        raise _stranded(accepted) from exception


async def _run_gemini(
    database_path: Path | str,
    accepted: AcceptedHandoff,
    factory: Callable[[str], Any],
) -> int:
    client: Any | None = None
    response: Any | None = None
    failure: Exception | None = None
    try:
        client = factory(accepted.api_key)
        response = await create_gemini_response(
            client,
            model=accepted.model,
            contents=contents_from_recorded(accepted.provider_input),
        )
    except asyncio.CancelledError as exception:
        raise _stranded(accepted) from exception
    except Exception as exception:
        failure = exception
    finally:
        if client is not None:
            try:
                await close_gemini_client(client)
            except Exception:
                pass
    if failure is not None:
        timeout = is_gemini_timeout(failure)
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {
                "error": safe_gemini_exception_diagnostics(
                    failure, timeout=timeout, api_key=accepted.api_key
                )
            },
        )
        raise TurnServiceError(
            status_code=504 if timeout else 502,
            code="gemini_provider_timeout" if timeout else "gemini_provider_failure",
            message=(
                "The Gemini provider request timed out."
                if timeout
                else "The Gemini provider request failed."
            ),
            turn_id=accepted.turn_id,
        ) from failure
    try:
        evaluation = serialize_and_evaluate_response(response, api_key=accepted.api_key)
    except GeminiResponseSerializationError as exception:
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {
                "error": {
                    "error_class": "ResponseSerializationError",
                    "reason": "gemini_provider_response_serialization_failed",
                    "summary": "The Gemini provider response could not be recorded safely.",
                }
            },
        )
        raise TurnServiceError(
            status_code=502,
            code="gemini_provider_response_serialization_failed",
            message="The Gemini provider response could not be recorded safely.",
            turn_id=accepted.turn_id,
        ) from exception
    if evaluation.failure_kind is not None or not isinstance(evaluation.output_text, str):
        await asyncio.to_thread(
            _finalize_failure,
            database_path,
            accepted,
            {
                "error": {
                    "failure_kind": evaluation.failure_kind,
                    "reason": "gemini_provider_unusable_response",
                    "summary": "The Gemini provider returned an unusable response.",
                },
                "response": evaluation.raw_response,
            },
        )
        raise TurnServiceError(
            status_code=502,
            code="gemini_provider_unusable_response",
            message="The Gemini provider returned an unusable response.",
            turn_id=accepted.turn_id,
        )
    try:
        return await asyncio.to_thread(
            _finalize_success,
            database_path,
            accepted,
            evaluation.output_text,
            {"response": evaluation.raw_response},
        )
    except Exception as exception:
        raise _stranded(accepted) from exception


def _assert_accepted(
    connection: sqlite3.Connection, accepted: AcceptedHandoff
) -> None:
    validate_bound_response_destination(connection, accepted.response_destination)
    turn = connection.execute(
        "SELECT room_id, initiated_by_participant_id, status FROM turns WHERE id=?",
        (accepted.turn_id,),
    ).fetchone()
    if (
        turn is None
        or tuple(turn) != (accepted.room_id, accepted.peter_id, "open")
        or connection.execute(
            "SELECT count(*) FROM messages WHERE turn_id=?", (accepted.turn_id,)
        ).fetchone()[0]
        != 0
    ):
        raise RuntimeError("handoff turn evidence changed")
    source = _load_source(connection, accepted.source_message_id)
    if (
        source is None
        or source["turn_id"] != accepted.source_turn_id
        or source["room_id"] != accepted.room_id
        or source["room_sequence_no"] != accepted.source_room_sequence
        or source["message_text"] != accepted.source_message_text
        or source["participant_id"] != accepted.source_sender_id
        or source["participant_key"] != accepted.source_sender_key
        or source["sender_alias_id"] != accepted.source_sender_alias_id
        or source["sender_display"] != accepted.source_sender_display
        or source["destination_alias_id"]
        != accepted.source_destination_alias_id
        or source["destination_display"]
        != accepted.source_destination_display
        or source["recipient_participant_id"] != accepted.responder_id
        or source["recipient_key"] != accepted.responder_key
    ):
        raise RuntimeError("handoff source evidence changed")
    responder_alias = connection.execute(
        "SELECT participant_id, display_alias FROM participant_aliases WHERE id=?",
        (accepted.responder_alias_id,),
    ).fetchall()
    if (
        len(responder_alias) != 1
        or tuple(responder_alias[0])
        != (accepted.responder_id, accepted.responder_display)
    ):
        raise RuntimeError("handoff responder alias evidence changed")
    events = connection.execute(
        """SELECT id, participant_id, participant_config_id, sequence_no,
                  event_type, related_message_id, payload_json, is_redacted,
                  tool_invocation_id FROM api_events WHERE turn_id=?""",
        (accepted.turn_id,),
    ).fetchall()
    expected_type = (
        OPENAI_REQUEST_EVENT if accepted.provider == "openai" else GOOGLE_REQUEST_EVENT
    )
    if (
        len(events) != 1
        or events[0]["id"] != accepted.request_event_id
        or events[0]["participant_id"] != accepted.responder_id
        or events[0]["participant_config_id"] != accepted.config_id
        or events[0]["sequence_no"] != 1
        or events[0]["event_type"] != expected_type
        or events[0]["related_message_id"] != accepted.source_message_id
        or events[0]["payload_json"] != accepted.request_payload_json
        or events[0]["is_redacted"]
        or events[0]["tool_invocation_id"] is not None
    ):
        raise RuntimeError("handoff request evidence changed")
    payload = json.loads(events[0]["payload_json"])
    if accepted.provider == "openai":
        validate_recorded_openai_shared_request_payload(payload)
        actual_input = payload["request"]["input"]
    else:
        validate_recorded_google_shared_request_payload(payload)
        actual_input = payload["request"]["contents"]
    if (
        payload["local_context"]["handoff_authorization"] != accepted.authorization
        or payload["local_context"]["handoff_version"] != HANDOFF_PROTOCOL_VERSION
        or payload["local_context"]["history_visibility"] != HISTORY_VISIBILITY_V4
        or payload["local_context"]["response_destination"]
        != accepted.response_destination.evidence()
        or actual_input != accepted.provider_input
    ):
        raise RuntimeError("handoff authorization evidence changed")
    config = connection.execute(
        """SELECT participant_id, provider, model, system_instructions,
                  settings_json, tools_json FROM participant_configs WHERE id=?""",
        (accepted.config_id,),
    ).fetchone()
    expected_instructions = (
        OPENAI_SYSTEM_INSTRUCTIONS_V3
        if accepted.provider == "openai"
        else GEMINI_SYSTEM_INSTRUCTIONS_V3
    )
    expected_settings = (
        canonical_json(OPENAI_RESPONSE_SETTINGS)
        if accepted.provider == "openai"
        else canonical_json(
            recorded_settings(max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS)
        )
    )
    if (
        config is None
        or config["participant_id"] != accepted.responder_id
        or config["provider"] != accepted.provider
        or config["model"] != accepted.model
        or config["system_instructions"] != expected_instructions
        or config["settings_json"] != expected_settings
        or config["tools_json"] != "[]"
    ):
        raise RuntimeError("handoff configuration evidence changed")


def _finalize_success(
    database_path: Path | str,
    accepted: AcceptedHandoff,
    output_text: str,
    payload: dict[str, Any],
) -> int:
    connection = _open_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_phase_a_foundation(connection)
        validate_database_integrity(connection)
        _assert_accepted(connection, accepted)
        message_id = store_message(
            connection,
            room_id=accepted.room_id,
            participant_id=accepted.responder_id,
            participant_config_id=accepted.config_id,
            message_text=output_text,
            turn_id=accepted.turn_id,
            reply_to_id=accepted.source_message_id,
            message_type="chat",
            sender_alias_id=accepted.responder_alias_id,
            destination_kind="participant",
            destination_alias_id=accepted.response_destination.destination_alias_id,
            recipient_participant_id=accepted.source_sender_id,
        )
        _insert_event(
            connection,
            turn_id=accepted.turn_id,
            room_id=accepted.room_id,
            participant_id=accepted.responder_id,
            config_id=accepted.config_id,
            sequence_no=2,
            event_type=(
                OPENAI_RESPONSE_EVENT
                if accepted.provider == "openai"
                else GOOGLE_RESPONSE_EVENT
            ),
            related_message_id=message_id,
            payload=payload,
        )
        _set_terminal(connection, accepted.turn_id, "completed")
        validate_v14_foundation(connection)
        validate_database_integrity(connection)
        connection.commit()
        return message_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finalize_failure(
    database_path: Path | str,
    accepted: AcceptedHandoff,
    payload: dict[str, Any],
) -> None:
    connection = _open_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_phase_a_foundation(connection)
        _assert_accepted(connection, accepted)
        _insert_event(
            connection,
            turn_id=accepted.turn_id,
            room_id=accepted.room_id,
            participant_id=accepted.responder_id,
            config_id=accepted.config_id,
            sequence_no=2,
            event_type=(
                OPENAI_ERROR_EVENT
                if accepted.provider == "openai"
                else GOOGLE_ERROR_EVENT
            ),
            related_message_id=accepted.source_message_id,
            payload=payload,
        )
        _set_terminal(connection, accepted.turn_id, "failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _insert_event(
    connection: sqlite3.Connection,
    *,
    turn_id: int,
    room_id: int,
    participant_id: int,
    config_id: int,
    sequence_no: int,
    event_type: str,
    related_message_id: int,
    payload: dict[str, Any],
) -> int:
    return connection.execute(
        """INSERT INTO api_events (
               turn_id, room_id, participant_id, participant_config_id,
               sequence_no, event_type, related_message_id, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            turn_id,
            room_id,
            participant_id,
            config_id,
            sequence_no,
            event_type,
            related_message_id,
            canonical_json(payload),
        ),
    ).lastrowid


def _set_terminal(connection: sqlite3.Connection, turn_id: int, status: str) -> None:
    cursor = connection.execute(
        """UPDATE turns SET status=?,
               completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           WHERE id=? AND status='open'""",
        (status, turn_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("handoff turn could not be finalized")


def _is_openai_timeout(exception: BaseException) -> bool:
    return isinstance(exception, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(
        exception
    ).__name__.lower()


def _stranded(accepted: AcceptedHandoff) -> TurnServiceError:
    return TurnServiceError(
        status_code=500,
        code="turn_finalization_failed",
        message="The provider may have responded, but this turn requires manual reconciliation.",
        turn_id=accepted.turn_id,
    )
