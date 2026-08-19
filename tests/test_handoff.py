from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.database import (
    _ensure_identity_bootstrap,
    _ensure_participant,
    connect_database,
    create_turn,
    initialize_database,
    store_message,
)
from app.gemini_service import run_gemini_turn
from app.handoff_contract import HANDOFF_PROTOCOL_VERSION, ROOM_HANDOFF_AUTHORIZATION
from app.handoff_service import run_manual_handoff
from app.models import HandoffRequest
from app.room_service import TurnServiceError, run_helios_turn
from app.trace_service import TraceServiceError, load_trace
from tests import test_direct_addressing as direct_fixtures
from tests.test_gemini import FakeClient as FakeGeminiClient
from tests.test_gemini import SYNTHETIC_KEY, success_response
from tests.test_room_service import FakeClient, FakeResponses


class ManualParticipantHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "room.db"
        self.dotenv = Path(self.temp.name) / "missing.env"
        initialize_database(self.database)

    def seed_helios_to_gemini(self, text: str = "Hello Gemini") -> int:
        response = direct_fixtures.DirectParticipantAddressingTests.openai_response("From Helios")
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            result = asyncio.run(run_helios_turn(
                text,
                response_destination={"kind": "participant", "participant_key": "gemini"},
                database_path=self.database,
                client_factory=lambda _key: FakeClient(FakeResponses(response)),
                dotenv_path=self.dotenv,
            ))
        return result["helios_message_id"]

    def seed_gemini_to_helios(self, text: str = "Hello Helios") -> int:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result = asyncio.run(run_gemini_turn(
                text,
                response_destination={"kind": "participant", "participant_key": "helios"},
                database_path=self.database,
                client_factory=lambda _key: FakeGeminiClient(success_response("From Gemini")),
                dotenv_path=self.dotenv,
            ))
        return result["gemini_message_id"]

    def test_request_schema_contains_only_positive_strict_source_id(self) -> None:
        self.assertEqual(HandoffRequest.model_validate({"source_message_id": 1}).source_message_id, 1)
        for value in (0, -1, 1.0, "1", True, 9_223_372_036_854_775_808):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                HandoffRequest.model_validate({"source_message_id": value})
        for extra in ("message_text", "destination", "responder", "participant_key", "model"):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                HandoffRequest.model_validate({"source_message_id": 1, extra: "attacker"})

    def test_helios_source_invokes_gemini_once_and_adds_one_message(self) -> None:
        source_id = self.seed_helios_to_gemini()
        gemini_client = FakeGeminiClient(success_response("Gemini answers Helios"))
        openai_factory = Mock(side_effect=AssertionError("OpenAI must remain inert"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                openai_client_factory=openai_factory,
                gemini_client_factory=lambda key: gemini_client if key == SYNTHETIC_KEY else None,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(openai_factory.call_count, 0)
        self.assertEqual(len(gemini_client.aio.models.calls), 1)
        call_contents = gemini_client.aio.models.calls[0]["contents"]
        self.assertTrue(call_contents[-1].parts[0].text.startswith(ROOM_HANDOFF_AUTHORIZATION + "\n"))
        self.assertFalse(any(item.parts[0].text == "Peter authorizes this response." for item in call_contents))
        with closing(connect_database(self.database)) as connection:
            rows = connection.execute(
                """SELECT m.id,m.turn_sequence_no,m.reply_to_id,p.participant_key,
                          recipient.participant_key AS recipient_key
                   FROM messages AS m JOIN participants AS p ON p.id=m.participant_id
                   JOIN message_routes AS mr ON mr.message_id=m.id
                   LEFT JOIN participants AS recipient ON recipient.id=mr.recipient_participant_id
                   WHERE m.turn_id=?""",
                (result["turn_id"],),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(tuple(rows[0])[1:], (1, source_id, "gemini", "helios"))
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE id=?", (source_id,)
            ).fetchone()[0], 1)
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                (result["turn_id"],),
            ).fetchone()[0])
            self.assertEqual(payload["local_context"]["handoff_version"], HANDOFF_PROTOCOL_VERSION)
            self.assertEqual(payload["local_context"]["history_visibility"]["projection_version"], "provider_history_v4")
            self.assertEqual(payload["local_context"]["handoff_authorization"]["source_message_id"], source_id)
        trace = load_trace(self.database, result["turn_id"])
        self.assertEqual(trace["trace_version"], 3)
        self.assertEqual(trace["turn"]["initiated_by"]["participant_key"], "peter")
        self.assertTrue(trace["messages"][0]["reply_to_outside_selected_turn"])

    def test_gemini_source_invokes_helios_once_and_adds_one_message(self) -> None:
        source_id = self.seed_gemini_to_helios()
        openai_response = direct_fixtures.DirectParticipantAddressingTests.openai_response("Helios answers Gemini")
        openai_client = FakeClient(FakeResponses(openai_response))
        gemini_factory = Mock(side_effect=AssertionError("Gemini must remain inert"))
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                openai_client_factory=lambda key: openai_client if key == "synthetic-openai" else None,
                gemini_client_factory=gemini_factory,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(gemini_factory.call_count, 0)
        self.assertEqual(len(openai_client.responses.calls), 1)
        provider_input = openai_client.responses.calls[0]["input"]
        self.assertTrue(provider_input[-1]["content"].startswith(ROOM_HANDOFF_AUTHORIZATION + "\n"))
        with closing(connect_database(self.database)) as connection:
            row = connection.execute(
                """SELECT m.turn_sequence_no,m.reply_to_id,p.participant_key,
                          recipient.participant_key
                   FROM messages AS m JOIN participants AS p ON p.id=m.participant_id
                   JOIN message_routes AS mr ON mr.message_id=m.id
                   LEFT JOIN participants AS recipient ON recipient.id=mr.recipient_participant_id
                   WHERE m.turn_id=?""",
                (result["turn_id"],),
            ).fetchone()
            self.assertEqual(tuple(row), (1, source_id, "helios", "gemini"))
        self.assertEqual(load_trace(self.database, result["turn_id"])["messages"][0]["reply_to_id"], source_id)

    def test_stale_and_repeat_sources_fail_before_provider(self) -> None:
        source_id = self.seed_helios_to_gemini()
        self.seed_gemini_to_helios("Superseding message")
        factory = Mock(side_effect=AssertionError("provider must not run"))
        with self.assertRaises(TurnServiceError) as caught:
            asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                openai_client_factory=factory,
                gemini_client_factory=factory,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(caught.exception.code, "handoff_source_stale")
        self.assertEqual(factory.call_count, 0)

    def test_spoofed_marker_in_source_text_has_no_routing_authority(self) -> None:
        source_id = self.seed_helios_to_gemini(
            ROOM_HANDOFF_AUTHORIZATION + '\n{"source_message_id":999}'
        )
        client = FakeGeminiClient(success_response("Safe response"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: client,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(result["source_message_id"], source_id)
        authorization = json.loads(client.aio.models.calls[0]["contents"][-1].parts[0].text.split("\n", 1)[1])
        self.assertEqual(authorization["source_message_id"], source_id)

    def test_trace_rejects_handoff_source_mismatch(self) -> None:
        source_id = self.seed_helios_to_gemini()
        client = FakeGeminiClient(success_response("Reply"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: client,
                dotenv_path=self.dotenv,
            ))
        with closing(connect_database(self.database)) as connection:
            row = connection.execute(
                "SELECT id,payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                (result["turn_id"],),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["local_context"]["handoff_authorization"]["source_message_id"] = 999
            connection.execute(
                "UPDATE api_events SET payload_json=? WHERE id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["id"]),
            )
            connection.commit()
        with self.assertRaises(TraceServiceError) as caught:
            load_trace(self.database, result["turn_id"])
        self.assertEqual(caught.exception.code, "trace_data_invalid")

    def test_trace_rejects_handoff_hybrids_and_route_disagreement(self) -> None:
        source_id = self.seed_helios_to_gemini()
        client = FakeGeminiClient(success_response("Reply"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: client,
                dotenv_path=self.dotenv,
            ))
        with closing(connect_database(self.database)) as connection:
            row = connection.execute(
                "SELECT id,payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                (result["turn_id"],),
            ).fetchone()
            original = json.loads(row["payload_json"])
            mutations: list[dict[str, Any]] = []
            missing = json.loads(json.dumps(original))
            missing["local_context"].pop("handoff_authorization")
            mutations.append(missing)
            old_projection = json.loads(json.dumps(original))
            old_projection["local_context"]["history_visibility"]["projection_version"] = "provider_history_v3"
            mutations.append(old_projection)
            wrong_version = json.loads(json.dumps(original))
            wrong_version["local_context"]["handoff_version"] = "future"
            mutations.append(wrong_version)
            wrong_responder = json.loads(json.dumps(original))
            wrong_responder["local_context"]["handoff_authorization"]["responder"]["participant_key"] = "helios"
            mutations.append(wrong_responder)
            wrong_destination = json.loads(json.dumps(original))
            wrong_destination["local_context"]["response_destination"] = {
                "display_name": "Peter", "kind": "participant", "participant_key": "peter"
            }
            mutations.append(wrong_destination)
            for payload in mutations:
                with self.subTest(fields=sorted(payload["local_context"])):
                    connection.execute(
                        "UPDATE api_events SET payload_json=? WHERE id=?",
                        (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["id"]),
                    )
                    connection.commit()
                    with self.assertRaises(TraceServiceError) as caught:
                        load_trace(self.database, result["turn_id"])
                    self.assertEqual(caught.exception.code, "trace_data_invalid")
                    connection.execute(
                        "UPDATE api_events SET payload_json=? WHERE id=?",
                        (json.dumps(original, sort_keys=True, separators=(",", ":")), row["id"]),
                    )
                    connection.commit()

    def test_provider_failure_adds_no_message_and_explicit_retry_succeeds(self) -> None:
        source_id = self.seed_helios_to_gemini()
        failed_client = FakeGeminiClient(RuntimeError("synthetic private failure"))
        environment = {
            "GEMINI_API_KEY": SYNTHETIC_KEY,
            "HELIOS_GEMINI_MODEL": "gemini-test",
        }
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(
            TurnServiceError
        ) as caught:
            asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: failed_client,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(caught.exception.code, "gemini_provider_failure")
        with closing(connect_database(self.database)) as connection:
            failed_turn = caught.exception.turn_id
            self.assertEqual(connection.execute(
                "SELECT status FROM turns WHERE id=?", (failed_turn,)
            ).fetchone()[0], "failed")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE turn_id=?", (failed_turn,)
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE id=?", (source_id,)
            ).fetchone()[0], 1)
        failed_trace = load_trace(self.database, failed_turn)
        self.assertEqual(failed_trace["turn"]["status"], "failed")
        self.assertEqual(failed_trace["messages"], [])
        self.assertEqual(
            failed_trace["recorded_request"]["local_context"]
            ["handoff_authorization"]["source_message_id"],
            source_id,
        )
        retry_client = FakeGeminiClient(success_response("Explicit retry"))
        with patch.dict(os.environ, environment, clear=True):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: retry_client,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(failed_client.aio.models.calls), 1)
        self.assertEqual(len(retry_client.aio.models.calls), 1)

    def test_concurrent_duplicate_acceptance_invokes_exactly_one_provider(self) -> None:
        source_id = self.seed_helios_to_gemini()

        class BlockingModels:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def generate_content(self, **request: Any) -> Any:
                self.calls.append(request)
                self.started.set()
                await self.release.wait()
                return success_response("One response")

        models = BlockingModels()
        client = FakeGeminiClient(success_response("unused"))
        client.aio.models = models

        async def exercise() -> tuple[dict[str, Any], TurnServiceError]:
            first = asyncio.create_task(run_manual_handoff(
                source_id,
                database_path=self.database,
                gemini_client_factory=lambda _key: client,
                dotenv_path=self.dotenv,
            ))
            await models.started.wait()
            try:
                await run_manual_handoff(
                    source_id,
                    database_path=self.database,
                    gemini_client_factory=lambda _key: client,
                    dotenv_path=self.dotenv,
                )
            except TurnServiceError as error:
                duplicate = error
            else:
                raise AssertionError("duplicate handoff was accepted")
            models.release.set()
            return await first, duplicate

        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            result, duplicate = asyncio.run(exercise())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(duplicate.code, "handoff_in_progress")
        self.assertEqual(len(models.calls), 1)

    def test_multiple_alternating_handoffs_reconstruct_provider_history_v4(self) -> None:
        first = self.seed_helios_to_gemini("Start exchange")
        gemini_client = FakeGeminiClient(success_response("Gemini handoff one"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            gemini_result = asyncio.run(run_manual_handoff(
                first,
                database_path=self.database,
                gemini_client_factory=lambda _key: gemini_client,
                dotenv_path=self.dotenv,
            ))
        openai_response = direct_fixtures.DirectParticipantAddressingTests.openai_response(
            "Helios handoff two"
        )
        helios_client = FakeClient(FakeResponses(openai_response))
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            helios_result = asyncio.run(run_manual_handoff(
                gemini_result["handoff_message_id"],
                database_path=self.database,
                openai_client_factory=lambda _key: helios_client,
                dotenv_path=self.dotenv,
            ))
        history = helios_client.responses.calls[0]["input"]
        self.assertIn({"role": "assistant", "content": "From Helios"}, history)
        self.assertTrue(any(
            item["role"] == "user"
            and item["content"].startswith("ROOM_PARTICIPANT_MESSAGE\n")
            and "Gemini handoff one" in item["content"]
            for item in history
        ))
        self.assertFalse(any(
            item.get("content", "").startswith(ROOM_HANDOFF_AUTHORIZATION)
            for item in history[:-1]
        ))
        self.assertEqual(load_trace(self.database, helios_result["turn_id"])["messages"][0]["message_text"], "Helios handoff two")

        later_client = FakeGeminiClient(success_response("Later ordinary Gemini turn"))
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            asyncio.run(run_gemini_turn(
                "Peter resumes",
                database_path=self.database,
                client_factory=lambda _key: later_client,
                dotenv_path=self.dotenv,
            ))
        later_contents = later_client.aio.models.calls[0]["contents"]
        self.assertTrue(any(
            item.role == "model"
            and any(part.text == "Gemini handoff one" for part in item.parts)
            for item in later_contents
        ))
        self.assertFalse(any(
            item.parts[0].text.startswith(ROOM_HANDOFF_AUTHORIZATION)
            for item in later_contents[:-2]
        ))

    def test_ai_to_peter_and_room_are_natural_stopping_conditions(self) -> None:
        factory = Mock(side_effect=AssertionError("handoff provider must not run"))
        response = direct_fixtures.DirectParticipantAddressingTests.openai_response(
            "Back to Peter"
        )
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            first = asyncio.run(run_helios_turn(
                "Return",
                database_path=self.database,
                client_factory=lambda _key: FakeClient(FakeResponses(response)),
                dotenv_path=self.dotenv,
            ))
        for source_id in (first["helios_message_id"],):
            with self.assertRaises(TurnServiceError) as caught:
                asyncio.run(run_manual_handoff(
                    source_id,
                    database_path=self.database,
                    openai_client_factory=factory,
                    gemini_client_factory=factory,
                    dotenv_path=self.dotenv,
                ))
            self.assertEqual(caught.exception.code, "handoff_not_ai_to_ai")

        room_response = direct_fixtures.DirectParticipantAddressingTests.openai_response(
            "To the Room"
        )
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            second = asyncio.run(run_helios_turn(
                "Broadcast",
                response_destination={"kind": "room"},
                database_path=self.database,
                client_factory=lambda _key: FakeClient(FakeResponses(room_response)),
                dotenv_path=self.dotenv,
            ))
        with self.assertRaises(TurnServiceError) as caught:
            asyncio.run(run_manual_handoff(
                second["helios_message_id"],
                database_path=self.database,
                openai_client_factory=factory,
                gemini_client_factory=factory,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(caught.exception.code, "handoff_not_ai_to_ai")
        self.assertEqual(factory.call_count, 0)

    def test_peter_authored_missing_and_inactive_sources_fail_before_provider(self) -> None:
        failing = FakeClient(FakeResponses(RuntimeError("synthetic failure")))
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ), self.assertRaises(TurnServiceError) as failed:
            asyncio.run(run_helios_turn(
                "Peter remains terminal",
                database_path=self.database,
                client_factory=lambda _key: failing,
                dotenv_path=self.dotenv,
            ))
        peter_message_id = failed.exception.peter_message_id
        guard = Mock(side_effect=AssertionError("provider must not run"))
        for source_id, expected in ((peter_message_id, "handoff_recipient_unavailable"), (99999, "handoff_source_not_found")):
            with self.subTest(source_id=source_id), self.assertRaises(TurnServiceError) as caught:
                asyncio.run(run_manual_handoff(
                    source_id,
                    database_path=self.database,
                    openai_client_factory=guard,
                    gemini_client_factory=guard,
                    dotenv_path=self.dotenv,
                ))
            self.assertEqual(caught.exception.code, expected)

        self.seed_helios_to_gemini("Establish Helios configuration")
        with closing(connect_database(self.database)) as connection:
            inactive_id = _ensure_participant(
                connection, "inactive-ai", "Inactive AI", "ai"
            )
            _ensure_identity_bootstrap(connection, inactive_id, "Inactive AI")
            alias_id = connection.execute(
                "SELECT alias_id FROM participant_primary_aliases WHERE participant_id=?",
                (inactive_id,),
            ).fetchone()[0]
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]
            peter_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='peter'"
            ).fetchone()[0]
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='helios'"
            ).fetchone()[0]
            helios_alias_id = connection.execute(
                "SELECT alias_id FROM participant_primary_aliases WHERE participant_id=?",
                (helios_id,),
            ).fetchone()[0]
            config_id = connection.execute(
                """SELECT id FROM participant_configs
                   WHERE participant_id=? ORDER BY id DESC LIMIT 1""",
                (helios_id,),
            ).fetchone()[0]
            turn_id = create_turn(connection, room_id, peter_id)
            source_id = store_message(
                connection,
                room_id=room_id,
                participant_id=helios_id,
                participant_config_id=config_id,
                message_text="Inactive recipient",
                turn_id=turn_id,
                message_type="chat",
                sender_alias_id=helios_alias_id,
                destination_kind="participant",
                destination_alias_id=alias_id,
                recipient_participant_id=inactive_id,
            )
            connection.commit()
        from app.participant_registry import ParticipantRegistration, registration_for

        def registration(key: str):
            if key == "inactive-ai":
                return ParticipantRegistration("inactive-ai", "google", "synthetic")
            return registration_for(key)

        with patch("app.handoff_service.registration_for", side_effect=registration), self.assertRaises(
            TurnServiceError
        ) as caught:
            asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                openai_client_factory=guard,
                gemini_client_factory=guard,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(caught.exception.code, "handoff_recipient_unavailable")
        self.assertEqual(guard.call_count, 0)

    def test_phase_a_return_alias_survives_later_primary_alias_change(self) -> None:
        source_id = self.seed_gemini_to_helios("Alias binding")

        def rename_gemini(_request: dict[str, Any]) -> None:
            from app.identity_service import adopt_participant_name

            with closing(connect_database(self.database)) as connection:
                room_id = connection.execute(
                    "SELECT id FROM rooms WHERE room_key='main'"
                ).fetchone()[0]
                gemini_id = connection.execute(
                    "SELECT id FROM participants WHERE participant_key='gemini'"
                ).fetchone()[0]
                connection.execute("BEGIN IMMEDIATE")
                adopt_participant_name(connection, room_id, gemini_id, gemini_id, "Nova")
                connection.commit()

        response = direct_fixtures.DirectParticipantAddressingTests.openai_response(
            "Bound alias"
        )
        client = FakeClient(FakeResponses(response, on_call=rename_gemini))
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            result = asyncio.run(run_manual_handoff(
                source_id,
                database_path=self.database,
                openai_client_factory=lambda _key: client,
                dotenv_path=self.dotenv,
            ))
        self.assertEqual(
            load_trace(self.database, result["turn_id"])["messages"][0]["routing"]["destination"],
            {"kind": "participant", "participant_key": "gemini", "display_name": "Gemini"},
        )


if __name__ == "__main__":
    unittest.main()
