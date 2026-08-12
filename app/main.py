"""Command-line entry point and FastAPI application for Helios Room."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from .commands import classify_local_command, is_reserved_trace_command
from .database import (
    DEFAULT_DATABASE_PATH,
    connect_database,
    create_turn,
    initialize_database,
    is_blank_message,
    store_message,
)
from .models import MessageRequest
from .openai_client import create_openai_client
from .room_service import TurnServiceError, run_helios_turn
from .seed_memory import SeedMemoryError, import_seed_memories, load_seed_manifest
from .trace_service import TraceServiceError, load_trace


_CANONICAL_TURN_ID = re.compile(r"[1-9][0-9]*\Z")
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807

# FastAPI application
app = FastAPI(title="Helios Room")
app.state.database_path = DEFAULT_DATABASE_PATH
app.state.openai_client_factory = create_openai_client
app.state.dotenv_path = None

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main chat interface."""
    index_path = static_dir / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/messages")
async def get_messages():
    """Retrieve all messages from the room."""
    return await asyncio.to_thread(_load_messages, app.state.database_path)


def _load_messages(database_path: Path | str) -> list[dict[str, object]]:
    connection = connect_database(database_path)
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
        
        return [
            {
                "id": row["id"],
                "message_text": row["message_text"],
                "created_at": row["created_at"],
                "participant_key": row["participant_key"],
                "room_sequence_no": row["room_sequence_no"],
            }
            for row in rows
        ]
    finally:
        connection.close()


@app.post("/api/messages")
async def post_message(request: MessageRequest):
    """Run one browser-authored Peter/Helios turn."""
    try:
        return await run_helios_turn(
            request.message_text,
            database_path=app.state.database_path,
            client_factory=app.state.openai_client_factory,
            dotenv_path=app.state.dotenv_path,
        )
    except TurnServiceError as error:
        return JSONResponse(
            status_code=error.status_code,
            content=error.as_payload(),
        )


@app.get("/api/trace/latest")
async def get_latest_trace(request: Request):
    """Return the latest main-room turn through the read-only trace reader."""

    return await _trace_response(request, None)


@app.get("/api/trace/{turn_id}")
async def get_trace(request: Request, turn_id: str):
    """Return a specific canonical decimal turn ID without path coercion."""

    if _CANONICAL_TURN_ID.fullmatch(turn_id) is None:
        return _trace_error(
            TraceServiceError(
                400,
                "invalid_trace_turn_id",
                "Trace turn IDs must be canonical positive decimal integers.",
            )
        )
    parsed_turn_id = int(turn_id)
    if parsed_turn_id > _SQLITE_MAX_INTEGER:
        return _trace_error(
            TraceServiceError(
                400,
                "invalid_trace_turn_id",
                "Trace turn IDs must fit SQLite's signed 64-bit integer range.",
            )
        )
    return await _trace_response(request, parsed_turn_id)


async def _trace_response(request: Request, turn_id: int | None) -> JSONResponse:
    try:
        trace = await asyncio.to_thread(
            load_trace,
            request.app.state.database_path,
            turn_id,
        )
    except TraceServiceError as error:
        return _trace_error(error)
    except Exception:
        return _trace_error(
            TraceServiceError(
                500,
                "trace_data_invalid",
                "The recorded trace data is invalid.",
            )
        )
    return JSONResponse(content=trace, headers={"Cache-Control": "no-store"})


def _trace_error(error: TraceServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.as_payload(),
        headers={"Cache-Control": "no-store"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Helios Room")
    parser.add_argument(
        "command",
        choices=("init-db", "store-message", "import-seed-memories", "serve"),
        help=(
            "Initialize the database, store a test message, import validated seed "
            "memories, or start the web server."
        ),
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
        "--file",
        help="UTF-8 JSON manifest path (for import-seed-memories command).",
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

        if is_reserved_trace_command(classify_local_command(arguments.message)):
            parser.exit(
                status=2,
                message=(
                    "store-message rejects /trace commands; use the local browser UI.\n"
                ),
            )

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

    elif arguments.command == "import-seed-memories":
        if arguments.file is None:
            parser.error("--file is required for import-seed-memories command")
        try:
            manifest = load_seed_manifest(arguments.file)
            report = import_seed_memories(arguments.database, manifest)
        except SeedMemoryError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif arguments.command == "serve":
        app.state.database_path = Path(arguments.database).expanduser().resolve()
        print(f"Starting Helios Room web server on http://{arguments.host}:{arguments.port}")
        uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()

