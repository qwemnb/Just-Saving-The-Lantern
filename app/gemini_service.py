"""One-call, three-phase Gemini participant turn orchestration."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .commands import classify_local_command, is_reserved_local_command
from .database import (
    DEFAULT_DATABASE_PATH,
    MessageVisibilityGuardError,
    connect_database,
    create_turn,
    is_blank_message,
    store_message,
)
from .gemini_client import (
    GEMINI_SYSTEM_INSTRUCTIONS_V3,
    GEMINI_THINKING_POLICY_VERSION,
    GEMINI_TIMEOUT_SECONDS,
    GEMINI_TOTAL_ATTEMPTS,
    GeminiResponseSerializationError,
    close_gemini_client,
    contents_from_recorded,
    create_gemini_client,
    create_gemini_response,
    generate_content_config,
    is_timeout_exception,
    load_gemini_environment,
    model_aware_request_contract,
    recorded_settings_from_request_config,
    safe_gemini_exception_diagnostics,
    serialize_and_evaluate_response,
    validate_recorded_google_shared_request_payload,
)
from .identity_service import current_alias, resolve_room_post_context
from .participant_registry import registration_for
from .provider_history import ProviderHistoryError, load_provider_history
from .request_validation import HISTORY_VISIBILITY_V4
from .room_service import TurnServiceError, canonical_json, validate_phase_a_foundation
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
    ResponseDestinationUnavailable,
    TURN_ROUTING_VERSION,
    build_gemini_provider_contents,
    resolve_response_destination,
    validate_bound_response_destination,
)
from .maintenance_lock import MaintenanceLockError, ResetRecoveryRequiredError


GEMINI_KEY = "gemini"
GOOGLE_PROVIDER = "google"
GOOGLE_REQUEST_EVENT = "google.generate_content.request"
GOOGLE_RESPONSE_EVENT = "google.generate_content.response"
GOOGLE_ERROR_EVENT = "google.generate_content.error"


@dataclass(frozen=True)
class AcceptedGeminiTurn:
    turn_id: int
    room_id: int
    peter_message_id: int
    peter_id: int
    gemini_id: int
    config_id: int
    gemini_alias_id: int
    response_destination: BoundResponseDestination
    model: str
    recorded_contents: list[dict[str, Any]]
    peter_message_text: str
    room_sequence_boundary: int
    request_event_id: int
    request_payload_json: str
    api_key: str


@dataclass(frozen=True)
class GeminiFinalizationResult:
    status: str
    message_id: int | None = None


async def run_gemini_turn(
    message_text: str,
    *,
    destination_participant_key: str = GEMINI_KEY,
    response_destination: Mapping[str, Any] | None = None,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    client_factory: Callable[[str], Any] | None = None,
    dotenv_path: Path | str | None = None,
) -> dict[str, Any]:
    if is_blank_message(message_text):
        return {"ignored": True, "reason": "empty_message"}
    if is_reserved_local_command(classify_local_command(message_text)):
        raise TurnServiceError(
            status_code=400,
            code="local_command_only",
            message="Local commands are available only in the browser interface.",
        )

    accepted = await asyncio.to_thread(
        _accept_gemini_turn,
        database_path,
        message_text,
        destination_participant_key,
        response_destination,
        dotenv_path,
    )
    factory = client_factory or create_gemini_client
    client: Any | None = None
    provider_exception: Exception | None = None
    response: Any | None = None
    try:
        accepted_payload = json.loads(accepted.request_payload_json)
        client = factory(accepted.api_key)
        response = await create_gemini_response(
            client,
            model=accepted_payload["request"]["model"],
            contents=contents_from_recorded(accepted_payload["request"]["contents"]),
            recorded_config=accepted_payload["request"]["config"],
        )
    except asyncio.CancelledError as exception:
        raise _stranded_error(accepted) from exception
    except Exception as exception:
        provider_exception = exception
    finally:
        if client is not None:
            try:
                await close_gemini_client(client)
            except Exception:
                pass

    try:
        finalization = await asyncio.to_thread(
            _finalize_phase_c,
            database_path,
            accepted,
            response,
            provider_exception,
        )
    except TurnServiceError:
        raise
    except Exception as exception:
        raise _stranded_error(accepted) from exception
    if finalization.status != "completed":
        contracts = {
            "provider_timeout": (504, "gemini_provider_timeout", "The Gemini provider request timed out."),
            "provider_failure": (502, "gemini_provider_failure", "The Gemini provider request failed."),
            "serialization_failure": (502, "gemini_provider_response_serialization_failed", "The Gemini provider response could not be recorded safely."),
            "unusable_response": (502, "gemini_provider_unusable_response", "The Gemini provider returned an unusable response."),
        }
        status_code, code, message = contracts[finalization.status]
        raise TurnServiceError(
            status_code=status_code,
            code=code,
            message=message,
            turn_id=accepted.turn_id,
            peter_message_id=accepted.peter_message_id,
        )
    return {
        "gemini_message_id": finalization.message_id,
        "peter_message_id": accepted.peter_message_id,
        "status": "completed",
        "turn_id": accepted.turn_id,
    }


def _accept_gemini_turn(
    database_path: Path | str,
    message_text: str,
    destination_participant_key: str,
    response_destination: Mapping[str, Any] | None,
    dotenv_path: Path | str | None,
) -> AcceptedGeminiTurn:
    try:
        connection = connect_database(database_path)
    except ResetRecoveryRequiredError as exception:
        raise TurnServiceError(
            status_code=503, code=exception.code, message=exception.message,
        ) from None
    except MaintenanceLockError as exception:
        raise TurnServiceError(
            status_code=503, code="database_maintenance_in_progress",
            message="The Helios Room database is unavailable during maintenance.",
        ) from exception
    except (OSError, sqlite3.Error) as exception:
        raise TurnServiceError(
            status_code=503,
            code="invalid_database_configuration",
            message="The Helios database configuration is invalid.",
        ) from exception
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
        registration = registration_for(destination_participant_key)
        rows = connection.execute(
            """SELECT id, participant_key, name, participant_type
               FROM participants WHERE participant_key=?""",
            (destination_participant_key,),
        ).fetchall()
        destination = rows[0] if len(rows) == 1 else None
        active = 0 if destination is None else connection.execute(
            """SELECT count(*) FROM room_participants
               WHERE room_id=? AND participant_id=? AND left_at IS NULL""",
            (context["room_id"], destination["id"]),
        ).fetchone()[0]
        if (
            registration is None
            or registration.participant_key != GEMINI_KEY
            or registration.provider != GOOGLE_PROVIDER
            or destination is None
            or destination["participant_key"] != GEMINI_KEY
            or destination["name"] != "Gemini"
            or destination["participant_type"] != "ai"
            or active != 1
        ):
            raise TurnServiceError(
                status_code=409,
                code="participant_destination_unavailable",
                message="The selected participant destination is unavailable.",
            )

        gemini_alias = current_alias(connection, destination["id"])
        try:
            response_route = resolve_response_destination(
                connection,
                room_id=context["room_id"],
                responding_participant_id=destination["id"],
                requested=response_destination,
            )
        except ResponseDestinationUnavailable:
            raise TurnServiceError(
                status_code=409,
                code="response_destination_unavailable",
                message="The selected response destination is unavailable.",
            ) from None

        turn_id = create_turn(connection, context["room_id"], context["peter"]["id"])
        peter_message_id = store_message(
            connection,
            room_id=context["room_id"],
            participant_id=context["peter"]["id"],
            message_text=message_text,
            turn_id=turn_id,
            message_type="chat",
            sender_alias_id=context["peter_alias"]["id"],
            destination_kind="participant",
            destination_alias_id=gemini_alias["id"],
            recipient_participant_id=destination["id"],
        )
        boundary = connection.execute(
            "SELECT room_sequence_no FROM messages WHERE id=?", (peter_message_id,)
        ).fetchone()[0]
        try:
            contents = load_provider_history(
                connection,
                room_id=context["room_id"],
                boundary=boundary,
                provider_participant_key=GEMINI_KEY,
                projection_version=PROVIDER_HISTORY_V4,
            )
        except ProviderHistoryError as error:
            raise TurnServiceError(
                status_code=409, code=error.code, message=error.message
            ) from error
        try:
            memory_search = search_seeded_memories(
                connection,
                destination["id"],
                message_text,
                result_limit=MEMORY_RESULT_LIMIT,
                text_budget_chars=MEMORY_TEXT_BUDGET_CHARS,
            )
            inherited_memory_context = (
                serialize_inherited_memory_context(memory_search.selected)
                if memory_search.selected
                else None
            )
            contents = build_gemini_provider_contents(
                contents,
                inherited_memory_context=inherited_memory_context,
                response_destination=response_route.evidence(),
            )
            memory_retrieval = memory_search.audit_envelope(
                owner_participant_id=destination["id"],
                query_source_message_id=peter_message_id,
                result_limit=MEMORY_RESULT_LIMIT,
                text_budget_chars=MEMORY_TEXT_BUDGET_CHARS,
            )
        except Exception as exception:
            raise TurnServiceError(
                status_code=500,
                code="memory_retrieval_failed",
                message="Seeded memory retrieval failed before the provider call.",
            ) from exception
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
        _thinking_policy, request_config, configuration_settings = (
            model_aware_request_contract(model, GEMINI_SYSTEM_INSTRUCTIONS_V3)
        )
        generate_content_config(request_config)
        config_id = find_or_create_gemini_configuration(
            connection,
            gemini_id=destination["id"],
            model=model,
            settings=configuration_settings,
        )
        # Typed content conversion is part of Phase A validation before acceptance.
        contents_from_recorded(contents)
        request = {
            "config": request_config,
            "contents": contents,
            "model": model,
        }
        request_payload = {
            "local_context": {
                "api_version": "v1beta",
                "memory_retrieval": memory_retrieval,
                "operation": "models.generate_content",
                "provider": GOOGLE_PROVIDER,
                "room_sequence_boundary": boundary,
                "safety_settings": "provider_default",
                "sdk_policy": {
                    "automatic_function_calling": {"disable": True}
                },
                "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
                "total_attempts": GEMINI_TOTAL_ATTEMPTS,
                "trigger_message_id": peter_message_id,
                "history_visibility": dict(HISTORY_VISIBILITY_V4),
                "gemini_thinking_policy_version": GEMINI_THINKING_POLICY_VERSION,
                "turn_routing_version": TURN_ROUTING_VERSION,
                "response_destination": response_route.evidence(),
            },
            "request": request,
        }
        validate_recorded_google_shared_request_payload(
            request_payload,
            _accepted_model=model,
            _accepted_config=request_config,
        )
        request_event_id = _insert_event(
            connection,
            accepted_ids=(turn_id, context["room_id"], destination["id"], config_id),
            sequence_no=1,
            event_type=GOOGLE_REQUEST_EVENT,
            related_message_id=peter_message_id,
            payload=request_payload,
        )
        connection.commit()
        return AcceptedGeminiTurn(
            turn_id=turn_id,
            room_id=context["room_id"],
            peter_message_id=peter_message_id,
            peter_id=context["peter"]["id"],
            gemini_id=destination["id"],
            config_id=config_id,
            gemini_alias_id=gemini_alias["id"],
            response_destination=response_route,
            model=model,
            recorded_contents=contents,
            peter_message_text=message_text,
            room_sequence_boundary=boundary,
            request_event_id=request_event_id,
            request_payload_json=canonical_json(request_payload),
            api_key=environment.api_key,
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


def find_or_create_gemini_configuration(
    connection: sqlite3.Connection,
    *,
    gemini_id: int,
    model: str,
    settings: Mapping[str, Any] | None = None,
) -> int:
    if settings is None:
        _policy, _request_config, settings = model_aware_request_contract(
            model, GEMINI_SYSTEM_INSTRUCTIONS_V3
        )
    settings_json = canonical_json(settings)
    tools = canonical_json([])
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") or "model"
    prefix = f"direct-address-google-{slug}-v"
    rows = connection.execute(
        """SELECT id, provider, model, config_label, system_instructions,
                  settings_json, tools_json
           FROM participant_configs WHERE participant_id=? ORDER BY id""",
        (gemini_id,),
    ).fetchall()
    versions: list[int] = []
    matches: list[int] = []
    for row in rows:
        label = row["config_label"]
        if isinstance(label, str) and label.startswith(prefix):
            suffix = label[len(prefix):]
            if not suffix.isdigit() or int(suffix) <= 0:
                raise TurnServiceError(
                    status_code=503,
                    code="invalid_database_configuration",
                    message="The Gemini participant configuration is invalid.",
                )
            versions.append(int(suffix))
            if (
                row["provider"] == GOOGLE_PROVIDER
                and row["model"] == model
                and row["system_instructions"] == GEMINI_SYSTEM_INSTRUCTIONS_V3
                and row["settings_json"] == settings_json
                and row["tools_json"] == tools
            ):
                matches.append(row["id"])
    if len(matches) > 1:
        raise TurnServiceError(
            status_code=503,
            code="invalid_database_configuration",
            message="The Gemini participant configuration is invalid.",
        )
    if matches:
        return matches[0]
    label = f"{prefix}{max(versions, default=0) + 1}"
    return connection.execute(
        """INSERT INTO participant_configs (
               participant_id, provider, model, config_label,
               system_instructions, settings_json, tools_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            gemini_id,
            GOOGLE_PROVIDER,
            model,
            label,
            GEMINI_SYSTEM_INSTRUCTIONS_V3,
            settings_json,
            tools,
        ),
    ).lastrowid


