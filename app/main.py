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
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from .commands import classify_local_command, is_reserved_local_command
from .database import (
    DEFAULT_DATABASE_PATH,
    DatabaseInitializationError,
    MessageVisibilityGuardError,
    initialize_database,
    is_blank_message,
)
from .identity_service import (
    IdentityServiceError,
    load_message_history,
    load_participant_directory,
    post_room_message,
)
from .gemini_client import create_gemini_client
from .gemini_identity import (
    GeminiIdentityError,
    install_gemini,
    publish_gemini_welcome,
)
from .gemini_service import run_gemini_turn
from .migration import DatabaseMigrationError, migrate_database
from .maintenance_lock import (
    MaintenanceLockError,
    ResetRecoveryRequiredError,
    acquire_database_lease,
)
from .models import MessageRequest
from .openai_client import create_openai_client
from .participant_registry import registration_for
from .preflight import DatabasePreflightError, preflight_database
from .room_service import TurnServiceError, run_helios_turn
from .reset_database import (
    DatabaseResetError,
    execute_database_reset,
    plan_database_reset,
    recover_database_reset,
)
from .seed_memory import SeedMemoryError, import_seed_memories, load_seed_manifest
from .trace_service import TraceServiceError, load_trace


_CANONICAL_TURN_ID = re.compile(r"[1-9][0-9]*\Z")
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807

