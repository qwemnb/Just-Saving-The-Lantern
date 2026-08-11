"""Command-line entry point for the first Helios Room milestone.

FastAPI routes will be added in the API-integration milestone. For now this
module exposes only database initialization so the first vertical slice stays
small and testable.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .database import (
    DEFAULT_DATABASE_PATH,
    connect_database,
    create_turn,
    initialize_database,
    store_message,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Helios Room")
    parser.add_argument(
        "command",
        choices=("init-db", "store-message"),
        help="Initialize and verify the local SQLite database, or store a test message.",
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DATABASE_PATH,
        help="Optional SQLite database path.",
    )
    parser.add_argument(
        "--message",
        help="Message text to store (for store-message command).",
    )
    arguments = parser.parse_args()

    if arguments.command == "init-db":
        report = initialize_database(database_path=arguments.database)
        print(json.dumps(asdict(report), indent=2))

    elif arguments.command == "store-message":
        if not arguments.message:
            parser.error("--message is required for store-message command")

        connection = connect_database(database_path=arguments.database)
        try:
            connection.execute("BEGIN IMMEDIATE")

            # Get room and participant IDs
            room_row = connection.execute(
                "SELECT id FROM rooms WHERE room_key = ?",
                ("main",),
            ).fetchone()
            if not room_row:
                raise RuntimeError("Room 'main' not found")
            room_id = room_row["id"]

            participant_row = connection.execute(
                "SELECT id FROM participants WHERE participant_key = ?",
                ("peter",),
            ).fetchone()
            if not participant_row:
                raise RuntimeError("Participant 'peter' not found")
            participant_id = participant_row["id"]

            # Create a turn and store the message
            turn_id = create_turn(connection, room_id, participant_id)
            message_id = store_message(
                connection,
                room_id=room_id,
                participant_id=participant_id,
                message_text=arguments.message,
                turn_id=turn_id,
            )

            connection.commit()

            result = {
                "turn_id": turn_id,
                "message_id": message_id,
                "room_id": room_id,
                "participant_id": participant_id,
                "message": arguments.message,
            }
            print(json.dumps(result, indent=2))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


if __name__ == "__main__":
    main()

