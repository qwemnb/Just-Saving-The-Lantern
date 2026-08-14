from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main
from app.database import connect_database, initialize_database
from app.identity_service import (
    IdentityServiceError,
    adopt_participant_name,
    load_message_history,
    load_participant_directory,
    post_room_message,
)
from app.preflight import DatabasePreflightError, preflight_database
from app.read_snapshot import ReadSnapshotError, run_read_snapshot
from app.room_service import TurnServiceError, run_helios_turn
from app.schema_validation import SchemaValidationError
from app.trace_service import TraceServiceError, load_trace


class SchemaPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_path = Path(self.temp.name) / "helios.db"

    def make_v13(self) -> Path:
        initialize_database(self.database_path)
        return self.database_path

    def make_v12(self) -> Path:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                (Path(__file__).parents[1] / "schema" / "helios_room_schema_v1_2.sql").read_text(
                    encoding="utf-8"
                )
            )
            room_id = connection.execute(
                "INSERT INTO rooms(room_key,name) VALUES ('main','The Room')"
            ).lastrowid
            peter_id = connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('peter','Peter','human')"""
            ).lastrowid
            helios_id = connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('helios','Helios','ai')"""
            ).lastrowid
            connection.executemany(
                "INSERT INTO room_participants(room_id,participant_id) VALUES (?,?)",
                ((room_id, peter_id), (room_id, helios_id)),
            )
            connection.commit()
        return self.database_path

    def assert_preflight_error(self, code: str) -> DatabasePreflightError:
        with self.assertRaises(DatabasePreflightError) as raised:
            preflight_database(self.database_path)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_clean_fresh_and_migrated_v13_use_immutable_without_sidecars(self) -> None:
        self.make_v13()
        report = preflight_database(self.database_path)
        self.assertEqual((report.schema_label, report.snapshot_branch), ("1.3", "immutable"))
        self.assertFalse(Path(f"{self.database_path}-wal").exists())
        self.assertFalse(Path(f"{self.database_path}-shm").exists())

        migrated = Path(self.temp.name) / "migrated.db"
        self.database_path = migrated
        self.make_v12()
        from app.migration import migrate_database

        migrate_database(migrated)
        self.assertEqual(preflight_database(migrated).snapshot_branch, "immutable")
        self.assertFalse(Path(f"{migrated}-wal").exists())
        self.assertFalse(Path(f"{migrated}-shm").exists())

    def test_validation_surfaces_share_the_same_pure_validator_functions(self) -> None:
        from app import database, identity_service, migration, preflight, trace_service
        from app import schema_validation

        self.assertIs(preflight.validate_v12_source, schema_validation.validate_v12_source)
        self.assertIs(migration.validate_v12_source, schema_validation.validate_v12_source)
        self.assertIs(preflight.validate_v13_foundation, schema_validation.validate_v13_foundation)
        self.assertIs(database.validate_v13_foundation, schema_validation.validate_v13_foundation)
        self.assertIs(identity_service.validate_v13_foundation, schema_validation.validate_v13_foundation)
        self.assertIs(trace_service.validate_v13_foundation, schema_validation.validate_v13_foundation)

    def test_exact_v12_is_migration_required_but_spoof_is_incompatible(self) -> None:
        self.make_v12()
        error = self.assert_preflight_error("database_migration_required")
        self.assertEqual(
            error.message,
            "Database schema 1.2 must be migrated to 1.3 before the server can start.",
        )
        spoof = Path(self.temp.name) / "spoof.db"
        self.database_path = spoof
        with closing(sqlite3.connect(spoof)) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations(migration_no INTEGER, schema_label TEXT)"
            )
            connection.execute("INSERT INTO schema_migrations VALUES (1,'1.2')")
            connection.commit()
        self.assert_preflight_error("database_schema_incompatible")

    def test_v12_every_migration_source_violation_category_is_incompatible(self) -> None:
        def foreign_key(connection: sqlite3.Connection) -> None:
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO room_participants(room_id,participant_id) VALUES (?,999999)",
                (room_id,),
            )

        def identity(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE participants SET name='Not Peter' WHERE participant_key='peter'"
            )

        def reserved_name(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('reserved','System','human')"""
            )

        def alias_collision(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('collision','Ｐｅｔｅｒ','human')"""
            )

        def room_graph(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE rooms SET name='Wrong Room' WHERE room_key='main'"
            )

        def room_system_conflict(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('room-system','Room Owner','system')"""
            )

        def historical_message(connection: sqlite3.Connection) -> None:
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]
            peter_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='peter'"
            ).fetchone()[0]
            other_id = connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('historical-other','Historical Other','human')"""
            ).lastrowid
            turn_id = connection.execute(
                """INSERT INTO turns(room_id,initiated_by_participant_id,status)
                   VALUES (?,?,'open')""",
                (room_id, peter_id),
            ).lastrowid
            connection.execute(
                """INSERT INTO messages
                   (turn_id,room_id,room_sequence_no,turn_sequence_no,
                    participant_id,message_type,message_text)
                   VALUES (?,?,1,1,?,'chat','unsupported sender')""",
                (turn_id, room_id, other_id),
            )

        cases = {
            "foreign_keys": foreign_key,
            "identity": identity,
            "reserved_names": reserved_name,
            "alias_collisions": alias_collision,
            "room_graph": room_graph,
            "room_system": room_system_conflict,
            "historical_messages": historical_message,
        }
        for index, (category, mutate) in enumerate(cases.items(), start=1):
            with self.subTest(category=category):
                self.database_path = Path(self.temp.name) / f"v12-{index}.db"
                self.make_v12()
                with closing(sqlite3.connect(self.database_path)) as connection:
                    mutate(connection)
                    connection.commit()
                self.assert_preflight_error("database_schema_incompatible")

        self.database_path = Path(self.temp.name) / "v12-integrity.db"
        self.make_v12()
        with patch(
            "app.schema_validation.validate_database_integrity",
            side_effect=SchemaValidationError("synthetic integrity failure"),
        ):
            self.assert_preflight_error("database_schema_incompatible")

        partial = Path(self.temp.name) / "partial-migration.db"
        self.database_path = partial
        self.make_v12()
        with closing(sqlite3.connect(partial)) as connection:
            connection.execute(
                "CREATE TABLE participant_aliases(id INTEGER PRIMARY KEY)"
            )
            connection.commit()
        self.assert_preflight_error("database_schema_incompatible")

    def test_missing_empty_malformed_newer_and_partial_fail_closed(self) -> None:
        missing_parent = Path(self.temp.name) / "missing" / "room.db"
        self.database_path = missing_parent
        self.assert_preflight_error("database_not_initialized")
        self.assertFalse(missing_parent.exists())
        self.assertFalse(missing_parent.parent.exists())

        for name, creator in (
            ("empty.db", lambda path: path.touch()),
            ("malformed.db", lambda path: path.write_bytes(b"not sqlite")),
        ):
            with self.subTest(name=name):
                self.database_path = Path(self.temp.name) / name
                creator(self.database_path)
                self.assert_preflight_error("database_schema_incompatible")

        self.database_path = Path(self.temp.name) / "partial.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations(migration_no INTEGER, schema_label TEXT)"
            )
            connection.executemany(
                "INSERT INTO schema_migrations VALUES (?,?)",
                ((1, "1.2"), (2, "1.3")),
            )
            connection.commit()
        self.assert_preflight_error("database_schema_incompatible")

    def test_v13_divergent_schema_object_categories_are_incompatible(self) -> None:
        def table(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE unexpected_object(id INTEGER PRIMARY KEY)")

        def column(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE rooms ADD COLUMN unexpected TEXT")

        def view(connection: sqlite3.Connection) -> None:
            connection.execute("DROP VIEW current_room_participants")
            connection.execute(
                """CREATE VIEW current_room_participants AS
                   SELECT id,room_id,participant_id,joined_at
                   FROM room_participants WHERE left_at IS NOT NULL"""
            )

        def index(connection: sqlite3.Connection) -> None:
            connection.execute("DROP INDEX idx_messages_turn")

        def constraint(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='rooms'"
            ).fetchone()
            changed = row[0].replace(
                "name TEXT NOT NULL CHECK (trim(name) <> '')",
                "name TEXT NOT NULL CHECK (length(name) > 0)",
            )
            self.assertNotEqual(changed, row[0])
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='rooms'",
                (changed,),
            )
            connection.execute("PRAGMA writable_schema=OFF")
            version = connection.execute("PRAGMA schema_version").fetchone()[0]
            connection.execute(f"PRAGMA schema_version={version + 1}")

        def trigger(connection: sqlite3.Connection) -> None:
            connection.execute("DROP TRIGGER message_routes_no_delete")

        cases = {
            "table": table,
            "column": column,
            "view": view,
            "index": index,
            "constraint": constraint,
            "trigger": trigger,
        }
        for index_value, (category, mutate) in enumerate(cases.items(), start=1):
            with self.subTest(category=category):
                self.database_path = Path(self.temp.name) / f"v13-{index_value}.db"
                self.make_v13()
                with closing(sqlite3.connect(self.database_path)) as connection:
                    mutate(connection)
                    connection.commit()
                self.assert_preflight_error("database_schema_incompatible")

    def test_both_sidecars_use_wal_and_preserve_main_and_wal_bytes(self) -> None:
        self.make_v13()
        connection = connect_database(self.database_path)
        self.addCleanup(connection.close)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO rooms(room_key,name) VALUES ('extra','Extra')")
        connection.commit()
        wal_path = Path(f"{self.database_path}-wal")
        shm_path = Path(f"{self.database_path}-shm")
        self.assertTrue(wal_path.exists() and shm_path.exists())
        before_main = self.database_path.read_bytes()
        before_wal = wal_path.read_bytes()
        report = preflight_database(self.database_path)
        self.assertEqual(report.snapshot_branch, "wal")
        self.assertEqual(self.database_path.read_bytes(), before_main)
        self.assertEqual(wal_path.read_bytes(), before_wal)
        self.assertTrue(shm_path.exists())

    def test_one_sided_states_fail_before_sqlite_open(self) -> None:
        self.make_v13()
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = Path(f"{self.database_path}{suffix}")
                sidecar.write_bytes(b"unsupported")
                with patch(
                    "app.read_snapshot._open_connection",
                    side_effect=AssertionError("SQLite must not open"),
                ):
                    self.assert_preflight_error("database_schema_incompatible")
                sidecar.unlink()

    def test_sidecar_free_non_wal_header_is_rejected(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY)")
            connection.commit()
        header = self.database_path.read_bytes()[:20]
        self.assertEqual(header[18:20], b"\x01\x01")
        self.assert_preflight_error("database_schema_incompatible")

    def test_every_strong_fingerprint_field_change_discards_result(self) -> None:
        self.make_v13()
        from app import read_snapshot

        real = read_snapshot.fingerprint_file
        changes = {
            "canonical_path": lambda value: f"{value}.replacement",
            "device": lambda value: (value or 0) + 1,
            "inode": lambda value: (value or 0) + 1,
            "size": lambda value: value + 1,
            "mtime_ns": lambda value: value + 1,
            "sha256": lambda _value: "f" * 64,
        }
        for field_name, change in changes.items():
            with self.subTest(field=field_name):
                calls = 0

                def changed(path: Path):
                    nonlocal calls
                    fingerprint = real(path)
                    calls += 1
                    if calls == 2:
                        return replace(
                            fingerprint,
                            **{field_name: change(getattr(fingerprint, field_name))},
                        )
                    return fingerprint

                with patch(
                    "app.read_snapshot.fingerprint_file", side_effect=changed
                ):
                    self.assert_preflight_error("database_schema_incompatible")

    def test_immutable_result_retries_once_only_after_both_sidecars_appear(self) -> None:
        self.make_v13()
        writer: sqlite3.Connection | None = None
        calls = 0

        def read_with_concurrent_writer(connection: sqlite3.Connection) -> int:
            nonlocal writer, calls
            calls += 1
            if calls == 1:
                writer = connect_database(self.database_path)
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO rooms(room_key,name) VALUES ('concurrent','Concurrent')"
                )
                writer.commit()
            return connection.execute("SELECT count(*) FROM rooms").fetchone()[0]

        try:
            result = run_read_snapshot(self.database_path, read_with_concurrent_writer)
            self.assertEqual(result.branch, "wal")
            self.assertEqual(result.value, 2)
            self.assertEqual(calls, 2)
        finally:
            if writer is not None:
                writer.close()

    def test_startup_discards_sidecar_appearance_without_retrying(self) -> None:
        self.make_v13()
        from app import preflight

        writer: sqlite3.Connection | None = None
        calls = 0
        real_validate = preflight.validate_v13_foundation

        def validate_then_write(connection: sqlite3.Connection) -> None:
            nonlocal writer, calls
            calls += 1
            real_validate(connection)
            writer = connect_database(self.database_path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO rooms(room_key,name) VALUES ('during-startup','During Startup')"
            )
            writer.commit()

        stderr = io.StringIO()
        try:
            with patch(
                "app.preflight.validate_v13_foundation",
                side_effect=validate_then_write,
            ), patch.object(
                sys,
                "argv",
                ["helios", "serve", "--database", str(self.database_path)],
            ), patch("app.main.uvicorn.run") as run, redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main.main()
            self.assertEqual(raised.exception.code, 1)
            run.assert_not_called()
            self.assertEqual(calls, 1)
            self.assertEqual(
                json.loads(stderr.getvalue())["error"],
                "database_schema_incompatible",
            )
        finally:
            if writer is not None:
                writer.close()

    def test_immutable_result_never_retries_a_one_sided_appearance(self) -> None:
        self.make_v13()
        calls = 0
        wal_path = Path(f"{self.database_path}-wal")

        def create_unsafe_sidecar(connection: sqlite3.Connection) -> int:
            nonlocal calls
            calls += 1
            wal_path.write_bytes(b"unsafe test sidecar")
            return connection.execute("SELECT count(*) FROM rooms").fetchone()[0]

        try:
            with self.assertRaises(ReadSnapshotError):
                run_read_snapshot(self.database_path, create_unsafe_sidecar)
            self.assertEqual(calls, 1)
        finally:
            if wal_path.exists():
                wal_path.unlink()

    def test_mature_rows_and_valid_name_lineage_pass(self) -> None:
        self.make_v13()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]
            peter_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='peter'"
            ).fetchone()[0]
            adopt_participant_name(connection, room_id, peter_id, peter_id, "Petrus")
            extra_room = connection.execute(
                "INSERT INTO rooms(room_key,name) VALUES ('studio','Studio')"
            ).lastrowid
            extra_id = connection.execute(
                """INSERT INTO participants(participant_key,name,participant_type)
                   VALUES ('observer','Observer','human')"""
            ).lastrowid
            alias_id = connection.execute(
                """INSERT INTO participant_aliases(participant_id,display_alias,alias_key)
                   VALUES (?,'Observer','observer')""",
                (extra_id,),
            ).lastrowid
            connection.execute(
                "INSERT INTO participant_primary_aliases VALUES (?,?)", (extra_id, alias_id)
            )
            connection.execute(
                """INSERT INTO participant_name_events
                   (event_type,actor_participant_id,subject_participant_id,new_alias_id)
                   VALUES ('bootstrap',?,?,?)""",
                (extra_id, extra_id, alias_id),
            )
            connection.execute(
                "INSERT INTO room_participants(room_id,participant_id) VALUES (?,?)",
                (room_id, extra_id),
            )
            connection.execute(
                """INSERT INTO participant_configs
                   (participant_id,provider,model,config_label,settings_json,tools_json)
                   SELECT id,'openai','offline-model','historical','{}','[]'
                   FROM participants WHERE participant_key='helios'"""
            )
            connection.execute(
                """INSERT INTO turns(room_id,initiated_by_participant_id,status)
                   VALUES (?,?,'open')""",
                (room_id, peter_id),
            )
            connection.commit()
        self.assertEqual(preflight_database(self.database_path).schema_label, "1.3")
        directory = load_participant_directory(self.database_path)
        peter = next(
            item for item in directory["destinations"]
            if item.get("participant_key") == "peter"
        )
        self.assertEqual(peter["primary_name"], "Petrus")

    def test_direct_primary_change_and_broken_lineage_fail_foundation(self) -> None:
        self.make_v13()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peter_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='peter'"
            ).fetchone()[0]
            alias_id = connection.execute(
                """INSERT INTO participant_aliases(participant_id,display_alias,alias_key)
                   VALUES (?,'Petrus','petrus')""",
                (peter_id,),
            ).lastrowid
            connection.execute(
                "UPDATE participant_primary_aliases SET alias_id=? WHERE participant_id=?",
                (alias_id, peter_id),
            )
            connection.commit()
        self.assert_preflight_error("database_schema_incompatible")

    def test_broken_adopted_event_lineage_categories_fail_foundation(self) -> None:
        def corrupt(
            connection: sqlite3.Connection,
            trigger_name: str,
            statement: str,
            parameters: tuple[object, ...],
            *,
            ignore_checks: bool = False,
        ) -> None:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {trigger_name}")
            if ignore_checks:
                connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(statement, parameters)
            if ignore_checks:
                connection.execute("PRAGMA ignore_check_constraints=OFF")
            connection.execute(trigger_sql)

        for category in ("actor", "alias_chain", "canonical_message", "room_route"):
            with self.subTest(category=category):
                self.database_path = Path(self.temp.name) / f"lineage-{category}.db"
                self.make_v13()
                with closing(connect_database(self.database_path)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    room_id = connection.execute(
                        "SELECT id FROM rooms WHERE room_key='main'"
                    ).fetchone()[0]
                    peter_id = connection.execute(
                        "SELECT id FROM participants WHERE participant_key='peter'"
                    ).fetchone()[0]
                    adopt_participant_name(
                        connection, room_id, peter_id, peter_id, "Petrus"
                    )
                    adopt_participant_name(
                        connection, room_id, peter_id, peter_id, "Peter Prime"
                    )
                    connection.commit()

                with closing(sqlite3.connect(self.database_path)) as connection:
                    adopted = connection.execute(
                        """SELECT id,previous_alias_id,new_alias_id,canonical_message_id
                           FROM participant_name_events
                           WHERE event_type='adopted' ORDER BY id"""
                    ).fetchall()
                    helios_id = connection.execute(
                        "SELECT id FROM participants WHERE participant_key='helios'"
                    ).fetchone()[0]
                    bootstrap_alias = connection.execute(
                        """SELECT new_alias_id FROM participant_name_events
                           WHERE event_type='bootstrap' AND subject_participant_id=
                           (SELECT id FROM participants WHERE participant_key='peter')"""
                    ).fetchone()[0]
                    if category == "actor":
                        corrupt(
                            connection,
                            "participant_name_events_no_update",
                            "UPDATE participant_name_events SET actor_participant_id=? WHERE id=?",
                            (helios_id, adopted[0][0]),
                            ignore_checks=True,
                        )
                    elif category == "alias_chain":
                        corrupt(
                            connection,
                            "participant_name_events_no_update",
                            "UPDATE participant_name_events SET previous_alias_id=? WHERE id=?",
                            (bootstrap_alias, adopted[1][0]),
                        )
                    elif category == "canonical_message":
                        corrupt(
                            connection,
                            "messages_no_update",
                            "UPDATE messages SET message_text='Incorrect adoption notice' WHERE id=?",
                            (adopted[0][3],),
                        )
                    else:
                        corrupt(
                            connection,
                            "message_routes_no_update",
                            "UPDATE message_routes SET routing_mode='legacy_implicit' WHERE message_id=?",
                            (adopted[0][3],),
                        )
                    connection.commit()
                self.assert_preflight_error("database_schema_incompatible")

    def test_trace_invalid_turn_is_isolated_from_startup_and_other_reads(self) -> None:
        self.make_v13()
        valid = post_room_message("valid isolated turn", self.database_path)
        invalid = post_room_message("invalid isolated turn", self.database_path)
        with closing(connect_database(self.database_path)) as connection:
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='helios'"
            ).fetchone()[0]
            config_id = connection.execute(
                """SELECT id FROM participant_configs
                   WHERE participant_id=? ORDER BY id LIMIT 1""",
                (helios_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO api_events
                   (turn_id,room_id,participant_id,participant_config_id,
                    sequence_no,event_type,payload_json)
                   VALUES (?,?,?,?,1,'openai.responses.request','{}')""",
                (invalid["turn_id"], room_id, helios_id, config_id),
            )
            connection.commit()

        self.assertEqual(preflight_database(self.database_path).schema_label, "1.3")
        self.assertTrue(load_participant_directory(self.database_path)["destinations"])
        self.assertEqual(len(load_message_history(self.database_path)), 2)
        self.assertEqual(
            load_trace(self.database_path, valid["turn_id"])["turn"]["id"],
            valid["turn_id"],
        )
        with self.assertRaises(TraceServiceError) as raised:
            load_trace(self.database_path, invalid["turn_id"])
        self.assertEqual(raised.exception.code, "trace_data_invalid")

    def test_post_preflight_replacement_fails_closed_across_every_service(self) -> None:
        def replace_with_incompatible(path: Path) -> None:
            path.unlink()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations(migration_no INTEGER, schema_label TEXT)"
                )
                connection.execute("INSERT INTO schema_migrations VALUES (99,'future')")
                connection.commit()

        async def helios_turn(path: Path) -> None:
            await run_helios_turn(
                "must fail before provider setup",
                database_path=path,
                dotenv_path=Path(self.temp.name) / "missing.env",
                client_factory=lambda _key: (_ for _ in ()).throw(
                    AssertionError("provider must not be constructed")
                ),
            )

        cases = {
            "directory": (
                lambda path: load_participant_directory(path),
                IdentityServiceError,
            ),
            "history": (
                lambda path: load_message_history(path),
                IdentityServiceError,
            ),
            "trace": (
                lambda path: load_trace(path),
                TraceServiceError,
            ),
            "room_post": (
                lambda path: post_room_message("must fail", path),
                IdentityServiceError,
            ),
            "helios_turn": (
                lambda path: asyncio.run(helios_turn(path)),
                TurnServiceError,
            ),
        }
        for index_value, (surface, (call, error_type)) in enumerate(
            cases.items(), start=1
        ):
            with self.subTest(surface=surface):
                path = Path(self.temp.name) / f"replaced-{index_value}.db"
                self.database_path = path
                self.make_v13()
                self.assertEqual(preflight_database(path).schema_label, "1.3")
                replace_with_incompatible(path)
                with self.assertRaises(error_type):
                    call(path)

    def test_serve_calls_uvicorn_only_after_valid_preflight(self) -> None:
        self.make_v13()
        with patch.object(
            sys, "argv", ["helios", "serve", "--database", str(self.database_path)]
        ), patch("app.main.uvicorn.run") as run:
            with redirect_stdout(io.StringIO()):
                main.main()
        run.assert_called_once()

        missing = Path(self.temp.name) / "missing" / "room.db"
        v12 = Path(self.temp.name) / "serve-v12.db"
        self.database_path = v12
        self.make_v12()
        incompatible = Path(self.temp.name) / "serve-incompatible.db"
        incompatible.write_bytes(b"malformed database")
        cases = (
            (
                missing,
                "database_not_initialized",
                "The Helios Room database is not initialized.",
            ),
            (
                v12,
                "database_migration_required",
                "Database schema 1.2 must be migrated to 1.3 before the server can start.",
            ),
            (
                incompatible,
                "database_schema_incompatible",
                "The Helios Room database schema is incompatible with this application.",
            ),
        )
        for path, code, message in cases:
            with self.subTest(code=code):
                stderr = io.StringIO()
                with patch.object(
                    sys, "argv", ["helios", "serve", "--database", str(path)]
                ), patch("app.main.uvicorn.run") as run, redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main.main()
                self.assertEqual(raised.exception.code, 1)
                run.assert_not_called()
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {"error": code, "message": message},
                )
        self.assertFalse(missing.parent.exists())

    def test_cli_error_does_not_leak_path_or_malformed_values(self) -> None:
        secret = "PRIVATE-PREFLIGHT-SENTINEL"
        self.database_path = Path(self.temp.name) / f"{secret}.db"
        self.database_path.write_bytes(b"not sqlite " + secret.encode())
        stderr = io.StringIO()
        with patch.object(
            sys, "argv", ["helios", "serve", "--database", str(self.database_path)]
        ), patch("app.main.uvicorn.run") as run, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                main.main()
        run.assert_not_called()
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue())["error"], "database_schema_incompatible"
        )


class BrowserCacheContractTests(unittest.TestCase):
    async def request(self, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get(path)

    def test_root_is_no_store_and_references_matching_asset_versions(self) -> None:
        response = asyncio.run(self.request("/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("style.css?v=gemini-participant-v1", response.text)
        self.assertIn("app.js?v=gemini-participant-v1", response.text)


if __name__ == "__main__":
    unittest.main()