# FastAPI application
app = FastAPI(title="Helios Room")
app.state.database_path = DEFAULT_DATABASE_PATH
app.state.openai_client_factory = create_openai_client
app.state.gemini_client_factory = create_gemini_client
app.state.dotenv_path = None

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_message_request(_request: Request, _error: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_message_request",
            "message": "The message request is invalid.",
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main chat interface."""
    index_path = static_dir / "index.html"
    return HTMLResponse(
        index_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/messages")
async def get_messages():
    """Retrieve all messages from the room."""
    try:
        messages = await asyncio.to_thread(
            load_message_history, app.state.database_path
        )
    except IdentityServiceError as error:
        return _identity_error(error)
    return JSONResponse(content=messages, headers={"Cache-Control": "no-store"})


@app.get("/api/participants")
async def get_participants():
    """Return the read-only, versioned main-room destination directory."""

    try:
        directory = await asyncio.to_thread(
            load_participant_directory, app.state.database_path
        )
    except IdentityServiceError as error:
        return _identity_error(error)
    return JSONResponse(content=directory, headers={"Cache-Control": "no-store"})


@app.post("/api/messages")
async def post_message(request: MessageRequest):
    """Store one explicit Room post or run one explicit Peter/Helios turn."""
    try:
        if request.destination.kind == "room":
            result = await asyncio.to_thread(
                post_room_message, request.message_text, app.state.database_path
            )
        else:
            participant_key = request.destination.participant_key
            registration = registration_for(participant_key)
            if registration is None:
                raise TurnServiceError(
                    status_code=409,
                    code="participant_destination_unavailable",
                    message="The selected participant destination is unavailable.",
                )
            if registration.participant_key == "helios":
                result = await run_helios_turn(
                    request.message_text,
                    destination_participant_key=participant_key,
                    database_path=app.state.database_path,
                    client_factory=app.state.openai_client_factory,
                    dotenv_path=app.state.dotenv_path,
                )
            elif registration.participant_key == "gemini":
                result = await run_gemini_turn(
                    request.message_text,
                    destination_participant_key=participant_key,
                    database_path=app.state.database_path,
                    client_factory=app.state.gemini_client_factory,
                    dotenv_path=app.state.dotenv_path,
                )
            else:  # The static registry is intentionally exhaustive.
                raise TurnServiceError(
                    status_code=409,
                    code="participant_destination_unavailable",
                    message="The selected participant destination is unavailable.",
                )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
    except TurnServiceError as error:
        return JSONResponse(
            status_code=error.status_code,
            content=error.as_payload(),
            headers={"Cache-Control": "no-store"},
        )
    except IdentityServiceError as error:
        return _identity_error(error)


def _identity_error(error: IdentityServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.as_payload(),
        headers={"Cache-Control": "no-store"},
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
        choices=(
            "init-db",
            "migrate-database",
            "store-message",
            "import-seed-memories",
            "install-gemini",
            "publish-gemini-welcome",
            "reset-database",
            "serve",
        ),
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
        "--destination-kind",
        choices=("room", "participant"),
        help="Required Room destination kind for store-message.",
    )
    parser.add_argument(
        "--participant-key",
        help="Unsupported by the Room-only store-message command.",
    )
    parser.add_argument(
        "--file",
        help="UTF-8 JSON manifest path (for import-seed-memories command).",
    )
    parser.add_argument(
        "--owner-participant-key",
        default="helios",
        help="Stable AI owner key for import-seed-memories (default: helios).",
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
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--expected-plan-token")
    parser.add_argument("--expected-backup-path")
    parser.add_argument("--expected-audit-path")
    parser.add_argument("--confirm-destroy-canonical-history", action="store_true")
    parser.add_argument("--action", choices=("restore-source", "complete-fresh"))
    parser.add_argument("--reviewed-plan-manifest")
    parser.add_argument("--confirm-reset-recovery", action="store_true")
    arguments = parser.parse_args()

    if arguments.command == "init-db":
        try:
            report = initialize_database(database_path=arguments.database)
        except DatabaseInitializationError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(asdict(report), indent=2))

    elif arguments.command == "migrate-database":
        try:
            report = migrate_database(arguments.database)
        except DatabaseMigrationError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report.as_dict(), indent=2))

    elif arguments.command == "store-message":
        if arguments.message is None:
            parser.error("--message is required for store-message command")
        if arguments.destination_kind != "room":
            parser.error("store-message requires --destination-kind room")
        if arguments.participant_key is not None:
            parser.error("--participant-key is not supported by store-message")

        if is_blank_message(arguments.message):
            print(json.dumps({"ignored": True, "reason": "empty_message"}, indent=2))
            return

        if is_reserved_local_command(classify_local_command(arguments.message)):
            parser.exit(
                status=2,
                message=(
                    "store-message rejects local commands; use the browser interface.\n"
                ),
            )
        try:
            result = post_room_message(arguments.message, arguments.database)
        except IdentityServiceError as error:
            print(
                json.dumps(error.as_payload(), sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        print(json.dumps(result, indent=2))

    elif arguments.command == "import-seed-memories":
        if arguments.file is None:
            parser.error("--file is required for import-seed-memories command")
        try:
            manifest = load_seed_manifest(arguments.file)
            report = import_seed_memories(
                arguments.database,
                manifest,
                owner_participant_key=arguments.owner_participant_key,
            )
        except SeedMemoryError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif arguments.command == "install-gemini":
        try:
            report = install_gemini(arguments.database)
        except GeminiIdentityError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif arguments.command == "publish-gemini-welcome":
        try:
            report = publish_gemini_welcome(arguments.database)
        except MessageVisibilityGuardError as error:
            print(json.dumps(error.as_payload(), sort_keys=True, separators=(",", ":")), file=sys.stderr)
            raise SystemExit(1) from None
        except GeminiIdentityError as error:
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif arguments.command == "reset-database":
        modes = sum((arguments.plan, arguments.execute, arguments.recover))
        if modes != 1:
            error = DatabaseResetError(
                "reset_confirmation_required",
                "Reset execution requires the exact reviewed plan and explicit confirmation.",
                exit_code=2,
            )
            print(json.dumps(error.as_payload(), sort_keys=True, separators=(",", ":")), file=sys.stderr)
            raise SystemExit(error.exit_code) from None
        try:
            if arguments.plan:
                report = plan_database_reset(arguments.database)
            elif arguments.execute:
                report = execute_database_reset(
                    arguments.database,
                    expected_plan_token=arguments.expected_plan_token,
                    expected_backup_path=arguments.expected_backup_path,
                    expected_audit_path=arguments.expected_audit_path,
                    confirm_destroy_canonical_history=arguments.confirm_destroy_canonical_history,
                )
            else:
                report = recover_database_reset(
                    arguments.database,
                    expected_plan_token=arguments.expected_plan_token,
                    action=arguments.action,
                    reviewed_plan_manifest=arguments.reviewed_plan_manifest,
                    confirm_reset_recovery=arguments.confirm_reset_recovery,
                )
        except DatabaseResetError as error:
            print(json.dumps(error.as_payload(), sort_keys=True, separators=(",", ":")), file=sys.stderr)
            raise SystemExit(error.exit_code) from None
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    elif arguments.command == "serve":
        server_lease = None
        try:
            if Path(arguments.database).is_file():
                server_lease = acquire_database_lease(arguments.database, shared=True)
            preflight_database(arguments.database)
        except ResetRecoveryRequiredError as error:
            if server_lease is not None:
                server_lease.close()
            print(json.dumps({
                "error": error.code,
                "message": error.message,
            }, sort_keys=True, separators=(",", ":")), file=sys.stderr)
            raise SystemExit(1) from None
        except MaintenanceLockError:
            if server_lease is not None:
                server_lease.close()
            print(json.dumps({
                "error": "database_maintenance_in_progress",
                "message": "The Helios Room database is unavailable during maintenance.",
            }, sort_keys=True, separators=(",", ":")), file=sys.stderr)
            raise SystemExit(1) from None
        except DatabasePreflightError as error:
            if server_lease is not None:
                server_lease.close()
            print(json.dumps(error.as_payload(), sort_keys=True), file=sys.stderr)
            raise SystemExit(1) from None
        app.state.database_path = Path(arguments.database).expanduser().resolve()
        print(f"Starting Helios Room web server on http://{arguments.host}:{arguments.port}")
        try:
            uvicorn.run(app, host=arguments.host, port=arguments.port)
        finally:
            if server_lease is not None:
                server_lease.close()


if __name__ == "__main__":
    main()

