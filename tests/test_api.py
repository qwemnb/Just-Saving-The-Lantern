from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app import main
from app.database import connect_database, initialize_database


class MessageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "helios.db"
        initialize_database(self.database_path)
        self.original_database_path = main.app.state.database_path
        self.original_client_factory = main.app.state.openai_client_factory
        self.original_gemini_client_factory = main.app.state.gemini_client_factory
        self.original_dotenv_path = main.app.state.dotenv_path
        main.app.state.database_path = self.database_path
        main.app.state.openai_client_factory = self._provider_must_not_run
        main.app.state.dotenv_path = Path(self.temporary_directory.name) / "missing.env"
        self.addCleanup(self._restore_app_state)

    def _restore_app_state(self) -> None:
        main.app.state.database_path = self.original_database_path
        main.app.state.openai_client_factory = self.original_client_factory
        main.app.state.gemini_client_factory = self.original_gemini_client_factory
        main.app.state.dotenv_path = self.original_dotenv_path

    @staticmethod
    def _provider_must_not_run(api_key: str):
        del api_key
        raise AssertionError("The provider must not be constructed")

    def test_whitespace_only_message_is_ignored(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = asyncio.run(
                main.post_message(main.MessageRequest(
                    message_text=" \t\r\n ",
                    destination={"kind": "participant", "participant_key": "helios"},
                ))
            )

        self.assertEqual(
            json.loads(response.body),
            {"ignored": True, "reason": "empty_message"},
        )

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                0,
            )

    def test_request_model_forbids_authorship_even_when_blank(self) -> None:
        with self.assertRaises(ValidationError):
            main.MessageRequest.model_validate(
                {"message_text": " \t\n", "participant_key": "helios"}
            )

        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 0)

    def test_room_post_rejects_response_destination_before_any_write(self) -> None:
        response = asyncio.run(main.post_message(main.MessageRequest(
            message_text="invalid routed Room post",
            destination={"kind": "room"},
            response_destination={"kind": "participant", "participant_key": "peter"},
        )))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"], "invalid_request")
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_browser_sends_exact_text_without_authorship(self) -> None:
        javascript = (Path(__file__).parents[1] / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("const messageText = input.value;", javascript)
        self.assertIn("if (!messageText.trim()) return;", javascript)
        self.assertIn("message_text: messageText", javascript)
        self.assertIn("participant_key: destination.participant_key", javascript)
        self.assertIn("destination: destination.kind", javascript)

    def test_handoff_endpoint_passes_only_source_authority_to_service(self) -> None:
        expected = {
            "handoff_message_id": 31,
            "source_message_id": 25,
            "status": "completed",
            "turn_id": 12,
        }
        operation = AsyncMock(return_value=expected)
        with patch("app.main.run_manual_handoff", operation):
            response = asyncio.run(main.post_handoff(main.HandoffRequest(
                source_message_id=25
            )))
        self.assertEqual(json.loads(response.body), expected)
        operation.assert_awaited_once_with(
            25,
            database_path=self.database_path,
            openai_client_factory=main.app.state.openai_client_factory,
            gemini_client_factory=main.app.state.gemini_client_factory,
            dotenv_path=main.app.state.dotenv_path,
        )

    def test_post_acceptance_error_response_contains_canonical_ids(self) -> None:
        class FailingResponses:
            async def create(self, **request):
                del request
                raise RuntimeError("simulated provider failure")

        class FailingClient:
            responses = FailingResponses()

            async def close(self):
                return None

        main.app.state.openai_client_factory = lambda api_key: FailingClient()
        environment = {
            "OPENAI_API_KEY": "test-only-key",
            "HELIOS_OPENAI_MODEL": "gpt-5.6-luna",
        }
        with patch.dict(os.environ, environment, clear=True):
            response = asyncio.run(
                main.post_message(main.MessageRequest(
                    message_text="Accepted Peter text",
                    destination={"kind": "participant", "participant_key": "helios"},
                ))
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["error"], "provider_failure")
        self.assertIsInstance(payload["turn_id"], int)
        self.assertIsInstance(payload["peter_message_id"], int)
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "failed")
            self.assertEqual(
                connection.execute("SELECT message_text FROM messages").fetchone()[0],
                "Accepted Peter text",
            )

    def test_cli_ignores_whitespace_only_message(self) -> None:
        arguments = [
            "helios-room",
            "store-message",
            "--database",
            str(self.database_path),
            "--message",
            "\t\r\n",
            "--destination-kind",
            "room",
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
