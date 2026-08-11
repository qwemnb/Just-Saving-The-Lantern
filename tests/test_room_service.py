from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from app.database import connect_database, create_turn, initialize_database, store_message
from app.openai_client import (
    OPENAI_MAX_RETRIES,
    OPENAI_TIMEOUT_SECONDS,
    create_openai_client,
    load_openai_environment,
)
from app.room_service import (
    HELIOS_KEY,
    PETER_KEY,
    RESPONSE_SETTINGS,
    RESPONSE_TOOLS,
    SYSTEM_INSTRUCTIONS,
    TurnServiceError,
    canonical_json,
    find_or_create_helios_configuration,
    run_helios_turn,
)


SENTINEL_API_KEY = "sk-test-HELIOS-SECRET-SENTINEL-7391"


class FakeResponse:
    def __init__(
        self,
        *,
        status: str,
        output_text: str | None,
        raw: dict[str, Any],
    ) -> None:
        self.status = status
        self.output_text = output_text
        self.raw = raw

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        if mode != "json":
            raise AssertionError("Responses must be serialized in JSON mode")
        return json.loads(json.dumps(self.raw))


class FakeResponses:
    def __init__(
        self,
        effect: FakeResponse | BaseException,
        *,
        on_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.effect = effect
        self.on_call = on_call
        self.calls: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> FakeResponse:
        self.calls.append(request)
        if self.on_call is not None:
            self.on_call(request)
        if isinstance(self.effect, BaseException):
            raise self.effect
        return self.effect


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.api_keys: list[str] = []

    def __call__(self, api_key: str) -> FakeClient:
        self.api_keys.append(api_key)
        return self.client


class ProviderRejection(RuntimeError):
    status_code = 403
    code = "model_not_available"
    request_id = "req_provider_rejection"


class RoomServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "helios.db"
        self.dotenv_path = Path(self.temporary_directory.name) / "missing.env"
        initialize_database(self.database_path)

    def _success_response(self, output_text: str = "Helios reply") -> FakeResponse:
        return FakeResponse(
            status="completed",
            output_text=output_text,
            raw={
                "id": "resp_test_success",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.6-luna-resolved",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
                "output": [
                    {
                        "id": "rs_test",
                        "type": "reasoning",
                        "encrypted_content": "encrypted-test-reasoning",
                    },
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": output_text}
                        ],
                    },
                ],
                "metadata": {"unsafe_echo": SENTINEL_API_KEY},
            },
        )

    def _factory(
        self,
        effect: FakeResponse | BaseException,
        *,
        on_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> RecordingFactory:
        return RecordingFactory(FakeClient(FakeResponses(effect, on_call=on_call)))

    def _run(
        self,
        factory: RecordingFactory,
        *,
        message_text: str = "Hello, Helios",
        model: str = "gpt-5.6-luna",
        database_path: Path | None = None,
    ) -> dict[str, Any]:
        environment = {
            "OPENAI_API_KEY": SENTINEL_API_KEY,
            "HELIOS_OPENAI_MODEL": model,
        }
        with patch.dict(os.environ, environment, clear=True):
            return asyncio.run(
                run_helios_turn(
                    message_text,
                    database_path=database_path or self.database_path,
                    client_factory=factory,
                    dotenv_path=self.dotenv_path,
                )
            )

    @staticmethod
    def _counts(database_path: Path) -> dict[str, int]:
        with closing(connect_database(database_path)) as connection:
            return {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("participant_configs", "turns", "messages", "api_events")
            }

    def test_successful_turn_records_canonical_history_and_full_provenance(self) -> None:
        output_text = "  Helios answer with preserved space  "
        response = self._success_response(output_text)

        def inspect_acceptance(request: dict[str, Any]) -> None:
            with closing(connect_database(self.database_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM api_events").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT status FROM turns").fetchone()[0],
                    "open",
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()

            self.assertEqual(
                set(request),
                {
                    "model",
                    "instructions",
                    "input",
                    "store",
                    "reasoning",
                    "max_output_tokens",
                    "tools",
                },
            )

        factory = self._factory(response, on_call=inspect_acceptance)
        result = self._run(factory, message_text="  Hello, Helios  ")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(factory.api_keys, [SENTINEL_API_KEY])
        self.assertEqual(len(factory.client.responses.calls), 1)
        self.assertTrue(factory.client.closed)

        provider_request = factory.client.responses.calls[0]
        self.assertEqual(provider_request["model"], "gpt-5.6-luna")
        self.assertEqual(provider_request["instructions"], SYSTEM_INSTRUCTIONS)
        self.assertEqual(
            provider_request["input"],
            [{"role": "user", "content": "  Hello, Helios  "}],
        )
        self.assertIs(provider_request["store"], False)
        self.assertEqual(
            provider_request["reasoning"],
            {"effort": "low", "context": "current_turn"},
        )
        self.assertEqual(provider_request["max_output_tokens"], 2048)
        self.assertEqual(provider_request["tools"], [])

        with closing(connect_database(self.database_path)) as connection:
            turn = connection.execute(
                "SELECT id, status, completed_at FROM turns"
            ).fetchone()
            messages = connection.execute(
                """
                SELECT m.id, m.turn_id, m.turn_sequence_no, m.message_text,
                       m.message_type, m.participant_config_id, m.reply_to_id,
                       p.participant_key
                FROM messages AS m
                JOIN participants AS p ON p.id = m.participant_id
                ORDER BY m.room_sequence_no
                """
            ).fetchall()
            events = connection.execute(
                """
                SELECT ae.sequence_no, ae.event_type, ae.related_message_id,
                       ae.participant_id, ae.participant_config_id,
                       ae.payload_json, p.participant_key
                FROM api_events AS ae
                JOIN participants AS p ON p.id = ae.participant_id
                ORDER BY ae.sequence_no
                """
            ).fetchall()
            config = connection.execute(
                """
                SELECT id, model, system_instructions, settings_json, tools_json
                FROM participant_configs
                WHERE model IS NOT NULL
                """
            ).fetchone()

        self.assertEqual(turn["status"], "completed")
        self.assertIsNotNone(turn["completed_at"])
        self.assertEqual([row["turn_sequence_no"] for row in messages], [1, 2])
        self.assertEqual([row["participant_key"] for row in messages], ["peter", "helios"])
        self.assertEqual(messages[0]["message_text"], "  Hello, Helios  ")
        self.assertEqual(messages[1]["message_text"], output_text)
        self.assertEqual(messages[1]["reply_to_id"], messages[0]["id"])
        self.assertEqual(messages[1]["participant_config_id"], config["id"])
        self.assertEqual([row["sequence_no"] for row in events], [1, 2])
        self.assertEqual(
            [row["event_type"] for row in events],
            ["openai.responses.request", "openai.responses.response"],
        )
        self.assertEqual(events[0]["related_message_id"], messages[0]["id"])
        self.assertEqual(events[1]["related_message_id"], messages[1]["id"])
        self.assertTrue(all(row["participant_key"] == HELIOS_KEY for row in events))
        self.assertTrue(
            all(row["participant_config_id"] == config["id"] for row in events)
        )

        request_event = json.loads(events[0]["payload_json"])
        response_event = json.loads(events[1]["payload_json"])
        self.assertEqual(set(request_event), {"request", "local_context"})
        self.assertEqual(request_event["request"], provider_request)
        self.assertEqual(
            request_event["local_context"],
            {
                "provider": "openai",
                "operation": "responses.create",
                "trigger_message_id": messages[0]["id"],
                "room_sequence_boundary": 1,
                "timeout_seconds": 120,
                "max_retries": 0,
            },
        )
        self.assertEqual(
            response_event["response"]["output"][0]["encrypted_content"],
            "encrypted-test-reasoning",
        )
        self.assertEqual(
            response_event["response"]["model"],
            "gpt-5.6-luna-resolved",
        )
        self.assertEqual(
            response_event["response"]["metadata"]["unsafe_echo"],
            "[REDACTED]",
        )
        self.assertNotIn(
            SENTINEL_API_KEY,
            "\n".join(row["payload_json"] for row in events),
        )
        self.assertEqual(config["model"], "gpt-5.6-luna")
        self.assertEqual(config["system_instructions"], SYSTEM_INSTRUCTIONS)
        self.assertEqual(config["settings_json"], canonical_json(RESPONSE_SETTINGS))
        self.assertEqual(config["tools_json"], canonical_json(RESPONSE_TOOLS))

    def test_client_boundary_sets_timeout_and_disables_sdk_retries(self) -> None:
        captured: dict[str, Any] = {}

        class ClientConstructor:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        client = create_openai_client(
            SENTINEL_API_KEY,
            client_class=ClientConstructor,
        )

        self.assertIsInstance(client, ClientConstructor)
        self.assertEqual(
            captured,
            {
                "api_key": SENTINEL_API_KEY,
                "timeout": OPENAI_TIMEOUT_SECONDS,
                "max_retries": OPENAI_MAX_RETRIES,
            },
        )

    def test_dotenv_does_not_override_process_environment(self) -> None:
        dotenv_path = Path(self.temporary_directory.name) / ".env"
        dotenv_path.write_text(
            "OPENAI_API_KEY=file-key\nHELIOS_OPENAI_MODEL=file-model\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "process-key",
                "HELIOS_OPENAI_MODEL": "process-model",
            },
            clear=True,
        ):
            environment = load_openai_environment(dotenv_path)

        self.assertEqual(environment.api_key, "process-key")
        self.assertEqual(environment.model, "process-model")

    def test_blank_message_is_ignored_before_missing_configuration(self) -> None:
        factory = self._factory(self._success_response())
        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(
                run_helios_turn(
                    " \t\r\n ",
                    database_path=self.database_path,
                    client_factory=factory,
                    dotenv_path=self.dotenv_path,
                )
            )

        self.assertEqual(result, {"ignored": True, "reason": "empty_message"})
        self.assertEqual(factory.api_keys, [])
        self.assertEqual(
            self._counts(self.database_path),
            {"participant_configs": 1, "turns": 0, "messages": 0, "api_events": 0},
        )

    def test_missing_key_or_model_creates_no_history(self) -> None:
        cases = (
            ({"HELIOS_OPENAI_MODEL": "gpt-5.6-luna"}, "missing_openai_api_key"),
            ({"OPENAI_API_KEY": SENTINEL_API_KEY}, "missing_openai_model"),
        )
        for environment, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                factory = self._factory(self._success_response())
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(TurnServiceError) as caught:
                        asyncio.run(
                            run_helios_turn(
                                "Hello",
                                database_path=self.database_path,
                                client_factory=factory,
                                dotenv_path=self.dotenv_path,
                            )
                        )
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(factory.api_keys, [])
                self.assertEqual(
                    self._counts(self.database_path),
                    {
                        "participant_configs": 1,
                        "turns": 0,
                        "messages": 0,
                        "api_events": 0,
                    },
                )

    def test_invalid_participant_identity_is_a_preflight_failure(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            connection.execute(
                "UPDATE participants SET name = 'Unexpected' WHERE participant_key = ?",
                (PETER_KEY,),
            )
            connection.commit()

        factory = self._factory(self._success_response())
        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.code, "invalid_participant_configuration")
        self.assertEqual(factory.api_keys, [])
        self.assertEqual(self._counts(self.database_path)["turns"], 0)

    def test_history_boundary_excludes_message_inserted_after_acceptance(self) -> None:
        def insert_later_message(request: dict[str, Any]) -> None:
            del request
            with closing(connect_database(self.database_path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                room_id = connection.execute(
                    "SELECT id FROM rooms WHERE room_key = 'main'"
                ).fetchone()[0]
                peter_id = connection.execute(
                    "SELECT id FROM participants WHERE participant_key = 'peter'"
                ).fetchone()[0]
                turn_id = create_turn(connection, room_id, peter_id)
                store_message(
                    connection,
                    room_id=room_id,
                    participant_id=peter_id,
                    turn_id=turn_id,
                    message_text="Later concurrent message",
                    message_type="chat",
                )
                connection.commit()

        factory = self._factory(
            self._success_response(),
            on_call=insert_later_message,
        )
        self._run(factory, message_text="Trigger boundary")

        self.assertEqual(
            factory.client.responses.calls[0]["input"],
            [{"role": "user", "content": "Trigger boundary"}],
        )
        with closing(connect_database(self.database_path)) as connection:
            texts = [
                row[0]
                for row in connection.execute(
                    "SELECT message_text FROM messages ORDER BY room_sequence_no"
                ).fetchall()
            ]
        self.assertEqual(
            texts,
            ["Trigger boundary", "Later concurrent message", "Helios reply"],
        )

    def test_unsupported_history_participant_rolls_back_phase_a(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key = 'main'"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO participants (participant_key, name, participant_type)
                VALUES ('visitor', 'Visitor', 'human')
                """
            )
            visitor_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO room_participants (room_id, participant_id) VALUES (?, ?)",
                (room_id, visitor_id),
            )
            turn_id = create_turn(connection, room_id, visitor_id)
            store_message(
                connection,
                room_id=room_id,
                participant_id=visitor_id,
                turn_id=turn_id,
                message_text="Unsupported participant history",
                message_type="chat",
            )
            connection.commit()

        before = self._counts(self.database_path)
        factory = self._factory(self._success_response())
        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        self.assertEqual(caught.exception.code, "unsupported_history_participant")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(factory.api_keys, [])
        self.assertEqual(self._counts(self.database_path), before)

    def test_unsupported_history_message_types_roll_back_phase_a(self) -> None:
        for message_type in ("system", "correction", "retraction"):
            with self.subTest(message_type=message_type), tempfile.TemporaryDirectory() as temp:
                database_path = Path(temp) / "helios.db"
                initialize_database(database_path)
                with closing(connect_database(database_path)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    room_id = connection.execute(
                        "SELECT id FROM rooms WHERE room_key = 'main'"
                    ).fetchone()[0]
                    peter_id = connection.execute(
                        "SELECT id FROM participants WHERE participant_key = 'peter'"
                    ).fetchone()[0]
                    if message_type == "system":
                        store_message(
                            connection,
                            room_id=room_id,
                            participant_id=peter_id,
                            message_text="Unsupported system history",
                            message_type="system",
                        )
                    else:
                        first_turn = create_turn(connection, room_id, peter_id)
                        first_id = store_message(
                            connection,
                            room_id=room_id,
                            participant_id=peter_id,
                            turn_id=first_turn,
                            message_text="Original",
                            message_type="chat",
                        )
                        next_turn = create_turn(connection, room_id, peter_id)
                        store_message(
                            connection,
                            room_id=room_id,
                            participant_id=peter_id,
                            turn_id=next_turn,
                            message_text=f"Unsupported {message_type}",
                            message_type=message_type,
                            reply_to_id=first_id,
                        )
                    connection.commit()

                before = self._counts(database_path)
                factory = self._factory(self._success_response())
                with self.assertRaises(TurnServiceError) as caught:
                    self._run(factory, database_path=database_path)

                self.assertEqual(
                    caught.exception.code,
                    "unsupported_history_message_type",
                )
                self.assertEqual(factory.api_keys, [])
                self.assertEqual(self._counts(database_path), before)

    def test_provider_exception_is_sanitized_and_fails_the_turn(self) -> None:
        factory = self._factory(RuntimeError(f"transport leaked {SENTINEL_API_KEY}"))
        captured_logs = io.StringIO()
        handler = logging.StreamHandler(captured_logs)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)

        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        error = caught.exception
        self.assertEqual(error.status_code, 502)
        self.assertEqual(error.code, "provider_failure")
        self.assertEqual(len(factory.client.responses.calls), 1)
        self.assertNotIn(SENTINEL_API_KEY, json.dumps(error.as_payload()))
        self.assertNotIn(SENTINEL_API_KEY, captured_logs.getvalue())

        with closing(connect_database(self.database_path)) as connection:
            turn = connection.execute("SELECT status, completed_at FROM turns").fetchone()
            messages = connection.execute(
                """
                SELECT p.participant_key, m.id
                FROM messages AS m
                JOIN participants AS p ON p.id = m.participant_id
                """
            ).fetchall()
            events = connection.execute(
                """
                SELECT sequence_no, event_type, related_message_id,
                       participant_id, participant_config_id, payload_json
                FROM api_events ORDER BY sequence_no
                """
            ).fetchall()

        self.assertEqual(turn["status"], "failed")
        self.assertIsNotNone(turn["completed_at"])
        self.assertEqual([row["participant_key"] for row in messages], [PETER_KEY])
        self.assertEqual([row["sequence_no"] for row in events], [1, 2])
        self.assertEqual(events[1]["event_type"], "openai.responses.error")
        self.assertEqual(events[1]["related_message_id"], messages[0]["id"])
        self.assertIsNotNone(events[1]["participant_id"])
        self.assertIsNotNone(events[1]["participant_config_id"])
        all_payloads = "\n".join(row["payload_json"] for row in events)
        self.assertNotIn(SENTINEL_API_KEY, all_payloads)
        diagnostics = json.loads(events[1]["payload_json"])["error"]
        self.assertEqual(diagnostics["error_class"], "RuntimeError")
        self.assertEqual(diagnostics["reason"], "provider_failure")
        self.assertNotIn("transport leaked", json.dumps(diagnostics))

    def test_provider_rejection_keeps_allowlisted_diagnostics(self) -> None:
        factory = self._factory(ProviderRejection("untrusted provider text"))
        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        self.assertEqual(caught.exception.status_code, 502)
        with closing(connect_database(self.database_path)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM api_events WHERE sequence_no = 2"
                ).fetchone()[0]
            )["error"]
        self.assertEqual(payload["http_status"], 403)
        self.assertEqual(payload["provider_error_code"], "model_not_available")
        self.assertEqual(payload["provider_request_id"], "req_provider_rejection")
        self.assertNotIn("untrusted provider text", json.dumps(payload))

    def test_provider_timeout_returns_504_and_records_one_error(self) -> None:
        factory = self._factory(TimeoutError("slow provider"))
        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(caught.exception.code, "provider_timeout")
        self.assertEqual(len(factory.client.responses.calls), 1)
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM api_events WHERE event_type = 'openai.responses.error'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "failed")

    def test_unusable_normal_responses_preserve_raw_response(self) -> None:
        cases = (
            (
                "blank_output",
                FakeResponse(
                    status="completed",
                    output_text=" \t ",
                    raw={"id": "blank", "status": "completed", "output": []},
                ),
            ),
            (
                "incomplete_response",
                FakeResponse(
                    status="incomplete",
                    output_text=None,
                    raw={"id": "incomplete-empty", "status": "incomplete", "output": []},
                ),
            ),
            (
                "incomplete_response",
                FakeResponse(
                    status="incomplete",
                    output_text="partial text",
                    raw={
                        "id": "incomplete-partial",
                        "status": "incomplete",
                        "output": [{"type": "message", "text": "partial text"}],
                    },
                ),
            ),
            (
                "refusal_without_text",
                FakeResponse(
                    status="completed",
                    output_text="",
                    raw={
                        "id": "refusal",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "refusal", "refusal": "Cannot comply"}
                                ],
                            }
                        ],
                    },
                ),
            ),
            (
                "provider_status_failed",
                FakeResponse(
                    status="failed",
                    output_text=None,
                    raw={"id": "failed", "status": "failed", "error": {"code": "x"}},
                ),
            ),
        )

        for index, (expected_reason, response) in enumerate(cases):
            with self.subTest(expected_reason=expected_reason, response_id=response.raw["id"]), tempfile.TemporaryDirectory() as temp:
                database_path = Path(temp) / f"case-{index}.db"
                initialize_database(database_path)
                factory = self._factory(response)
                with self.assertRaises(TurnServiceError) as caught:
                    self._run(factory, database_path=database_path)

                self.assertEqual(caught.exception.code, expected_reason)
                self.assertEqual(len(factory.client.responses.calls), 1)
                with closing(connect_database(database_path)) as connection:
                    error_event = connection.execute(
                        """
                        SELECT payload_json FROM api_events
                        WHERE event_type = 'openai.responses.error'
                        """
                    ).fetchone()
                    self.assertEqual(
                        connection.execute(
                            """
                            SELECT count(*) FROM messages AS m
                            JOIN participants AS p ON p.id = m.participant_id
                            WHERE p.participant_key = 'helios'
                            """
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        connection.execute("SELECT status FROM turns").fetchone()[0],
                        "failed",
                    )
                payload = json.loads(error_event["payload_json"])
                self.assertEqual(payload["reason"], expected_reason)
                self.assertEqual(payload["response"]["id"], response.raw["id"])

    def test_finalization_database_failure_leaves_stranded_open_turn(self) -> None:
        factory = self._factory(self._success_response())
        with patch(
            "app.room_service._finalize_success",
            side_effect=sqlite3.OperationalError("simulated disk failure"),
        ):
            with self.assertRaises(TurnServiceError) as caught:
                self._run(factory)

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.code, "turn_finalization_failed")
        self.assertEqual(len(factory.client.responses.calls), 1)
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "open")
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 1)

    def test_cancellation_after_provider_call_begins_never_resends(self) -> None:
        factory = self._factory(asyncio.CancelledError())
        with self.assertRaises(TurnServiceError) as caught:
            self._run(factory)

        self.assertEqual(caught.exception.code, "turn_finalization_failed")
        self.assertEqual(len(factory.client.responses.calls), 1)
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "open")
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 1)

    def test_semantically_identical_configuration_is_reused_without_label_identity(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key = 'helios'"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO participant_configs (
                    participant_id, provider, model, config_label,
                    system_instructions, settings_json, tools_json
                ) VALUES (?, 'openai', 'gpt-5.6-luna', ?, ?, ?, ?)
                """,
                (
                    helios_id,
                    "descriptive-label-not-identity",
                    SYSTEM_INSTRUCTIONS,
                    """
                    {
                      "reasoning": {"context": "current_turn", "effort": "low"},
                      "max_output_tokens": 2048,
                      "store": false
                    }
                    """,
                    " [ ] ",
                ),
            )
            expected_config_id = cursor.lastrowid
            connection.commit()

        factory = self._factory(self._success_response())
        self._run(factory)

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM participant_configs").fetchone()[0],
                2,
            )
            used_config_id = connection.execute(
                """
                SELECT participant_config_id FROM messages AS m
                JOIN participants AS p ON p.id = m.participant_id
                WHERE p.participant_key = 'helios'
                """
            ).fetchone()[0]
        self.assertEqual(used_config_id, expected_config_id)

    def test_changed_configuration_fields_create_new_versions(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key = 'helios'"
            ).fetchone()[0]
            base_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-luna",
                instructions=SYSTEM_INSTRUCTIONS,
                settings=RESPONSE_SETTINGS,
                tools=RESPONSE_TOOLS,
            )
            same_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-luna",
                instructions=SYSTEM_INSTRUCTIONS,
                settings=json.loads(
                    '{"reasoning":{"context":"current_turn","effort":"low"},'
                    '"max_output_tokens":2048,"store":false}'
                ),
                tools=[],
            )
            changed_model_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-sol",
                instructions=SYSTEM_INSTRUCTIONS,
                settings=RESPONSE_SETTINGS,
                tools=RESPONSE_TOOLS,
            )
            changed_instruction_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-luna",
                instructions=SYSTEM_INSTRUCTIONS + " Changed.",
                settings=RESPONSE_SETTINGS,
                tools=RESPONSE_TOOLS,
            )
            changed_setting_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-luna",
                instructions=SYSTEM_INSTRUCTIONS,
                settings={**RESPONSE_SETTINGS, "max_output_tokens": 4096},
                tools=RESPONSE_TOOLS,
            )
            changed_tools_id = find_or_create_helios_configuration(
                connection,
                helios_id=helios_id,
                model="gpt-5.6-luna",
                instructions=SYSTEM_INSTRUCTIONS,
                settings=RESPONSE_SETTINGS,
                tools=[{"type": "function", "name": "future-only"}],
            )
            connection.commit()

        self.assertEqual(base_id, same_id)
        self.assertEqual(
            len(
                {
                    base_id,
                    changed_model_id,
                    changed_instruction_id,
                    changed_setting_id,
                    changed_tools_id,
                }
            ),
            5,
        )

    def test_environment_model_switch_preserves_luna_provenance(self) -> None:
        luna_factory = self._factory(self._success_response("Luna answer"))
        sol_response = FakeResponse(
            status="completed",
            output_text="Sol answer",
            raw={
                "id": "resp_sol",
                "status": "completed",
                "model": "gpt-5.6-sol-resolved",
                "usage": {},
                "output": [],
            },
        )
        sol_factory = self._factory(sol_response)

        self._run(luna_factory, message_text="Luna turn", model="gpt-5.6-luna")
        self._run(sol_factory, message_text="Sol turn", model="gpt-5.6-sol")

        self.assertEqual(luna_factory.client.responses.calls[0]["model"], "gpt-5.6-luna")
        self.assertEqual(sol_factory.client.responses.calls[0]["model"], "gpt-5.6-sol")
        with closing(connect_database(self.database_path)) as connection:
            configurations = connection.execute(
                """
                SELECT id, model, config_label
                FROM participant_configs
                WHERE model IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
            helios_messages = connection.execute(
                """
                SELECT m.message_text, pc.model
                FROM messages AS m
                JOIN participants AS p ON p.id = m.participant_id
                JOIN participant_configs AS pc ON pc.id = m.participant_config_id
                WHERE p.participant_key = 'helios'
                ORDER BY m.room_sequence_no
                """
            ).fetchall()

        self.assertEqual([row["model"] for row in configurations], ["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual(
            [tuple(row) for row in helios_messages],
            [("Luna answer", "gpt-5.6-luna"), ("Sol answer", "gpt-5.6-sol")],
        )


if __name__ == "__main__":
    unittest.main()
