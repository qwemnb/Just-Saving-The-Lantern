from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.database import (
    MessageVisibilityGuardError,
    connect_database,
    create_room,
    create_turn,
    initialize_database,
    store_message,
)
from app.gemini_client import (
    GEMINI_SYSTEM_INSTRUCTIONS,
    recorded_request_config,
    validate_recorded_google_shared_request_payload,
)
from app.maintenance_lock import MaintenanceLockError, acquire_database_lease
from app.provider_history import load_provider_history
from app.request_validation import (
    HISTORY_VISIBILITY,
    OPENAI_SYSTEM_INSTRUCTIONS_V1,
    validate_recorded_openai_shared_request_payload,
)
from app.room_service import RESPONSE_SETTINGS, RESPONSE_TOOLS
from app.schema_validation import SchemaValidationError, validate_v14_foundation
from app.seed_memory import build_fts_query, tokenize_memory_query


class RoomSharedFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "helios.db"
        initialize_database(self.path)

    def ids(self, connection: sqlite3.Connection) -> tuple[int, int]:
        return (
            connection.execute("SELECT id FROM rooms WHERE room_key='main'").fetchone()[0],
            connection.execute("SELECT id FROM participants WHERE participant_key='peter'").fetchone()[0],
        )

    def test_exact_policy_timeline_and_future_room_creation(self) -> None:
        with closing(connect_database(self.path)) as connection:
            self.assertEqual(
                [tuple(row) for row in connection.execute(
                    "SELECT policy_version,effective_from_room_sequence_no FROM room_history_visibility_events"
                )],
                [("room_shared_v1", 1)],
            )
            connection.execute("BEGIN IMMEDIATE")
            second = create_room(connection, "second", "Second")
            connection.commit()
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT policy_version,effective_from_room_sequence_no FROM room_history_visibility_events WHERE room_id=?",
                    (second,),
                ).fetchone()),
                ("room_shared_v1", 1),
            )
            validate_v14_foundation(connection)

    def test_policy_and_message_defense_triggers_are_immutable_and_closed(self) -> None:
        with closing(connect_database(self.path)) as connection:
            room, peter = self.ids(connection)
            for statement in (
                "UPDATE room_history_visibility_events SET policy_version='room_shared_v1'",
                "DELETE FROM room_history_visibility_events",
                "INSERT INTO room_history_visibility_events(room_id,policy_version,effective_from_room_sequence_no) VALUES (%d,'room_shared_v1',1)" % room,
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
                connection.rollback()

            connection.execute("BEGIN IMMEDIATE")
            ungoverned = connection.execute(
                "INSERT INTO rooms(room_key,name) VALUES ('unguarded','Unguarded')"
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError) as raised:
                connection.execute(
                    """INSERT INTO messages
                       (room_id,room_sequence_no,participant_id,message_type,message_text)
                       VALUES (?,1,?,'chat','blocked')""", (ungoverned, peter)
                )
            self.assertEqual(str(raised.exception), "message_visibility_guard")
            connection.rollback()

    def test_application_guard_is_exact_and_precedes_insert(self) -> None:
        with closing(connect_database(self.path)) as connection:
            _room, peter = self.ids(connection)
            connection.execute("BEGIN IMMEDIATE")
            ungoverned = connection.execute(
                "INSERT INTO rooms(room_key,name) VALUES ('missing-policy','Missing')"
            ).lastrowid
            with self.assertRaises(MessageVisibilityGuardError) as raised:
                store_message(connection, ungoverned, peter, "blocked")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.as_payload(), {
                "error": "message_visibility_policy_unavailable",
                "message": "The room history visibility policy does not permit this message to be stored.",
            })
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE room_id=?", (ungoverned,)
            ).fetchone()[0], 0)
            connection.rollback()

    def test_shared_projection_includes_other_destination_as_exact_external_envelope(self) -> None:
        with closing(connect_database(self.path)) as connection:
            room, peter = self.ids(connection)
            gemini = connection.execute(
                "SELECT id FROM participants WHERE participant_key='gemini'"
            ).fetchone()[0]
            peter_alias = connection.execute(
                "SELECT alias_id FROM participant_primary_aliases WHERE participant_id=?", (peter,)
            ).fetchone()[0]
            gemini_alias = connection.execute(
                "SELECT alias_id FROM participant_primary_aliases WHERE participant_id=?", (gemini,)
            ).fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            turn_id = create_turn(connection, room, peter)
            message_id = store_message(
                connection, room, peter, "Gemini-directed Unicode ☀",
                turn_id=turn_id,
                sender_alias_id=peter_alias, destination_kind="participant",
                destination_alias_id=gemini_alias, recipient_participant_id=gemini,
            )
            connection.commit()
            helios = load_provider_history(
                connection, room_id=room, boundary=1, provider_participant_key="helios"
            )
        self.assertEqual(len(helios), 1)
        self.assertEqual(helios[0]["role"], "user")
        prefix, raw = helios[0]["content"].split("\n", 1)
        self.assertEqual(prefix, "ROOM_PARTICIPANT_MESSAGE")
        self.assertEqual(json.loads(raw), {
            "destination": {"display_name": "Gemini", "kind": "participant", "participant_key": "gemini"},
            "kind": "room_participant_message", "message_id": message_id,
            "message_text": "Gemini-directed Unicode ☀",
            "sender": {"display_name": "Peter", "participant_key": "peter"},
        })
        self.assertIn("☀", raw)

    def test_shared_request_validators_require_exact_visibility(self) -> None:
        terms = tokenize_memory_query("hello")
        memory = {
            "retriever_version": "seed-fts-topic-v1", "owner_participant_id": 2,
            "query_source_message_id": 1, "query_terms": terms,
            "fts_query": build_fts_query(terms), "result_limit": 5,
            "text_budget_chars": 8000, "omitted_for_budget": 0, "selected": [],
        }
        payload = {
            "request": {
                "model": "model", "instructions": OPENAI_SYSTEM_INSTRUCTIONS_V1,
                "input": [{"role": "user", "content": "hello"}],
                "store": False, "reasoning": dict(RESPONSE_SETTINGS["reasoning"]),
                "max_output_tokens": 2048, "tools": list(RESPONSE_TOOLS),
            },
            "local_context": {
                "provider": "openai", "operation": "responses.create",
                "trigger_message_id": 1, "room_sequence_boundary": 1,
                "timeout_seconds": 120, "max_retries": 0,
                "memory_retrieval": memory, "history_visibility": dict(HISTORY_VISIBILITY),
            },
        }
        self.assertIs(validate_recorded_openai_shared_request_payload(payload), payload)
        for mutation in (
            lambda value: value["local_context"].pop("history_visibility"),
            lambda value: value["local_context"]["history_visibility"].update(extra=True),
            lambda value: value["local_context"]["history_visibility"].update(effective_from_room_sequence_no=2),
            lambda value: value["local_context"]["history_visibility"].update(effective_from_room_sequence_no=True),
        ):
            altered = json.loads(json.dumps(payload))
            mutation(altered)
            with self.assertRaises(ValueError):
                validate_recorded_openai_shared_request_payload(altered)

        google_payload = {
            "local_context": {
                "api_version": "v1beta", "memory_retrieval": memory,
                "operation": "models.generate_content", "provider": "google",
                "room_sequence_boundary": 1, "safety_settings": "provider_default",
                "sdk_policy": {"automatic_function_calling": {"disable": True}},
                "timeout_seconds": 120, "total_attempts": 1,
                "trigger_message_id": 1, "history_visibility": dict(HISTORY_VISIBILITY),
            },
            "request": {
                "model": "gemini-test", "config": recorded_request_config(),
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            },
        }
        self.assertEqual(
            google_payload["request"]["config"]["system_instruction"],
            GEMINI_SYSTEM_INSTRUCTIONS,
        )
        self.assertIs(
            validate_recorded_google_shared_request_payload(google_payload),
            google_payload,
        )
        for invalid_memory in ({}, {"selected": [{"client_secret": "never expose"}]}):
            altered = json.loads(json.dumps(google_payload))
            altered["local_context"]["memory_retrieval"] = invalid_memory
            with self.assertRaises(ValueError):
                validate_recorded_google_shared_request_payload(altered)

    def test_exclusive_maintenance_lease_blocks_every_normal_connection(self) -> None:
        lease = acquire_database_lease(self.path, shared=False)
        try:
            with self.assertRaises(MaintenanceLockError):
                connect_database(self.path)
        finally:
            lease.close()
        with closing(connect_database(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_mature_validator_rejects_room_without_policy(self) -> None:
        with closing(connect_database(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO rooms(room_key,name) VALUES ('bad','Bad')")
            with self.assertRaises(SchemaValidationError):
                validate_v14_foundation(connection)
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
