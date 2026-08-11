"""Command-line entry point and FastAPI application for Helios Room."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from .database import (
    DEFAULT_DATABASE_PATH,
    connect_database,
    create_turn,
    initialize_database,
    is_blank_message,
    store_message,
)

# FastAPI application
app = FastAPI(title="Helios Room")

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class MessageRequest(BaseModel):
    message_text: str
    participant_key: str = "peter"


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main chat interface."""
    index_path = static_dir / "index.html"
    return HTMLResponse(index_path.read_text())


@app.get("/api/messages")
async def get_messages():
    """Retrieve all messages from the room."""
    connection = connect_database()
    try:
        rows = connection.execute(
            """
            SELECT 
                m.id,
                m.message_text,
                m.created_at,
                p.participant_key,
                m.room_sequence_no
            FROM messages m
            JOIN participants p ON m.participant_id = p.id
            WHERE m.room_id = (SELECT id FROM rooms WHERE room_key = 'main')
            ORDER BY m.room_sequence_no
            """
        ).fetchall()
        
        messages = [
            {
                "id": row["id"],
                "message_text": row["message_text"],
                "created_at": row["created_at"],
                "participant_key": row["participant_key"],
                "room_sequence_no": row["room_sequence_no"],
            }
            for row in rows
        ]
        return messages
    finally:
        connection.close()


@app.post("/api/messages")
async def post_message(request: MessageRequest):
    """Store a new message in the room."""
    if is_blank_message(request.message_text):
        return {"ignored": True, "reason": "empty_message"}

    connection = connect_database()
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
            (request.participant_key,),
        ).fetchone()
        if not participant_row:
            raise RuntimeError(f"Participant '{request.participant_key}' not found")
        participant_id = participant_row["id"]

        # Create a turn and store the message
        turn_id = create_turn(connection, room_id, participant_id)
        message_id = store_message(
            connection,
            room_id=room_id,
            participant_id=participant_id,
            message_text=request.message_text,
            turn_id=turn_id,
        )

        connection.commit()

        return {
            "turn_id": turn_id,
            "message_id": message_id,
            "room_id": room_id,
            "participant_id": participant_id,
            "message": request.message_text,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Helios Room")
    parser.add_argument(
        "command",
        choices=("init-db", "store-message", "serve"),
        help="Initialize database, store a test message, or start the web server.",
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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the web server to (for serve command).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the web server to (for serve command).",
    )
    arguments = parser.parse_args()

    if arguments.command == "init-db":
        report = initialize_database(database_path=arguments.database)
        print(json.dumps(asdict(report), indent=2))

    elif arguments.command == "store-message":
        if arguments.message is None:
            parser.error("--message is required for store-message command")

        if is_blank_message(arguments.message):
            print(json.dumps({"ignored": True, "reason": "empty_message"}, indent=2))
            return

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

    elif arguments.command == "serve":
        print(f"Starting Helios Room web server on http://{arguments.host}:{arguments.port}")
        uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()

