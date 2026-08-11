# Helios Room

Helios Room is a local, persistent multi-participant chat room for durable
human and AI conversation. The project keeps canonical conversation history
separate from interpretive memory and records provenance so future model,
tool, and memory behavior can be inspected.

## Current status

The project currently provides:

- the unchanged SQLite schema v1.2
- idempotent database initialization and milestone seed data
- persistent Peter messages with deterministic room and turn ordering
- a small FastAPI HTTP API
- a minimal browser chat interface for viewing and sending Peter messages
- database and blank-message regression tests

Whitespace-only messages are ignored by the API, and the browser keeps the
Send button disabled until the input contains non-whitespace text.

OpenAI calls, Helios responses, raw API event recording, slash commands, and
memory retrieval or creation are not implemented yet.

## Requirements

- Python 3.10 or newer
- dependencies from `requirements.txt`

Install the dependencies from the project root:

```powershell
python -m pip install -r requirements.txt
```

## Initialize the database

```powershell
python -m app.main init-db
```

This creates `data/helios.db`, installs schema v1.2 when needed, and ensures
the following records exist:

- room `main` / `The Room`
- human participant `peter` / `Peter`
- AI participant `helios` / `Helios`
- active room membership for both participants
- one minimal OpenAI participant config for Helios labeled `initial`

Initialization is idempotent and preserves existing messages. The database is
local runtime data and is excluded from Git.

The initial Helios configuration intentionally leaves the model and system
instructions unset until the OpenAI integration milestone.

## Run the application

Start the local server:

```powershell
python -m app.main serve
```

Then open <http://127.0.0.1:8000> in a browser. The server binds to
`127.0.0.1:8000` by default; use `--host` and `--port` to override those
values.

The browser displays messages from the `main` room in deterministic room
sequence and can store new messages from Peter. It does not contact the OpenAI
API or generate a Helios response yet.

## Store a Peter message from the command line

```powershell
python -m app.main store-message --message "Hello from Peter"
```

This creates a new open turn and stores the message in the canonical room
history.

## HTTP endpoints

- `GET /` serves the browser interface.
- `GET /api/messages` returns messages from the `main` room.
- `POST /api/messages` stores a message. The participant defaults to Peter.

Example request body:

```json
{
  "message_text": "Hello from Peter"
}
```

## Run tests

```powershell
python -m unittest discover -s tests -v
```

The test suite uses temporary databases and does not modify
`data/helios.db`.

## Project structure

```text
app/       FastAPI entry point and database helpers
data/      Local SQLite database files (ignored by Git)
schema/    Authoritative SQLite schema v1.2
static/    Browser interface
tests/     Automated tests
```
