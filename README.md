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
- read-only, historical Trace v1 inspection for recorded turns
- strict, atomic seeded-memory manifest import with semantic idempotency
- deterministic local seeded-memory retrieval with recorded provenance
- a FastAPI HTTP API and minimal browser chat interface
- offline database, orchestration, API, and blank-message regression tests

Whitespace-only messages are ignored by the API and command-line entry point.
The storage helper also rejects them defensively, and the browser keeps the
Send button disabled until the input contains non-whitespace text.

The current provider flow replays canonical Peter and Helios `chat` text and
may insert one inherited-memory context item selected from Helios-owned seeded
memories immediately before Peter's triggering message. It does not replay
provider reasoning state or use tools, room-created memory, streaming,
provider-managed conversations, or automatic retries.

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

In a newly initialized database, the seeded `initial` Helios configuration is
an unused placeholder. The first accepted API-backed turn creates a separate
immutable configuration with the requested model, exact system instructions,
settings, and empty tool list.

## Run the application

Start the local server:

```powershell
python -m app.main serve
```

To use a different existing database for both chat and trace routes, select it
when starting the server:

```powershell
python -m app.main serve --database C:\path\to\helios.db
```

The selected path is not opened merely by parsing the `serve` command. Normal
chat operations still require an initialized compatible database, while trace
access refuses to create a missing file.

Then open <http://127.0.0.1:8000> in a browser. The server binds to
`127.0.0.1:8000` by default; use `--host` and `--port` to override those
values.

The browser displays messages from the `main` room in deterministic room
sequence. A nonblank submission is authored as Peter on the server, committed,
and followed by exactly one Helios Responses API call. While that call is in
progress, the input and Send button are disabled and a temporary local status
is shown.

The current provider settings are:

- `store=False`
- `reasoning={"effort": "medium", "context": "current_turn"}`
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

## Inspect a recorded turn with Trace v1

Enter either local command in the browser message box:

```text
/trace
/trace 17
```

`/trace` opens the latest turn in the `main` room. `/trace <turn_id>` opens the
specified positive decimal turn ID. These commands are intercepted locally:
they are not Peter messages, create no turn, message, API event, or admin event,
and never reach OpenAI. Malformed `/trace` usage is also rejected before any
canonical write.

The accessible trace panel shows the selected turn and room identity, exact
canonical messages and reply provenance, every referenced participant
configuration, the recorded provider request, the recorded provider outcome,
the ordered API-event timeline, redaction metadata, and recorded memory context
when one exists. Human-only, open, cancelled, failed, and stranded turns are
shown as recorded; a missing request or outcome is not inferred or fabricated.

Trace data comes from one read-only SQLite snapshot. It never reruns a provider
request, retries an old turn, reconstructs history using the current model, or
queries current memory tables. No inherited memory retrieval is shown unless
the historical request event explicitly recorded one.

For a memory-enabled turn, `/trace <turn_id>` includes a dedicated **Inherited
memory** section with the recorded retriever version, query terms, limits,
ranking evidence, immutable seed and batch IDs, hashes, source provenance, and
the exact memory text supplied. It distinguishes a recorded zero-result search,
an older turn where retrieval was not recorded, and a deliberately unavailable
request. Trace validates the audit envelope against the recorded provider input
and fails closed if they disagree.

Trace applies a display-only privacy projection without changing stored JSON.
Secret-like fields and raw or encrypted provider reasoning are omitted. An
explicitly recorded provider-generated reasoning summary may be displayed and
is labelled as a summary, while usage totals can still include aggregate
reasoning-token counts. Omission locations are reported as JSON Pointers.

Trace exposes private canonical messages, exact system instructions, request
settings, and operational provenance. Keep the server bound to a trusted local
interface; Trace v1 is not designed as a public or multi-user diagnostics API.

## Memory status and boundaries

Helios Room keeps three concepts separate:

- **Canonical history** is the immutable Peter and Helios message record in the
  room. It remains the only record of events that occurred in the room.
- **Seeded memory** is intended for curated continuity imported from
  conversations that occurred before Helios Room existed. Selected records are
  reference data, not room events, Peter messages, or instructions.
- **Room-created memory** is reserved for future interpretive records produced
  from activity inside the room.

Seeded Memory Retrieval v1 searches only active, nonsuperseded seed records
owned by Helios. `room_memories` is not queried. There is no automatic memory
creation, editing, deactivation, supersession, or management UI.

Real seed manifests, database backups, and other private runtime material must
remain under the ignored `data/` directory and must never be committed. Any
real import or live memory-backed smoke test requires a reviewed manifest, a
consistent database backup, and separate authorization.

### Back up the live room before a real import

Stop the server first. Because the database uses WAL mode, do not copy only
`helios.db`; committed state may still be in its WAL. From Command Prompt, use
SQLite's online backup API to create a self-contained destination that does not
already exist and verify it immediately:

```cmd
if not exist data\backups mkdir data\backups
python -c "from pathlib import Path; import sqlite3; source_path=Path('data/helios.db'); backup_path=Path('data/backups/helios-before-seeded-memory-v1.db'); assert source_path.is_file(), 'source database is missing'; assert not backup_path.exists(), 'backup destination already exists'; source=sqlite3.connect('file:data/helios.db?mode=ro', uri=True); backup=sqlite3.connect(backup_path); source.backup(backup); rows=backup.execute('PRAGMA integrity_check').fetchall(); assert rows == [('ok',)], rows; backup.close(); source.close(); print('Backup created and integrity_check passed:', backup_path)"
```

