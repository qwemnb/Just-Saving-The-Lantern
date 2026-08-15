from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import main
from app.commands import LocalCommandKind, classify_local_command
from app.database import connect_database, initialize_database
from app.room_service import (
    RESPONSE_SETTINGS,
    RESPONSE_TOOLS,
    SYSTEM_INSTRUCTIONS,
    TurnServiceError,
    run_helios_turn,
)
from app.seed_memory import build_fts_query, tokenize_memory_query
from app.trace_service import (
    TraceServiceError,
    load_trace,
    project_trace_json,
)


class TraceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "trace.db"
        initialize_database(self.database_path)
        with closing(connect_database(self.database_path)) as connection:
            self.room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key = 'main'"
            ).fetchone()[0]
            participants = connection.execute(
                "SELECT id, participant_key FROM participants"
            ).fetchall()
            by_key = {row["participant_key"]: row["id"] for row in participants}
            self.peter_id = by_key["peter"]
            self.helios_id = by_key["helios"]
            self.initial_config_id = connection.execute(
                "SELECT id FROM participant_configs WHERE participant_id = ?",
                (self.helios_id,),
            ).fetchone()[0]

    def shared_openai_request(
        self, message_id: int, text: str, boundary: int,
        *, model: str = "historical-request-model",
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        terms = tokenize_memory_query(text)
        return {
            "request": {
                "model": model,
                "instructions": SYSTEM_INSTRUCTIONS,
                "input": history or [{"role": "user", "content": text}],
                "store": RESPONSE_SETTINGS["store"],
                "reasoning": dict(RESPONSE_SETTINGS["reasoning"]),
                "max_output_tokens": RESPONSE_SETTINGS["max_output_tokens"],
                "tools": list(RESPONSE_TOOLS),
            },
            "local_context": {
                "provider": "openai", "operation": "responses.create",
                "trigger_message_id": message_id,
                "room_sequence_boundary": boundary,
                "timeout_seconds": 120, "max_retries": 0,
                "memory_retrieval": {
                    "retriever_version": "seed-fts-topic-v1",
                    "owner_participant_id": self.helios_id,
                    "query_source_message_id": message_id,
                    "query_terms": terms,
                    "fts_query": build_fts_query(terms),
                    "result_limit": 5, "text_budget_chars": 8000,
                    "omitted_for_budget": 0, "selected": [],
                },
                "history_visibility": {
                    "active_policy_version": "room_shared_v1",
                    "effective_from_room_sequence_no": 1,
                    "projection_version": "provider_history_v2",
                },
            },
        }

    def add_turn(
        self,
        *,
        status: str = "open",
        initiated_by: int | None | object = ...,
    ) -> int:
        if initiated_by is ...:
            initiated_by = self.peter_id
        completed_at = None if status == "open" else "2026-08-11T23:00:02.000Z"
        with closing(connect_database(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO turns (
                    room_id, initiated_by_participant_id, status,
                    created_at, completed_at
                )
                VALUES (?, ?, ?, '2026-08-11T23:00:00.000Z', ?)
                """,
                (self.room_id, initiated_by, status, completed_at),
            )
            connection.commit()
            return cursor.lastrowid

    def add_config(
        self,
        *,
        settings_json: str = json.dumps(RESPONSE_SETTINGS, sort_keys=True, separators=(",", ":")),
        tools_json: str = "[]",
        model: str | None = "historical-model",
    ) -> int:
        slug = model or "model"
        with closing(connect_database(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO participant_configs (
                    participant_id, provider, model, config_label,
                    system_instructions, settings_json, tools_json,
                    created_at
                )
                VALUES (?, 'openai', ?, ?, ?,
                        ?, ?, '2026-08-11T22:59:59.000Z')
                """,
                (
                    self.helios_id, model, f"seed-memory-openai-{slug}-v1",
                    SYSTEM_INSTRUCTIONS, settings_json, tools_json,
                ),
            )
            connection.commit()
            return cursor.lastrowid

    def add_message(
        self,
        turn_id: int,
        text: str,
        *,
        participant_id: int | None = None,
        config_id: int | None = None,
        reply_to_id: int | None = None,
    ) -> int:
        participant_id = participant_id or self.peter_id
        with closing(connect_database(self.database_path)) as connection:
            room_sequence = connection.execute(
                "SELECT COALESCE(MAX(room_sequence_no), 0) + 1 FROM messages"
            ).fetchone()[0]
            turn_sequence = connection.execute(
                "SELECT COALESCE(MAX(turn_sequence_no), 0) + 1 FROM messages WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    turn_id, room_id, room_sequence_no, turn_sequence_no,
                    participant_id, participant_config_id, reply_to_id,
                    message_type, message_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'chat', ?,
                          '2026-08-11T23:00:01.000Z')
                """,
                (
                    turn_id,
                    self.room_id,
                    room_sequence,
                    turn_sequence,
                    participant_id,
                    config_id,
                    reply_to_id,
                    text,
                ),
            )
            sender_alias_id = connection.execute(
                "SELECT alias_id FROM participant_primary_aliases WHERE participant_id=?",
                (participant_id,),
            ).fetchone()[0]
            participant_key = connection.execute(
                "SELECT participant_key FROM participants WHERE id=?",
                (participant_id,),
            ).fetchone()[0]
            recipient_key = "peter" if participant_key == "helios" else "helios"
            recipient = connection.execute(
                """SELECT p.id, ppa.alias_id FROM participants AS p
                   JOIN participant_primary_aliases AS ppa ON ppa.participant_id=p.id
                   WHERE p.participant_key=?""",
                (recipient_key,),
            ).fetchone()
            connection.execute(
                """INSERT INTO message_routes
                   (message_id,room_id,sender_participant_id,sender_alias_id,
                    destination_kind,recipient_participant_id,destination_alias_id,routing_mode)
                   VALUES (?,?,?,?,'participant',?,?,'explicit')""",
                (cursor.lastrowid, self.room_id, participant_id, sender_alias_id,
                 recipient["id"], recipient["alias_id"]),
            )
            connection.commit()
            return cursor.lastrowid

    def add_event(
        self,
        turn_id: int,
        sequence_no: int,
        event_type: str,
        payload: object,
        *,
        config_id: int | None = None,
        participant_id: int | None = None,
        related_message_id: int | None = None,
        redacted: bool = False,
    ) -> int:
        with closing(connect_database(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_events (
                    turn_id, room_id, participant_id, participant_config_id,
                    sequence_no, event_type, related_message_id, payload_json,
                    is_redacted, redacted_at, redaction_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          '2026-08-11T23:00:01.500Z')
                """,
                (
                    turn_id,
                    self.room_id,
                    participant_id,
                    config_id,
                    sequence_no,
                    event_type,
                    related_message_id,
                    json.dumps(payload, ensure_ascii=False),
                    int(redacted),
                    "2026-08-11T23:00:01.600Z" if redacted else None,
                    "security cleanup" if redacted else None,
                ),
            )
            connection.commit()
            return cursor.lastrowid

    def standard_trace(self) -> tuple[int, int, int]:
        earlier_turn = self.add_turn()
        earlier_message = self.add_message(earlier_turn, "Earlier target")
        turn_id = self.add_turn(status="completed")
        peter_message = self.add_message(
            turn_id,
            "  Exact Peter text\nnext line  ",
            reply_to_id=earlier_message,
        )
        config_id = self.add_config(
            model="historical-request-model"
        )
        helios_message = self.add_message(
            turn_id,
            "Visible Helios output",
            participant_id=self.helios_id,
            config_id=config_id,
            reply_to_id=peter_message,
        )
        self.add_event(
            turn_id,
            1,
            "openai.responses.request",
            self.shared_openai_request(
                peter_message, "  Exact Peter text\nnext line  ", 2,
                history=[
                    {"role": "user", "content": "Earlier target"},
                    {"role": "user", "content": "  Exact Peter text\nnext line  "},
                ],
            ),
            config_id=config_id,
            participant_id=self.helios_id,
            related_message_id=peter_message,
        )
        self.add_event(
            turn_id,
            2,
            "openai.responses.response",
            {
                "response": {
                    "id": "resp_recorded",
                    "status": "completed",
                    "model": "historical-resolved-model",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 6,
                        "total_tokens": 16,
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                    "incomplete_details": None,
                    "service_tier": "default",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "reason_1",
                            "status": "completed",
                            "content": [{"reasoning_text": "raw chain"}],
                            "encrypted_content": "opaque",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "Recorded provider summary",
                                    "token": "secret",
                                }
                            ],
                            "unfamiliar_raw": "must disappear",
                        },
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "Visible output"}
                            ],
                        },
                    ],
                }
            },
            config_id=config_id,
            participant_id=self.helios_id,
            related_message_id=helios_message,
        )
        self.add_event(
            turn_id,
            3,
            "future.scalar",
            17,
            related_message_id=earlier_message,
        )
        return turn_id, peter_message, config_id

    async def asgi_get(self, path: str) -> tuple[int, dict[str, str], object]:
        messages: list[dict[str, object]] = []
        received = False

        async def receive() -> dict[str, object]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "root_path": "",
            "app": main.app,
        }
        await main.app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start["headers"]
        }
        return int(start["status"]), headers, json.loads(body)


