# Helios Room

Helios Room is a local, persistent multi-participant chat room for durable
human and AI conversation. The project keeps canonical conversation history
separate from interpretive memory and records provenance so future model,
tool, and memory behavior can be inspected.

## Current status

The project currently provides:

- the unchanged SQLite schema v1.2
- idempotent database initialization and milestone seed data
- persistent Peter and Helios messages with deterministic room and turn ordering
- one-request, no-retry OpenAI Responses API orchestration
- raw request/response/error API events with immutable model provenance
- a FastAPI HTTP API and minimal browser chat interface
- offline database, orchestration, API, and blank-message regression tests

Whitespace-only messages are ignored by the API and command-line entry point.
The storage helper also rejects them defensively, and the browser keeps the
Send button disabled until the input contains non-whitespace text.

The first API milestone deliberately replays only canonical Peter and Helios
`chat` text. It does not replay provider reasoning state or use tools, memory,
streaming, provider-managed conversations, or automatic retries.

## Requirements

- Python 3.10 or newer
- dependencies from `requirements.txt`

Install the dependencies from the project root:

```powershell
python -m pip install -r requirements.txt
```

The OpenAI SDK and `python-dotenv` are direct, pinned dependencies.

## Configure OpenAI locally

Copy `.env.example` to an untracked `.env` file and set the API key:

```text
OPENAI_API_KEY=your-local-key
HELIOS_OPENAI_MODEL=gpt-5.6-luna
```

The application calls `load_dotenv(override=False)` before reading these
values. Existing process environment variables therefore take precedence over
the local `.env`. The API key remains server-side and is never included in
canonical messages or API event payloads.

Initial testing uses the explicit `gpt-5.6-luna` model ID. To move later to
the higher-capability Sol model, change only the model value and restart:

```text
HELIOS_OPENAI_MODEL=gpt-5.6-sol
```

Each distinct request configuration receives or reuses an immutable Helios
participant configuration. Changing models never rewrites earlier provenance.

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

The seeded `initial` Helios configuration remains an unused placeholder. The
first accepted API-backed turn creates a separate immutable configuration with
the requested model, exact system instructions, settings, and empty tool list.

## Run the application

Start the local server:

```powershell
python -m app.main serve
```

Then open <http://127.0.0.1:8000> in a browser. The server binds to
`127.0.0.1:8000` by default; use `--host` and `--port` to override those
values.

The browser displays messages from the `main` room in deterministic room
sequence. A nonblank submission is authored as Peter on the server, committed,
and followed by exactly one Helios Responses API call. While that call is in
progress, the input and Send button are disabled and a temporary local status
is shown.

The first-turn provider settings are:

- `store=False`
- `reasoning={"effort": "low", "context": "current_turn"}`
- `max_output_tokens=2048`
- no tools
- 120-second client timeout
- zero SDK or application retries

On a completed response with nonblank `output_text`, that text is stored
exactly as Helios's immutable canonical reply. A provider exception, timeout,
rejection, incomplete response, structured refusal without visible text, or
blank result fails the turn without inventing a Helios message. Peter's already
accepted message remains canonical and visible.

## Store a Peter message from the command line

```powershell
python -m app.main store-message --message "Hello from Peter"
```

This creates a new open turn and stores the message in the canonical room
history.

## HTTP endpoints

- `GET /` serves the browser interface.
- `GET /api/messages` returns messages from the `main` room.
- `POST /api/messages` accepts message text, assigns Peter server-side, and
  performs the first API-backed Helios turn. Extra request fields are rejected.

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
`data/helios.db`. Provider clients are injected or mocked; automated tests make
no network calls and no billable OpenAI requests.

Also run the complete offline verification set:

```powershell
python -m compileall app tests
git diff --check
```

## Intentional live smoke test

Do not perform this test until Peter explicitly authorizes a live, billable
OpenAI request. The current development database contains two Peter test
messages in open turns, and both will be included in the first canonical replay
unless Peter separately authorizes a database reset.

After authorization:

1. Put a valid key and `gpt-5.6-luna` in `.env` as shown above.
2. Start the server with `python -m app.main serve`.
3. Open <http://127.0.0.1:8000>.
4. Submit one nonblank Peter message once.
5. Confirm Peter and Helios appear in canonical order, then stop the server.

## Known stranded-turn limitation

Peter's message and the outbound request event are committed before OpenAI is
contacted. A process crash, cancellation, database lock, disk failure, or other
finalization failure can therefore leave an open turn after the provider call
begins. Helios Room never automatically retries or resends that turn because
the provider may already have produced a billable result. Manual reconciliation
is required in this milestone.

## Project structure

```text
app/       FastAPI entry point and database helpers
data/      Local SQLite database files (ignored by Git)
schema/    Authoritative SQLite schema v1.2
static/    Browser interface
tests/     Automated tests
```