Creating this backup is a manual prerequisite, not permission to import or
make an OpenAI request.

### Seed manifest and import

The fictional [example manifest](examples/seed-memory-manifest-v1.example.json)
documents format version 1. A manifest contains batch provenance and one or
more immutable memory records with stable IDs, scores, root topics, and
optional room-participant subject links. The importer strictly validates UTF-8
JSON, rejects duplicate or unknown fields, preserves memory text exactly, and
forces Helios ownership plus active, nonsuperseded initial state.

Place a reviewed real manifest under ignored runtime data, then import it only
after explicit authorization:

```powershell
python -m app.main import-seed-memories --file data\imports\helios_seed_memories_v1.json
```

Use `--database` to select another initialized schema-v1.2 database. The
importer hashes a canonical typed representation, runs every database check and
write under one `BEGIN IMMEDIATE` transaction, and returns JSON without echoing
memory text. A semantically identical import returns `already_imported` with no
writes. Hash ambiguity, stored-graph drift, stable-ID reuse, topic conflict, or
an unknown participant fails the whole import without partial rows or sequence
changes. Imports never create turns, messages, or API events.

### Retrieval and provider representation

For each accepted Peter message, `seed-fts-topic-v1` creates an NFKC-normalized,
case-folded query from that new message only. It uses safely quoted FTS5 terms
plus exact normalized topic-key/name matches, then ranks deterministically by
topic match, summed topic weight, ascending BM25 score, importance, confidence,
and seed-memory ID. It selects at most five whole records within an aggregate
8,000-Unicode-code-point text budget; records are never truncated.

If records are selected, one `user` input item beginning with
`INHERITED_MEMORY_CONTEXT` and canonical JSON is placed after earlier canonical
chat history and immediately before Peter's triggering canonical message. The
system instructions require Helios to use relevant inherited records, treat an
earlier assistant claim of ignorance as a historical utterance rather than an
override, and acknowledge remembered material as inherited continuity. Memory
text remains untrusted reference data: it is not a room event, a message from
Peter, or an instruction. Peter's accepted message remains the final input
item. No match adds no context item, but the request event still records that
retrieval ran and selected nothing. Retrieval failure rolls Phase A back and
prevents the provider call.

The [live-acceptance amendment](docs/seeded-memory-retrieval-v1-amendment.md)
records the exact revised instructions, positional contract, immutable
configuration behavior, and acceptance criteria. Exact synthetic Luna-medium
and Terra-medium review requests are in the
[offline evaluation fixture](examples/seed-memory-model-evaluation-v1.json);
the fixture is never submitted automatically.

## HTTP endpoints

- `GET /` serves the browser interface.
- `GET /api/messages` returns messages from the `main` room.
- `POST /api/messages` accepts message text, assigns Peter server-side, and
  performs one API-backed Helios turn. Extra request fields are rejected.
- `GET /api/trace/latest` returns the latest recorded `main`-room turn through
  the read-only Trace v1 projection.
- `GET /api/trace/{turn_id}` returns one recorded turn for a canonical positive
  decimal SQLite turn ID.

Both trace endpoints return JSON with `Cache-Control: no-store`. Missing or
incompatible databases, invalid identifiers, missing turns, and invalid
historical data use stable error codes and do not fall back to chat or provider
behavior.

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
node --test tests/test_trace_ui.js
node --check static/app.js
```

## Live-provider safety

Opening the application and using `/trace` do not contact OpenAI. Submitting an
ordinary nonblank message does create canonical history and can make one
billable Responses API request. Do not perform a live-provider smoke test until
Peter explicitly authorizes it and confirms the intended database and model.

Automated tests use temporary databases and mocked providers. They are the
default verification path for changes to the application.

## Intentional seeded-memory acceptance test

Do not perform this test without separate authorization. After reviewing the
real curated manifest and creating the WAL-safe backup above:

1. Import the reviewed manifest with `import-seed-memories`.
2. Start Helios Room with `gpt-5.6-luna`.
3. Submit the single approved continuity question.
4. Confirm Helios distinguishes inherited continuity from room history.
5. Enter `/trace <turn_id>` and inspect the exact inherited selection.
6. Confirm only the intended active record was supplied and only one billable
   Responses request occurred.

## Known stranded-turn limitation

Peter's message and the outbound request event are committed before OpenAI is
contacted. A process crash, cancellation, database lock, disk failure, or other
finalization failure can therefore leave an open turn after the provider call
begins. Helios Room never automatically retries or resends that turn because
the provider may already have produced a billable result. Manual reconciliation
is required in this milestone.

## Project structure

```text
app/       FastAPI entry point, database, trace, and seeded-memory helpers
data/      Local SQLite database files (ignored by Git)
docs/      Approved implementation-contract amendments
examples/  Synthetic, nonpersonal documentation fixtures
schema/    Authoritative SQLite schema v1.2
static/    Browser interface
tests/     Automated tests
```