class TraceCommandTests(TraceFixture):
    def test_python_classifier_exact_grammar_and_near_misses(self) -> None:
        cases = {
            "/trace": (LocalCommandKind.TRACE_LATEST, None),
            " \t/trace\n": (LocalCommandKind.TRACE_LATEST, None),
            "/trace 17": (LocalCommandKind.TRACE_TURN, "17"),
            "/trace\n999": (LocalCommandKind.TRACE_TURN, "999"),
            "/trace 0": (LocalCommandKind.MALFORMED_TRACE, None),
            "/trace 01": (LocalCommandKind.MALFORMED_TRACE, None),
            "/trace -1": (LocalCommandKind.MALFORMED_TRACE, None),
            "/trace 1 extra": (LocalCommandKind.MALFORMED_TRACE, None),
            "/TRACE": (LocalCommandKind.NON_COMMAND, None),
            "/tracefoo": (LocalCommandKind.NON_COMMAND, None),
            "/trace/1": (LocalCommandKind.NON_COMMAND, None),
            "hello /trace": (LocalCommandKind.NON_COMMAND, None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = classify_local_command(text)
                self.assertEqual((result.kind, result.turn_id_text), expected)

    def test_api_intercepts_valid_and_malformed_before_all_side_effects(self) -> None:
        for text in ("/trace", "/trace 17", "/trace 0", "/trace nope"):
            with self.subTest(text=text), patch(
                "app.room_service.load_openai_environment",
                side_effect=AssertionError("environment must not load"),
            ), patch(
                "app.room_service.connect_database",
                side_effect=AssertionError("database must not open"),
            ):
                with self.assertRaises(TurnServiceError) as raised:
                    asyncio.run(
                        run_helios_turn(
                            text,
                            database_path=self.database_path,
                            client_factory=lambda _: (_ for _ in ()).throw(
                                AssertionError("provider must not run")
                            ),
                        )
                    )
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(raised.exception.code, "local_command_only")

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM admin_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM participants").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM participant_configs").fetchone()[0], 1)

        accepted_turn = self.add_turn()
        accepted_message = self.add_message(accepted_turn, "Subsequent valid message")
        with closing(connect_database(self.database_path)) as connection:
            sequence = connection.execute(
                "SELECT room_sequence_no FROM messages WHERE id = ?",
                (accepted_message,),
            ).fetchone()[0]
        self.assertEqual(sequence, 1)

    def test_cli_rejects_trace_before_creating_a_database_path(self) -> None:
        for text in ("/trace", "/trace 17", "/trace 00", "/trace extra"):
            target = Path(self.temporary_directory.name) / text.replace("/", "x").replace(" ", "_") / "new.db"
            argv = [
                "helios-room",
                "store-message",
                "--database",
                str(target),
                "--message",
                text,
                "--destination-kind",
                "room",
            ]
            with self.subTest(text=text), patch.object(sys, "argv", argv), patch(
                "sys.stderr", new_callable=io.StringIO
            ) as error_output:
                with self.assertRaises(SystemExit) as raised:
                    main.main()
                self.assertNotEqual(raised.exception.code, 0)
                self.assertIn("browser interface", error_output.getvalue())
                self.assertFalse(target.exists())
                self.assertFalse(target.parent.exists())


