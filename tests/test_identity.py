from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main
from app.database import (
    DatabaseInitializationError,
    connect_database,
    initialize_database,
)
from app.identity_service import (
    ParticipantNameError,
    adopt_participant_name,
    load_message_history,
    load_participant_directory,
    validate_display_alias,
)
from app.migration import DatabaseMigrationError, migrate_database
from app.trace_service import load_trace


class IdentityFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_path = Path(self.temp.name) / "identity.db"
        initialize_database(self.database_path)
        self.original_database_path = main.app.state.database_path
        self.original_factory = main.app.state.openai_client_factory
        self.original_dotenv = main.app.state.dotenv_path
        main.app.state.database_path = self.database_path
        main.app.state.openai_client_factory = self.provider_must_not_run
        main.app.state.dotenv_path = Path(self.temp.name) / "missing.env"
        self.addCleanup(self.restore_app)

    def restore_app(self) -> None:
        main.app.state.database_path = self.original_database_path
        main.app.state.openai_client_factory = self.original_factory
        main.app.state.dotenv_path = self.original_dotenv

    @staticmethod
    def provider_must_not_run(_key: str):
        raise AssertionError("provider must not be constructed")

    async def request(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    def ids(self, connection: sqlite3.Connection) -> tuple[int, int, int, int]:
        room_id = connection.execute("SELECT id FROM rooms WHERE room_key='main'").fetchone()[0]
        participants = {
            row["participant_key"]: row["id"]
            for row in connection.execute("SELECT id,participant_key FROM participants")
        }
        return room_id, participants["peter"], participants["helios"], participants["room-system"]


class FreshSchemaAndAliasTests(IdentityFixture):
    def test_fresh_graph_is_exact_and_immutable(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(
                [tuple(row) for row in connection.execute(
                    "SELECT migration_no,schema_label FROM schema_migrations ORDER BY migration_no"
                )],
                [(1, "1.2"), (2, "1.3"), (3, "1.4")],
            )
            room_id, peter_id, helios_id, room_system_id = self.ids(connection)
            del room_id, peter_id, helios_id
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT participant_key,name,participant_type FROM participants WHERE id=?",
                    (room_system_id,),
                ).fetchone()),
                ("room-system", "Room", "system"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM room_participants WHERE participant_id=?",
                    (room_system_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM message_routes").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM participant_aliases").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM participant_primary_aliases").fetchone()[0], 4)
            events = connection.execute(
                """SELECT event_type,room_id,actor_participant_id,subject_participant_id,
                          previous_alias_id,new_alias_id,canonical_message_id
                   FROM participant_name_events ORDER BY id"""
            ).fetchall()
            self.assertEqual(len(events), 4)
            for event in events:
                self.assertEqual(event["event_type"], "bootstrap")
                self.assertIsNone(event["room_id"])
                self.assertEqual(event["actor_participant_id"], event["subject_participant_id"])
                self.assertIsNone(event["previous_alias_id"])
                self.assertIsNone(event["canonical_message_id"])

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE participants SET name='Changed' WHERE id=?", (room_system_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM participants WHERE id=?", (room_system_id,))
            alias_id = connection.execute(
                "SELECT id FROM participant_aliases WHERE participant_id=?", (room_system_id,)
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE participant_aliases SET display_alias='Other' WHERE id=?", (alias_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM participant_aliases WHERE id=?", (alias_id,))

    def test_shared_alias_validation_and_normalization(self) -> None:
        self.assertEqual(validate_display_alias("  Ａster  "), ("Ａster", "aster"))
        self.assertEqual(validate_display_alias("Straße"), ("Straße", "strasse"))
        for value in ("", "   ", "/Aster", "A[ster", "Aster]", "all", "EVERYONE", "Room", "a\u0000b", "x" * 65):
            with self.subTest(value=value), self.assertRaises(ParticipantNameError):
                validate_display_alias(value)

    def test_name_adoption_exact_event_history_and_historical_reuse(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            room_id, peter_id, _helios_id, room_system_id = self.ids(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = adopt_participant_name(connection, room_id, peter_id, peter_id, "  Aster  ")
            self.assertEqual(result["status"], "adopted")
            connection.commit()

            route = connection.execute(
                """SELECT m.message_text,m.participant_id,m.message_type,mr.destination_kind,
                          mr.recipient_participant_id,mr.routing_mode,sa.display_alias,da.display_alias
                   FROM messages AS m JOIN message_routes AS mr ON mr.message_id=m.id
                   JOIN participant_aliases AS sa ON sa.id=mr.sender_alias_id
                   JOIN participant_aliases AS da ON da.id=mr.destination_alias_id"""
            ).fetchone()
            self.assertEqual(route["message_text"], "Peter adopted the name Aster.")
            self.assertEqual(route["participant_id"], room_system_id)
            self.assertEqual(route["message_type"], "system")
            self.assertEqual(route["destination_kind"], "room")
            self.assertIsNone(route["recipient_participant_id"])
            self.assertEqual(route["routing_mode"], "explicit")
            self.assertEqual((route["display_alias"], route["display_alias"]), ("Room", "Room"))
            event = connection.execute(
                "SELECT * FROM participant_name_events WHERE event_type='adopted'"
            ).fetchone()
            self.assertEqual(event["actor_participant_id"], peter_id)
            self.assertEqual(event["subject_participant_id"], peter_id)
            self.assertEqual(event["canonical_message_id"], result["message_id"])

            before = connection.total_changes
            connection.execute("BEGIN IMMEDIATE")
            unchanged = adopt_participant_name(connection, room_id, peter_id, peter_id, "ASTER")
            self.assertEqual(unchanged["status"], "unchanged")
            self.assertEqual(connection.total_changes, before)
            connection.rollback()

            connection.execute("BEGIN IMMEDIATE")
            second = adopt_participant_name(connection, room_id, peter_id, peter_id, "Peter")
            connection.commit()
            self.assertEqual(second["status"], "adopted")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM participant_aliases WHERE participant_id=?", (peter_id,)
            ).fetchone()[0], 2)

    def test_conflict_is_stable_zero_write_and_caller_lock_is_immediate(self) -> None:
        first = connect_database(self.database_path)
        second = connect_database(self.database_path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        room_id, peter_id, _helios_id, _system_id = self.ids(first)
        before = first.total_changes
        first.execute("BEGIN IMMEDIATE")
        second.execute("PRAGMA busy_timeout=1")
        with self.assertRaises(sqlite3.OperationalError):
            second.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ParticipantNameError) as caught:
            adopt_participant_name(first, room_id, peter_id, peter_id, "Helios")
        self.assertEqual(caught.exception.code, "participant_alias_conflict")
        self.assertEqual(first.total_changes, before)
        first.rollback()


