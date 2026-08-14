from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.database import connect_database, initialize_database, store_message


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "helios.db"

    def test_initializes_schema_and_milestone_records(self) -> None:
        report = initialize_database(self.database_path)

        self.assertEqual(report.schema_label, "1.3")
        self.assertEqual(report.room_count, 1)
        self.assertEqual(report.participant_count, 4)
        self.assertEqual(report.active_membership_count, 3)
        self.assertEqual(report.helios_config_count, 1)
        self.assertEqual(report.integrity_check, "ok")
        self.assertEqual(report.foreign_key_violations, 0)

        with closing(connect_database(self.database_path)) as connection:
            room = connection.execute(
                "SELECT room_key, name FROM rooms"
            ).fetchone()
            participants = connection.execute(
                """
                SELECT participant_key, name, participant_type
                FROM participants
                ORDER BY participant_key
                """
            ).fetchall()
            config = connection.execute(
                """
                SELECT
                    p.participant_key,
                    pc.provider,
                    pc.model,
                    pc.config_label,
                    pc.system_instructions,
                    pc.settings_json,
                    pc.tools_json
                FROM participant_configs AS pc
                JOIN participants AS p ON p.id = pc.participant_id
                """
            ).fetchone()

        self.assertEqual(tuple(room), ("main", "The Room"))
        self.assertEqual(
            [tuple(row) for row in participants],
            [
                ("gemini", "Gemini", "ai"),
                ("helios", "Helios", "ai"),
                ("peter", "Peter", "human"),
                ("room-system", "Room", "system"),
            ],
        )
        self.assertEqual(
            tuple(config),
            ("helios", "openai", None, "initial", None, "{}", "[]"),
        )

    def test_initialization_is_idempotent(self) -> None:
        initialize_database(self.database_path)
        report = initialize_database(self.database_path)

        self.assertEqual(report.room_count, 1)
        self.assertEqual(report.participant_count, 4)
        self.assertEqual(report.active_membership_count, 3)
        self.assertEqual(report.helios_config_count, 1)

    def test_connections_enforce_foreign_keys(self) -> None:
        initialize_database(self.database_path)

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO room_participants (room_id, participant_id)
                    VALUES (?, ?)
                    """,
                    (999_999, 999_999),
                )

    def test_store_message_rejects_whitespace_only_text(self) -> None:
        initialize_database(self.database_path)

        with closing(connect_database(self.database_path)) as connection:
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key = ?",
                ("main",),
            ).fetchone()[0]
            peter_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key = ?",
                ("peter",),
            ).fetchone()[0]

            with self.assertRaisesRegex(ValueError, "non-whitespace"):
                store_message(
                    connection,
                    room_id=room_id,
                    participant_id=peter_id,
                    message_text=" \t\r\n ",
                )

            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()

