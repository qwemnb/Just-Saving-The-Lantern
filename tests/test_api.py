from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import main
from app.database import connect_database, initialize_database


class MessageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "helios.db"
        initialize_database(self.database_path)

    def test_whitespace_only_message_is_ignored(self) -> None:
        def connect_test_database():
            return connect_database(self.database_path)

        with patch("app.main.connect_database", side_effect=connect_test_database):
            response = asyncio.run(
                main.post_message(main.MessageRequest(message_text=" \t\r\n "))
            )

        self.assertEqual(
            response,
            {"ignored": True, "reason": "empty_message"},
        )

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                0,
            )

    def test_cli_ignores_whitespace_only_message(self) -> None:
        arguments = [
            "helios-room",
            "store-message",
            "--database",
            str(self.database_path),
            "--message",
            "\t\r\n",
        ]

        with patch.object(sys, "argv", arguments), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            main.main()

        self.assertEqual(
            json.loads(output.getvalue()),
            {"ignored": True, "reason": "empty_message"},
        )

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