class DirectoryPostHistoryAndTraceTests(IdentityFixture):
    def test_directory_is_versioned_ordered_read_only_and_hides_system(self) -> None:
        before = self.database_path.read_bytes()
        directory = load_participant_directory(self.database_path)
        after = self.database_path.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(directory["directory_version"], 1)
        self.assertEqual(directory["destinations"][0], {"kind": "room", "label": "Room", "addressable": True})
        self.assertEqual([item.get("participant_key") for item in directory["destinations"][1:]], ["gemini", "helios", "peter"])
        self.assertNotIn("room-system", json.dumps(directory))
        self.assertTrue(directory["destinations"][1]["addressable"])
        self.assertTrue(directory["destinations"][2]["addressable"])
        self.assertFalse(directory["destinations"][3]["addressable"])

        response = asyncio.run(self.request("GET", "/api/participants"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_room_post_is_atomic_provider_free_and_trace_v3(self) -> None:
        sentinel = "  exact Room note  "
        with patch("app.room_service.load_openai_environment", side_effect=AssertionError("no env")):
            response = asyncio.run(self.request(
                "POST", "/api/messages",
                json={"message_text": sentinel, "destination": {"kind": "room"}},
            ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "completed")
            self.assertEqual(connection.execute("SELECT message_text FROM messages").fetchone()[0], sentinel)
            self.assertEqual(connection.execute("SELECT count(*) FROM message_routes").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM participant_configs").fetchone()[0], 1)
        history = load_message_history(self.database_path)
        self.assertEqual(history[0]["routing"]["sender"]["display_name"], "Peter")
        self.assertEqual(history[0]["routing"]["destination"], {"kind": "room", "display_name": "Room"})
        trace = load_trace(self.database_path, response.json()["turn_id"])
        self.assertEqual(trace["trace_version"], 3)
        self.assertIsNone(trace["recorded_request"])
        self.assertIsNone(trace["provider_outcome"])
        self.assertEqual(trace["api_events"], [])
        self.assertEqual(trace["messages"][0]["routing"]["destination"]["kind"], "room")

    def test_exact_helios_destination_preserves_text_and_routes_both_messages(self) -> None:
        class Response:
            status = "completed"
            output_text = "Exact Helios reply"

            def model_dump(self, mode="json"):
                del mode
                return {"status": self.status, "output_text": self.output_text}

        class Responses:
            def __init__(self):
                self.calls = []

            async def create(self, **request):
                self.calls.append(request)
                return Response()

        class Client:
            def __init__(self):
                self.responses = Responses()

            async def close(self):
                return None

        client = Client()
        main.app.state.openai_client_factory = lambda _key: client
        exact = "  exact Peter text  "
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "HELIOS_OPENAI_MODEL": "gpt-5.6-luna",
        }, clear=True):
            response = asyncio.run(self.request(
                "POST", "/api/messages",
                json={
                    "message_text": exact,
                    "destination": {"kind": "participant", "participant_key": "helios"},
                },
            ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(client.responses.calls[0]["input"][-1], {"role": "user", "content": exact})
        history = load_message_history(self.database_path)
        self.assertEqual([item["message_text"] for item in history], [exact, "Exact Helios reply"])
        self.assertEqual(history[0]["routing"]["destination"]["participant_key"], "helios")
        self.assertEqual(history[1]["routing"]["destination"]["participant_key"], "peter")
        self.assertEqual([item["routing"]["routing_mode"] for item in history], ["explicit", "explicit"])

    def test_every_unavailable_participant_shape_is_409_before_environment(self) -> None:
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            room_id, _peter_id, _helios_id, _system_id = self.ids(connection)
            other_id = connection.execute(
                "INSERT INTO participants(participant_key,name,participant_type) VALUES ('other-ai','Other AI','ai')"
            ).lastrowid
            alias_id = connection.execute(
                "INSERT INTO participant_aliases(participant_id,display_alias,alias_key) VALUES (?,'Other AI','other ai')",
                (other_id,),
            ).lastrowid
            connection.execute(
                "INSERT INTO participant_primary_aliases(participant_id,alias_id) VALUES (?,?)",
                (other_id, alias_id),
            )
            connection.execute(
                """INSERT INTO participant_name_events
                   (event_type,room_id,actor_participant_id,subject_participant_id,
                    previous_alias_id,new_alias_id,canonical_message_id)
                   VALUES ('bootstrap',NULL,?,?,NULL,?,NULL)""",
                (other_id, other_id, alias_id),
            )
            connection.execute(
                "INSERT INTO room_participants(room_id,participant_id) VALUES (?,?)",
                (room_id, other_id),
            )
            connection.commit()
        with patch("app.room_service.load_openai_environment", side_effect=AssertionError("environment must not load")):
            for key in ("missing", "peter", "room-system", "other-ai"):
                with self.subTest(key=key):
                    response = asyncio.run(self.request(
                        "POST", "/api/messages",
                        json={"message_text": "hello", "destination": {"kind": "participant", "participant_key": key}},
                    ))
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["error"], "participant_destination_unavailable")
                    self.assertNotIn(key, response.text)
        with closing(connect_database(self.database_path)) as connection:
            connection.execute(
                """UPDATE room_participants SET left_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE participant_id=(SELECT id FROM participants WHERE participant_key='helios')"""
            )
            connection.commit()
        with patch("app.room_service.load_openai_environment", side_effect=AssertionError("environment must not load")):
            response = asyncio.run(self.request(
                "POST", "/api/messages",
                json={"message_text": "hello", "destination": {"kind": "participant", "participant_key": "helios"}},
            ))
        self.assertEqual(response.status_code, 503)
        with closing(connect_database(self.database_path)) as connection:
            for table in ("turns", "messages", "message_routes", "api_events"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_request_validation_local_and_destination_errors_do_not_leak(self) -> None:
        cases = [
            (
                {"message_text": "hello", "destination": {"kind": "room"}, "client_secret": "TOP-SECRET-A"},
                422, "invalid_message_request", "The message request is invalid.", "TOP-SECRET-A",
            ),
            (
                {"message_text": "hello", "destination": {"kind": "room", "token": "TOP-SECRET-B"}},
                422, "invalid_message_request", "The message request is invalid.", "TOP-SECRET-B",
            ),
            (
                {"message_text": "/participants TOP-SECRET-C", "destination": {"kind": "room"}},
                400, "local_command_only", "Local commands are available only in the browser interface.", "TOP-SECRET-C",
            ),
            (
                {"message_text": "hello", "destination": {"kind": "participant", "participant_key": "TOP-SECRET-D"}},
                409, "participant_destination_unavailable", "The selected participant destination is unavailable.", "TOP-SECRET-D",
            ),
        ]
        with patch("app.room_service.load_openai_environment", side_effect=AssertionError("no env")):
            for body, status, code, message, secret in cases:
                with self.subTest(code=code):
                    response = asyncio.run(self.request("POST", "/api/messages", json=body))
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json(), {"error": code, "message": message})
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertNotIn(secret, response.text + str(response.headers))
                    self.assertNotIn("client_secret", response.text)
                    self.assertNotIn("token", response.text)
        malformed = asyncio.run(self.request(
            "POST", "/api/messages",
            content=b'{"message_text":',
            headers={"content-type": "application/json"},
        ))
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.json(), {
            "error": "invalid_message_request", "message": "The message request is invalid."
        })
        self.assertEqual(malformed.headers["cache-control"], "no-store")
        with closing(connect_database(self.database_path)) as connection:
            for table in ("turns", "messages", "message_routes", "api_events"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_room_internal_failure_is_sanitized_and_atomic(self) -> None:
        secret = "ROOM-TEXT-SECRET"
        with patch("app.identity_service.store_message", side_effect=RuntimeError("SQL PATH " + secret)):
            response = asyncio.run(self.request(
                "POST", "/api/messages",
                json={"message_text": secret, "destination": {"kind": "room"}},
            ))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "room_post_failed", "message": "The Room message could not be saved."})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(secret, response.text + str(response.headers))
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_history_and_directory_use_exact_sanitized_read_errors(self) -> None:
        room_response = asyncio.run(self.request(
            "POST", "/api/messages",
            json={"message_text": "history anchor", "destination": {"kind": "room"}},
        ))
        self.assertEqual(room_response.status_code, 200)
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("DROP TRIGGER message_routes_no_delete")
            connection.execute("DELETE FROM message_routes")
            connection.commit()
        history = asyncio.run(self.request("GET", "/api/messages"))
        self.assertEqual(history.status_code, 500)
        self.assertEqual(history.json(), {
            "error": "message_history_invalid",
            "message": "The message history data is invalid.",
        })
        self.assertEqual(history.headers["cache-control"], "no-store")
        trace = asyncio.run(self.request("GET", f"/api/trace/{room_response.json()['turn_id']}"))
        self.assertEqual(trace.status_code, 500)
        self.assertEqual(trace.json()["error"], "trace_data_invalid")

        with closing(connect_database(self.database_path)) as connection:
            connection.execute("DROP TRIGGER participant_aliases_no_update")
            connection.execute(
                """UPDATE participant_aliases SET display_alias='PRIVATE-SENTINEL'
                   WHERE participant_id=(SELECT id FROM participants WHERE participant_key='peter')"""
            )
            connection.commit()
        directory = asyncio.run(self.request("GET", "/api/participants"))
        self.assertEqual(directory.status_code, 500)
        self.assertEqual(directory.json(), {
            "error": "participant_directory_invalid",
            "message": "The participant directory data is invalid.",
        })
        self.assertEqual(directory.headers["cache-control"], "no-store")
        self.assertNotIn("PRIVATE-SENTINEL", directory.text + str(directory.headers))

    def test_missing_read_database_is_503_and_not_created(self) -> None:
        missing = Path(self.temp.name) / "absent" / "missing.db"
        main.app.state.database_path = missing
        for path, code, message in (
            ("/api/participants", "participant_directory_unavailable", "The participant directory is unavailable."),
            ("/api/messages", "message_history_unavailable", "The message history is unavailable."),
        ):
            with self.subTest(path=path):
                response = asyncio.run(self.request("GET", path))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"error": code, "message": message})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "old.db"

    def make_v12(self, *, second_name: str | None = None) -> None:
        schema = (Path(__file__).parents[1] / "schema" / "helios_room_schema_v1_2.sql").read_text(encoding="utf-8")
        connection = sqlite3.connect(self.path)
        connection.executescript(schema)
        connection.execute("INSERT INTO rooms(room_key,name) VALUES ('main','The Room')")
        room_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO participants(participant_key,name,participant_type) VALUES ('peter','Peter','human')")
        peter_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO participants(participant_key,name,participant_type) VALUES ('helios','Helios','ai')")
        helios_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        for participant_id in (peter_id, helios_id):
            connection.execute("INSERT INTO room_participants(room_id,participant_id) VALUES (?,?)", (room_id, participant_id))
        connection.execute("""INSERT INTO participant_configs
            (participant_id,provider,config_label,settings_json,tools_json)
            VALUES (?,'openai','initial','{}','[]')""", (helios_id,))
        connection.execute("INSERT INTO turns(room_id,initiated_by_participant_id,status) VALUES (?,?,'open')", (room_id, peter_id))
        turn_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("""INSERT INTO messages
            (turn_id,room_id,room_sequence_no,turn_sequence_no,participant_id,message_type,message_text)
            VALUES (?,?,1,1,?,'chat','exact legacy text')""", (turn_id, room_id, peter_id))
        if second_name is not None:
            connection.execute(
                "INSERT INTO participants(participant_key,name,participant_type) VALUES ('other',?,'human')",
                (second_name,),
            )
        connection.commit()
        connection.close()

    def test_old_database_requires_reset_without_migration_writes(self) -> None:
        self.make_v12()
        with self.assertRaises(DatabaseInitializationError) as init_error:
            initialize_database(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(DatabaseMigrationError) as migration_error:
            migrate_database(self.path)
        self.assertEqual(init_error.exception.code, "database_reset_required")
        self.assertEqual(migration_error.exception.code, "database_reset_required")
        self.assertEqual(before, self.path.read_bytes())

    def test_collision_failure_is_atomic_exact_v12(self) -> None:
        self.make_v12(second_name="Ｐｅｔｅｒ")
        connection = sqlite3.connect(self.path)
        before_schema = connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
        before_history = connection.execute("SELECT * FROM schema_migrations").fetchall()
        connection.close()
        with self.assertRaises(DatabaseMigrationError):
            migrate_database(self.path)
        connection = sqlite3.connect(self.path)
        self.assertEqual(before_schema, connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall())
        self.assertEqual(before_history, connection.execute("SELECT * FROM schema_migrations").fetchall())
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name='participant_aliases'"
        ).fetchone()[0], 0)
        connection.close()


if __name__ == "__main__":
    unittest.main()