def _finalize_phase_c(
    database_path: Path | str,
    accepted: AcceptedGeminiTurn,
    response: Any | None,
    provider_exception: Exception | None,
) -> GeminiFinalizationResult:
    """Interpret and persist the provider outcome under one Phase C lock."""

    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_phase_a_foundation(connection)
        validate_database_integrity(connection)
        _assert_accepted_evidence(connection, accepted)

        if provider_exception is not None:
            timeout = is_timeout_exception(provider_exception)
            _insert_terminal_failure(
                connection,
                accepted,
                {
                    "error": safe_gemini_exception_diagnostics(
                        provider_exception,
                        timeout=timeout,
                        api_key=accepted.api_key,
                    )
                },
            )
            result = GeminiFinalizationResult(
                "provider_timeout" if timeout else "provider_failure"
            )
        else:
            try:
                evaluation = serialize_and_evaluate_response(
                    response, api_key=accepted.api_key
                )
            except GeminiResponseSerializationError:
                _insert_terminal_failure(
                    connection,
                    accepted,
                    {
                        "error": {
                            "error_class": "ResponseSerializationError",
                            "reason": "gemini_provider_response_serialization_failed",
                            "summary": "The Gemini provider response could not be recorded safely.",
                        }
                    },
                )
                result = GeminiFinalizationResult("serialization_failure")
            else:
                if evaluation.failure_kind is not None:
                    _insert_terminal_failure(
                        connection,
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
                    result = GeminiFinalizationResult("unusable_response")
                else:
                    if not isinstance(evaluation.output_text, str):
                        raise RuntimeError("Gemini output text is missing")
                    message_id = store_message(
                        connection,
                        room_id=accepted.room_id,
                        participant_id=accepted.gemini_id,
                        participant_config_id=accepted.config_id,
                        message_text=evaluation.output_text,
                        turn_id=accepted.turn_id,
                        reply_to_id=accepted.peter_message_id,
                        message_type="chat",
                        sender_alias_id=accepted.gemini_alias_id,
                        destination_kind=accepted.response_destination.kind,
                        destination_alias_id=accepted.response_destination.destination_alias_id,
                        recipient_participant_id=(
                            accepted.response_destination.recipient_participant_id
                        ),
                    )
                    _insert_event(
                        connection,
                        accepted_ids=(
                            accepted.turn_id,
                            accepted.room_id,
                            accepted.gemini_id,
                            accepted.config_id,
                        ),
                        sequence_no=2,
                        event_type=GOOGLE_RESPONSE_EVENT,
                        related_message_id=message_id,
                        payload={"response": evaluation.raw_response},
                    )
                    _set_terminal(connection, accepted.turn_id, "completed")
                    result = GeminiFinalizationResult("completed", message_id)
        validate_v14_foundation(connection)
        validate_database_integrity(connection)
        connection.commit()
        return result
    except MessageVisibilityGuardError:
        connection.rollback()
        raise TurnServiceError(
            status_code=409,
            code="unsupported_history_visibility_policy",
            message="Canonical history visibility cannot be reconstructed safely.",
            turn_id=accepted.turn_id,
            peter_message_id=accepted.peter_message_id,
        ) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _insert_terminal_failure(
    connection: sqlite3.Connection,
    accepted: AcceptedGeminiTurn,
    payload: dict[str, Any],
) -> None:
    _insert_event(
        connection,
        accepted_ids=(
            accepted.turn_id,
            accepted.room_id,
            accepted.gemini_id,
            accepted.config_id,
        ),
        sequence_no=2,
        event_type=GOOGLE_ERROR_EVENT,
        related_message_id=accepted.peter_message_id,
        payload=payload,
    )
    _set_terminal(connection, accepted.turn_id, "failed")


def _assert_accepted_evidence(
    connection: sqlite3.Connection, accepted: AcceptedGeminiTurn
) -> None:
    validate_bound_response_destination(connection, accepted.response_destination)
    row = connection.execute(
        "SELECT room_id, initiated_by_participant_id, status FROM turns WHERE id=?",
        (accepted.turn_id,),
    ).fetchone()
    if (
        row is None
        or row["room_id"] != accepted.room_id
        or row["initiated_by_participant_id"] != accepted.peter_id
        or row["status"] != "open"
    ):
        raise RuntimeError("Gemini turn is no longer open")
    message_rows = connection.execute(
        """SELECT m.id, m.room_id, m.participant_id, m.participant_config_id,
                  m.turn_id, m.room_sequence_no, m.message_type, m.message_text,
                  mr.sender_participant_id, mr.destination_kind,
                  mr.recipient_participant_id, mr.routing_mode,
                  sa.participant_id AS sender_alias_owner,
                  da.participant_id AS destination_alias_owner
           FROM messages AS m
           JOIN message_routes AS mr ON mr.message_id=m.id AND mr.room_id=m.room_id
           JOIN participant_aliases AS sa ON sa.id=mr.sender_alias_id
           JOIN participant_aliases AS da ON da.id=mr.destination_alias_id
           WHERE m.id=?""",
        (accepted.peter_message_id,),
    ).fetchall()
    if len(message_rows) != 1:
        raise RuntimeError("Gemini trigger evidence changed")
    message = message_rows[0]
    if (
        message["room_id"] != accepted.room_id
        or message["participant_id"] != accepted.peter_id
        or message["participant_config_id"] is not None
        or message["turn_id"] != accepted.turn_id
        or message["room_sequence_no"] != accepted.room_sequence_boundary
        or message["message_type"] != "chat"
        or message["message_text"] != accepted.peter_message_text
        or message["sender_participant_id"] != accepted.peter_id
        or message["sender_alias_owner"] != accepted.peter_id
        or message["destination_kind"] != "participant"
        or message["recipient_participant_id"] != accepted.gemini_id
        or message["destination_alias_owner"] != accepted.gemini_id
        or message["routing_mode"] != "explicit"
    ):
        raise RuntimeError("Gemini trigger evidence changed")
    event_rows = connection.execute(
        """SELECT id, turn_id, room_id, participant_id, participant_config_id,
                  sequence_no, event_type, related_message_id, payload_json,
                  tool_invocation_id, is_redacted
           FROM api_events WHERE turn_id=?""",
        (accepted.turn_id,),
    ).fetchall()
    if len(event_rows) != 1:
        raise RuntimeError("Gemini request evidence changed")
    event = event_rows[0]
    if (
        event["id"] != accepted.request_event_id
        or event["room_id"] != accepted.room_id
        or event["participant_id"] != accepted.gemini_id
        or event["participant_config_id"] != accepted.config_id
        or event["sequence_no"] != 1
        or event["event_type"] != GOOGLE_REQUEST_EVENT
        or event["related_message_id"] != accepted.peter_message_id
        or event["payload_json"] != accepted.request_payload_json
        or event["tool_invocation_id"] is not None
        or event["is_redacted"]
    ):
        raise RuntimeError("Gemini request evidence changed")
    validate_recorded_google_shared_request_payload(json.loads(event["payload_json"]))
    request_payload = json.loads(event["payload_json"])
    if (
        request_payload["local_context"]["response_destination"]
        != accepted.response_destination.evidence()
        or request_payload["request"]["contents"] != accepted.recorded_contents
    ):
        raise RuntimeError("Gemini routing evidence changed")
    config_rows = connection.execute(
        """SELECT participant_id, provider, model, config_label,
                  system_instructions, settings_json, tools_json
           FROM participant_configs WHERE id=?""",
        (accepted.config_id,),
    ).fetchall()
    if len(config_rows) != 1:
        raise RuntimeError("Gemini configuration evidence changed")
    config = config_rows[0]
    slug = re.sub(r"[^a-z0-9]+", "-", accepted.model.lower()).strip("-") or "model"
    prefix = f"direct-address-google-{slug}-v"
    label = config["config_label"]
    if (
        config["participant_id"] != accepted.gemini_id
        or config["provider"] != GOOGLE_PROVIDER
        or config["model"] != accepted.model
        or config["system_instructions"] != GEMINI_SYSTEM_INSTRUCTIONS_V3
        or config["settings_json"]
        != canonical_json(
            recorded_settings_from_request_config(
                request_payload["request"]["config"]
            )
        )
        or config["tools_json"] != "[]"
        or not isinstance(label, str)
        or not label.startswith(prefix)
        or not label[len(prefix):].isdigit()
        or int(label[len(prefix):]) <= 0
    ):
        raise RuntimeError("Gemini configuration evidence changed")


def _set_terminal(connection: sqlite3.Connection, turn_id: int, status: str) -> None:
    cursor = connection.execute(
        """UPDATE turns SET status=?,
               completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           WHERE id=? AND status='open'""",
        (status, turn_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Gemini turn could not be finalized")


def _insert_event(
    connection: sqlite3.Connection,
    *,
    accepted_ids: tuple[int, int, int, int],
    sequence_no: int,
    event_type: str,
    related_message_id: int,
    payload: dict[str, Any],
) -> int:
    turn_id, room_id, participant_id, config_id = accepted_ids
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


def _stranded_error(accepted: AcceptedGeminiTurn) -> TurnServiceError:
    return TurnServiceError(
        status_code=500,
        code="turn_finalization_failed",
        message="The provider may have responded, but this turn requires manual reconciliation.",
        turn_id=accepted.turn_id,
        peter_message_id=accepted.peter_message_id,
    )