class TraceProjectionTests(TraceFixture):
    def test_complete_historical_trace_fidelity_ordering_and_projection(self) -> None:
        turn_id, peter_message, config_id = self.standard_trace()
        with closing(connect_database(self.database_path)) as connection:
            before_json = [
                row[0]
                for row in connection.execute(
                    "SELECT payload_json FROM api_events ORDER BY sequence_no"
                ).fetchall()
            ]

        trace = load_trace(self.database_path, turn_id)

        self.assertEqual(trace["trace_version"], 3)
        self.assertEqual(trace["turn"]["id"], turn_id)
        self.assertEqual(trace["turn"]["initiated_by"]["participant_key"], "peter")
        self.assertEqual(
            [message["turn_sequence_no"] for message in trace["messages"]],
            [1, 2],
        )
        self.assertEqual(trace["messages"][0]["message_text"], "  Exact Peter text\nnext line  ")
        self.assertEqual(trace["messages"][0]["reply_to_outside_selected_turn"], True)
        self.assertNotEqual(trace["messages"][0]["reply_to_turn_id"], turn_id)
        self.assertEqual(trace["messages"][1]["reply_to_turn_id"], turn_id)
        self.assertEqual([item["id"] for item in trace["configurations"]], [config_id])
        config = trace["configurations"][0]
        self.assertEqual(config["system_instructions"], SYSTEM_INSTRUCTIONS)
        self.assertEqual(config["settings"], RESPONSE_SETTINGS)
        self.assertEqual(config["omitted_json_pointers"], [])
        self.assertEqual([event["sequence_no"] for event in trace["api_events"]], [1, 2, 3])
        self.assertEqual(trace["api_events"][2]["payload"], 17)
        self.assertEqual(trace["api_events"][2]["related_message_outside_selected_turn"], True)
        self.assertNotEqual(trace["api_events"][2]["related_message_turn_id"], turn_id)
        request = trace["recorded_request"]
        self.assertEqual(request["request"]["model"], "historical-request-model")
        self.assertEqual(
            request["local_context"]["history_visibility"]["projection_version"],
            "provider_history_v2",
        )
        outcome = trace["provider_outcome"]
        self.assertEqual(outcome["response_id"], "resp_recorded")
        self.assertEqual(outcome["requested_model"], "historical-request-model")
        self.assertEqual(outcome["resolved_model"], "historical-resolved-model")
        self.assertEqual(outcome["output_text"], "Visible output")
        self.assertEqual(outcome["usage"]["output_tokens_details"]["reasoning_tokens"], 2)
        response_event = trace["api_events"][1]
        reasoning = response_event["payload"]["response"]["output"][0]
        self.assertEqual(reasoning["summary_label"], "Provider-generated reasoning summary")
        self.assertEqual(reasoning["summary"][0]["text"], "Recorded provider summary")
        self.assertNotIn("content", reasoning)
        self.assertNotIn("encrypted_content", reasoning)
        self.assertNotIn("unfamiliar_raw", reasoning)
        self.assertIn("/response/output/0/content", response_event["omitted_json_pointers"])
        self.assertIn("/response/output/0/encrypted_content", response_event["omitted_json_pointers"])
        self.assertIn("/response/output/0/summary/0/token", response_event["omitted_json_pointers"])

        with closing(connect_database(self.database_path)) as connection:
            after_json = [
                row[0]
                for row in connection.execute(
                    "SELECT payload_json FROM api_events ORDER BY sequence_no"
                ).fetchall()
            ]
        self.assertEqual(after_json, before_json)
        self.assertEqual(peter_message, trace["recorded_request"]["local_context"]["trigger_message_id"])
        self.assertEqual(trace["recorded_request"]["local_context"]["room_sequence_boundary"], 2)

        later_turn = self.add_turn()
        self.add_message(later_turn, "Later canonical history")
        after_later_history = load_trace(self.database_path, turn_id)
        self.assertEqual(
            after_later_history["recorded_request"]["request"]["input"],
            [
                {"role": "user", "content": "Earlier target"},
                {"role": "user", "content": "  Exact Peter text\nnext line  "},
            ],
        )

    def test_null_initiator_human_only_open_and_cancelled_turns(self) -> None:
        open_turn = self.add_turn(initiated_by=None)
        self.add_message(open_turn, "Human-only")
        open_trace = load_trace(self.database_path, open_turn)
        self.assertIsNone(open_trace["turn"]["initiated_by"])
        self.assertEqual(open_trace["turn"]["status"], "open")
        self.assertEqual(open_trace["configurations"], [])
        self.assertIsNone(open_trace["recorded_request"])
        self.assertIsNone(open_trace["provider_outcome"])

        cancelled = self.add_turn(status="cancelled")
        cancelled_trace = load_trace(self.database_path, cancelled)
        self.assertEqual(cancelled_trace["turn"]["status"], "cancelled")
        self.assertEqual(cancelled_trace["messages"], [])

    def test_arbitrary_json_types_remain_types(self) -> None:
        turn_id = self.add_turn()
        config_id = self.add_config(settings_json="null", tools_json="42")
        self.add_event(
            turn_id,
            1,
            "future.boolean",
            False,
            config_id=config_id,
            participant_id=self.helios_id,
        )
        trace = load_trace(self.database_path, turn_id)
        self.assertIsNone(trace["configurations"][0]["settings"])
        self.assertEqual(trace["configurations"][0]["tools"], 42)
        self.assertIs(trace["api_events"][0]["payload"], False)

    def test_redacted_recognized_events_are_honest_and_count_for_cardinality(self) -> None:
        turn_id = self.add_turn(status="failed")
        peter_message = self.add_message(turn_id, "Redacted request")
        config_id = self.add_config(model="redacted-model")
        self.add_event(
            turn_id,
            1,
            "openai.responses.request",
            None,
            config_id=config_id,
            participant_id=self.helios_id,
            related_message_id=peter_message,
            redacted=True,
        )
        self.add_event(
            turn_id,
            2,
            "openai.responses.error",
            "removed",
            config_id=config_id,
            participant_id=self.helios_id,
            related_message_id=peter_message,
            redacted=True,
        )
        trace = load_trace(self.database_path, turn_id)
        self.assertTrue(trace["recorded_request"]["is_redacted"])
        self.assertIsNone(trace["recorded_request"]["request"])
        self.assertTrue(trace["provider_outcome"]["is_redacted"])
        self.assertIsNone(trace["provider_outcome"]["requested_model"])

        second_turn = self.add_turn()
        self.add_event(second_turn, 1, "openai.responses.request", None, redacted=True)
        self.add_event(second_turn, 2, "openai.responses.request", {}, redacted=True)
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, second_turn)
        self.assertEqual(raised.exception.code, "trace_data_invalid")

    def test_open_stranded_request_does_not_invent_an_outcome(self) -> None:
        turn_id = self.add_turn()
        peter_message = self.add_message(turn_id, "Accepted before interruption")
        config_id = self.add_config(model="recorded")
        self.add_event(
            turn_id,
            1,
            "openai.responses.request",
            self.shared_openai_request(
                peter_message, "Accepted before interruption", 1, model="recorded"
            ),
            participant_id=self.helios_id,
            config_id=config_id,
            related_message_id=peter_message,
        )
        trace = load_trace(self.database_path, turn_id)
        self.assertEqual(trace["turn"]["status"], "open")
        self.assertIsNotNone(trace["recorded_request"])
        self.assertIsNone(trace["provider_outcome"])
        self.assertEqual(len(trace["messages"]), 1)

    def test_terminal_without_request_and_error_allowlist(self) -> None:
        turn_id = self.add_turn(status="failed")
        peter_message = self.add_message(turn_id, "Accepted request")
        config_id = self.add_config(model="recorded")
        self.add_event(
            turn_id,
            1,
            "openai.responses.request",
            self.shared_openai_request(
                peter_message, "Accepted request", 1, model="recorded"
            ),
            participant_id=self.helios_id,
            config_id=config_id,
            related_message_id=peter_message,
        )
        self.add_event(
            turn_id,
            2,
            "openai.responses.error",
            {
                "error": {
                    "reason": "provider_failure",
                    "error_class": "RateLimitError",
                    "http_status": 429,
                    "provider_error_code": "rate_limit",
                    "provider_request_id": "req_safe",
                    "summary": "Provider request failed.",
                    "traceback": "C:/private/file.py",
                    "sql_dump": "SELECT secret FROM private",
                },
                "exception_object": {"anything": "unsafe"},
            },
            participant_id=self.helios_id,
            config_id=config_id,
            related_message_id=peter_message,
        )
        trace = load_trace(self.database_path, turn_id)
        outcome = trace["provider_outcome"]
        self.assertEqual(outcome["requested_model"], "recorded")
        self.assertEqual(outcome["error"]["http_status"], 429)
        serialized = json.dumps(trace)
        self.assertNotIn("private/file", serialized)
        self.assertNotIn("SELECT secret", serialized)
        self.assertNotIn("unsafe\"", serialized)
        self.assertIn("/error/traceback", trace["api_events"][1]["omitted_json_pointers"])
        self.assertIn("/exception_object", trace["api_events"][1]["omitted_json_pointers"])

    def test_unusable_response_error_shape_is_allowlisted(self) -> None:
        turn_id = self.add_turn(status="failed")
        peter_message = self.add_message(turn_id, "Accepted request")
        config_id = self.add_config(model="recorded")
        self.add_event(
            turn_id,
            1,
            "openai.responses.request",
            self.shared_openai_request(
                peter_message, "Accepted request", 1, model="recorded"
            ),
            participant_id=self.helios_id,
            config_id=config_id,
            related_message_id=peter_message,
        )
        self.add_event(
            turn_id,
            2,
            "openai.responses.error",
            {
                "reason": "provider_incomplete",
                "response": {
                    "id": "resp_bad",
                    "status": "incomplete",
                    "model": "resolved",
                    "usage": {"input_tokens": 2, "reasoning_tokens": 1},
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "headers_dump": "unsafe unfamiliar field",
                    "output": [{"private": "object"}],
                },
            },
            participant_id=self.helios_id,
            config_id=config_id,
            related_message_id=peter_message,
        )
        event = load_trace(self.database_path, turn_id)["api_events"][1]
        self.assertEqual(event["payload"]["response"]["id"], "resp_bad")
        self.assertNotIn("headers_dump", event["payload"]["response"])
        self.assertNotIn("output", event["payload"]["response"])

    def test_secret_normalization_and_rfc6901_pointers(self) -> None:
        projected, omissions = project_trace_json(
            {
                "Proxy-Authorization": "x",
                "Set-Cookie": "x",
                "X-API-Key": "x",
                "OPENAI_API_KEY": "x",
                "clientSecret": "x",
                "password": "x",
                "secret": "x",
                "token": "x",
                "nested/a~b": [{"refresh_token": "x", "input_tokens": 5}],
                "output_tokens": 3,
                "reasoning_tokens": 2,
                "total_tokens": 10,
            }
        )
        self.assertEqual(projected["nested/a~b"], [{"input_tokens": 5}])
        self.assertEqual(projected["output_tokens"], 3)
        self.assertEqual(projected["reasoning_tokens"], 2)
        self.assertEqual(projected["total_tokens"], 10)
        self.assertIn("/nested~1a~0b/0/refresh_token", omissions)
        self.assertNotIn("Proxy-Authorization", projected)

    def test_malformed_recognized_payloads_and_cardinality_are_invalid(self) -> None:
        malformed = self.add_turn()
        self.add_event(malformed, 1, "openai.responses.request", {"request": {}})
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, malformed)
        self.assertEqual(raised.exception.code, "trace_data_invalid")

        duplicate = self.add_turn(status="failed")
        self.add_event(duplicate, 1, "openai.responses.response", {"response": {}})
        self.add_event(duplicate, 2, "openai.responses.error", {"error": {}})
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, duplicate)
        self.assertEqual(raised.exception.code, "trace_data_invalid")

        bad_error = self.add_turn(status="failed")
        self.add_event(bad_error, 1, "openai.responses.error", {"unexpected": {}})
        with self.assertRaises(TraceServiceError):
            load_trace(self.database_path, bad_error)

    def test_invalid_json_and_broken_participant_provenance_are_invalid(self) -> None:
        turn_id = self.add_turn()
        event_id = self.add_event(turn_id, 1, "future.event", {})
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE api_events SET payload_json = 'not-json' WHERE id = ?",
                (event_id,),
            )
            connection.commit()
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, turn_id)
        self.assertEqual(raised.exception.code, "trace_data_invalid")

        broken_turn = self.add_turn()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO api_events (
                    turn_id, room_id, participant_id, sequence_no,
                    event_type, payload_json
                ) VALUES (?, ?, 999999, 1, 'future.event', '{}')
                """,
                (broken_turn, self.room_id),
            )
            connection.commit()
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, broken_turn)
        self.assertEqual(raised.exception.code, "trace_data_invalid")


class TraceRouteAndReadOnlyTests(TraceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.original_database_path = main.app.state.database_path
        main.app.state.database_path = self.database_path
        self.addCleanup(setattr, main.app.state, "database_path", self.original_database_path)

    def test_real_router_latest_specific_not_found_and_id_validation(self) -> None:
        status, headers, payload = asyncio.run(self.asgi_get("/api/trace/latest"))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "trace_not_found")
        self.assertEqual(headers["cache-control"], "no-store")

        first = self.add_turn()
        latest = self.add_turn()
        for path, expected_status, expected_code in (
            ("/api/trace/latest", 200, None),
            (f"/api/trace/{first}", 200, None),
            ("/api/trace/999999", 404, "trace_not_found"),
            ("/api/trace/0", 400, "invalid_trace_turn_id"),
            ("/api/trace/-1", 400, "invalid_trace_turn_id"),
            ("/api/trace/+1", 400, "invalid_trace_turn_id"),
            ("/api/trace/01", 400, "invalid_trace_turn_id"),
            ("/api/trace/ 1", 400, "invalid_trace_turn_id"),
            ("/api/trace/9223372036854775808", 400, "invalid_trace_turn_id"),
            ("/api/trace/not-decimal", 400, "invalid_trace_turn_id"),
        ):
            with self.subTest(path=path):
                status, headers, payload = asyncio.run(self.asgi_get(path))
                self.assertEqual(status, expected_status)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertTrue(headers["content-type"].startswith("application/json"))
                if expected_code:
                    self.assertEqual(payload["error"], expected_code)
        status, _, payload = asyncio.run(self.asgi_get("/api/trace/latest"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["turn"]["id"], latest)
        self.assertEqual(payload["turn"]["status"], "open")

        with closing(connect_database(self.database_path)) as connection:
            other_room = connection.execute(
                "INSERT INTO rooms (room_key, name) VALUES ('other', 'Other')"
            ).lastrowid
            connection.execute(
                """INSERT INTO room_history_visibility_events
                   (room_id,policy_version,effective_from_room_sequence_no)
                   VALUES (?,'room_shared_v1',1)""",
                (other_room,),
            )
            other_turn = connection.execute(
                "INSERT INTO turns (room_id, status) VALUES (?, 'open')",
                (other_room,),
            ).lastrowid
            connection.commit()
        status, _, payload = asyncio.run(self.asgi_get(f"/api/trace/{other_turn}"))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "trace_not_found")

    def test_invalid_recorded_data_is_a_stable_500_route_error(self) -> None:
        turn_id = self.add_turn()
        self.add_event(turn_id, 1, "openai.responses.request", {"request": {}})
        status, headers, payload = asyncio.run(self.asgi_get(f"/api/trace/{turn_id}"))
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "trace_data_invalid")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_missing_database_and_bad_schema_marker_are_503_without_creation(self) -> None:
        missing = Path(self.temporary_directory.name) / "absent" / "trace.db"
        main.app.state.database_path = missing
        status, headers, payload = asyncio.run(self.asgi_get("/api/trace/latest"))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "trace_database_unavailable")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

        bad = Path(self.temporary_directory.name) / "bad.db"
        with closing(sqlite3.connect(bad)) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations (migration_no INTEGER, schema_label TEXT)"
            )
            connection.execute("INSERT INTO schema_migrations VALUES (2, '2.0')")
            connection.commit()
        main.app.state.database_path = bad
        status, _, payload = asyncio.run(self.asgi_get("/api/trace/latest"))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "trace_database_unavailable")

    def test_reader_is_query_only_and_does_not_use_general_connector_or_commit(self) -> None:
        turn_id = self.add_turn()
        with patch(
            "app.database.connect_database",
            side_effect=AssertionError("trace must not use connect_database"),
        ):
            trace = load_trace(self.database_path, turn_id)
        self.assertEqual(trace["turn"]["id"], turn_id)

        real_connect = sqlite3.connect
        trackers: list[TrackingConnection] = []

        def tracked_connect(*args, **kwargs):
            tracker = TrackingConnection(real_connect(*args, **kwargs))
            tracker.connect_uri = args[0]
            trackers.append(tracker)
            return tracker

        with patch("app.read_snapshot.sqlite3.connect", side_effect=tracked_connect):
            load_trace(self.database_path, turn_id)
        self.assertEqual(trackers[0].commit_calls, 0)
        self.assertEqual(trackers[0].rollback_calls, 1)
        self.assertIn("mode=ro&immutable=1", trackers[0].connect_uri)
        self.assertIn("pragma query_only = on", "\n".join(trackers[0].sql).casefold())

    def test_wal_writer_cannot_mix_snapshot_state(self) -> None:
        turn_id = self.add_turn()
        self.add_message(turn_id, "Snapshot anchor")
        import app.trace_service as trace_service

        original = trace_service._load_messages

        def load_then_write(connection, selected_turn_id, room_id):
            result = original(connection, selected_turn_id, room_id)
            self.add_event(selected_turn_id, 1, "future.after-snapshot", {"new": True})
            return result

        with patch.object(trace_service, "_load_messages", side_effect=load_then_write):
            with self.assertRaises(TraceServiceError) as raised:
                load_trace(self.database_path, turn_id)
        self.assertEqual(raised.exception.code, "trace_database_unavailable")
        self.assertEqual(load_trace(self.database_path, turn_id)["api_events"][0]["event_type"], "future.after-snapshot")

    def test_route_reader_runs_across_thread_boundary_and_never_loads_environment(self) -> None:
        caller_thread = threading.get_ident()
        worker_threads: list[int] = []
        turn_id = self.add_turn()
        real_load = main.load_trace

        def observed_load(*args):
            worker_threads.append(threading.get_ident())
            return real_load(*args)

        with patch.object(main, "load_trace", side_effect=observed_load), patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "must-not-be-read"},
            clear=True,
        ), patch(
            "app.room_service.load_openai_environment",
            side_effect=AssertionError("trace must not load provider environment"),
        ):
            status, _, payload = asyncio.run(self.asgi_get(f"/api/trace/{turn_id}"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["turn"]["id"], turn_id)
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], caller_thread)

    def test_serve_missing_database_fails_preflight_without_creating_it(self) -> None:
        selected = Path(self.temporary_directory.name) / "not-created" / "chosen.db"
        argv = ["helios-room", "serve", "--database", str(selected)]
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            main.uvicorn, "run"
        ) as run_server, patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(selected.exists())
        self.assertFalse(selected.parent.exists())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": "database_not_initialized",
                "message": "The Helios Room database is not initialized.",
            },
        )
        run_server.assert_not_called()

    def test_trace_queries_no_memory_tables(self) -> None:
        turn_id = self.add_turn()
        real_connect = sqlite3.connect
        trackers: list[TrackingConnection] = []

        def tracked_connect(*args, **kwargs):
            tracker = TrackingConnection(real_connect(*args, **kwargs))
            tracker.connect_uri = args[0]
            trackers.append(tracker)
            return tracker

        with patch("app.read_snapshot.sqlite3.connect", side_effect=tracked_connect):
            load_trace(self.database_path, turn_id)
        sql = "\n".join(trackers[0].sql).casefold()
        self.assertNotIn("seed_memor", sql)
        self.assertNotIn("room_memor", sql)


class TrackingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "commit_calls", 0)
        object.__setattr__(self, "rollback_calls", 0)
        object.__setattr__(self, "sql", [])
        object.__setattr__(self, "connect_uri", "")

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __setattr__(self, name, value):
        if name in {"connection", "commit_calls", "rollback_calls", "sql", "connect_uri"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.connection, name, value)

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def execute(self, sql, parameters=()):
        self.sql.append(sql)
        return self.connection.execute(sql, parameters)

    def commit(self):
        self.commit_calls += 1
        raise AssertionError("trace reader must never commit")

    def rollback(self):
        self.rollback_calls += 1
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


if __name__ == "__main__":
    unittest.main()
