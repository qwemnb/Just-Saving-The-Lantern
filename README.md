# Helios Room

Helios Room is a local, persistent multi-participant chat room for durable
human and AI conversation. The project keeps canonical conversation history
separate from interpretive memory and records provenance so future model,
tool, and memory behavior can be inspected.

## Current status

The project currently provides:

- fresh-install SQLite schema v1.4 with immutable room-wide history policy
- idempotent database initialization and milestone seed data
- persistent Peter, Helios, Gemini, and Room-directed messages with deterministic ordering
- isolated, one-request, no-retry OpenAI Responses and Google Generate Content orchestration
- raw request/response/error API events with immutable model provenance
- read-only, historical Trace v3 inspection with immutable route and visibility display
- strict, atomic seeded-memory manifest import with semantic idempotency
- deterministic local seeded-memory retrieval with recorded provenance
- a read-only participant directory and structured participant/Room destinations
- a browser participant panel, destination picker, and local participant command
- a manual one-click AI-to-AI handoff control with one provider call per click
- room-wide canonical provider history and participant-owned inherited-memory projections
- offline database, orchestration, API, browser-privacy, and blank-message regression tests

Whitespace-only messages are ignored by the API and command-line entry point.
The storage helper also rejects them defensively, and the browser keeps the
Send button disabled until the input contains non-whitespace text.

Every selected provider receives every valid canonical chat message in room
sequence order. Addressing determines who is spoken to and which single
provider may be triggered; it is not a privacy boundary. Messages outside a
provider's native Peter dialogue use an exact attributed external envelope.
Each provider can receive one inherited-memory
context selected only from records it owns, immediately before Peter's
triggering message. Room posts never invoke an AI automatically. An eligible
terminal AI-to-AI message may be advanced only by Peter pressing the global
Push button; each press authorizes exactly one provider response and never
cascades.

| Canonical route | Helios input | Gemini input |
| --- | --- | --- |
| Peter -> Room | include | include |
| Peter -> Helios / Helios -> Peter | native | external envelope |
| Peter -> Gemini / Gemini -> Peter | external envelope | native |
| Helios -> Gemini welcome | external envelope | external envelope |
| Valid system/name event -> Room | validate, then omit | validate, then omit |
| Other valid direct exchange | external envelope | external envelope |

## Requirements

- Python 3.10 or newer
- dependencies from `requirements.txt`

Node is not a runtime dependency. It is optional developer-only tooling for
the standalone JavaScript test file; database initialization, migration,
server startup, and the browser UI do not require Node.

Install the dependencies from the project root:

```powershell
python -m pip install -r requirements.txt
```

The OpenAI SDK, `google-genai==2.18.0`, FastAPI, Starlette, and
`python-dotenv` are direct pinned dependencies. Compatible AnyIO and HTTPX
ranges are recorded explicitly in `requirements.txt`.

## Configure providers locally

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

Gemini uses separate environment variables and ignores `GOOGLE_API_KEY` as
application configuration:

```text
GEMINI_API_KEY=your-private-local-key
HELIOS_GEMINI_MODEL=gemini-3.6-flash
```

The Google client is forced onto the Developer API with `v1beta`, a 120-second
timeout, one total attempt, no tools, one text candidate, 2,048 output tokens,
medium thinking with thoughts excluded, provider-default safety, and SDK
automatic function calling explicitly disabled. Hostile ambient Vertex,
Enterprise, project, location, and alternate-key variables cannot select its
backend or credential. Changing the model creates or reuses a new immutable
Gemini configuration; it never rewrites earlier provenance. Provider keys
stay server-side and are never stored in messages, events, Trace, or browser
responses.

## Initialize the database

```powershell
python -m app.main init-db
```

This creates `data/helios.db` only when missing or object-free, installs schema
v1.4, and ensures
the following records exist:

- room `main` / `The Room`
- human participant `peter` / `Peter`
- AI participant `helios` / `Helios`
- AI participant `gemini` / `Gemini`
- hidden system participant `room-system` with immutable alias `Room`
- one immutable `room_shared_v1` event effective from room sequence 1
- active room membership for Peter, Helios, and Gemini
- one minimal OpenAI participant config for Helios labeled `initial`

The fresh foundation contains no messages, turns, provider events, Gemini
configuration, or imported memory rows. Existing exact schema 1.2 or 1.3
databases are never migrated in place; `init-db`, `migrate-database`, and
server preflight return `database_reset_required`. Retiring one requires the
separately authorized, reviewed reset protocol below.

Initialization is idempotent for an exact schema 1.4 database and preserves
its canonical history. `migrate-database` now performs closed dispatch only:
it returns `already_current` for exact schema 1.4 and never upgrades an older
database. Local runtime databases are excluded from Git.

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

Before binding a socket or serving static files, `serve` performs a read-only
schema preflight against one consistent SQLite snapshot. It accepts only exact
schema history through 1.4, the complete required schema objects, the mature
identity and alias foundation, current-primary/name-event lineage, one route
per canonical message, and successful integrity and foreign-key checks.
Startup never initializes, migrates, checkpoints, repairs, or creates the
selected database. A missing path, an exact old v1.2/v1.3 database, and an
incompatible database return these sanitized errors respectively:

```text
database_not_initialized
database_reset_required
database_schema_incompatible
```

Every application database open participates in the shared maintenance lock
`data/.helios-room-database.lock`. Reset planning, execution, and recovery use
one exclusive lease; normal startup and connections fail closed while it is
held or while any legacy, immutable-generation, evidence-record, or partial
reset-state control exists. Presence detection is platform-independent and
malformed reserved-prefix names also block before SQLite opens.

## Controlled fresh-database reset

Reset planning, execution, and recovery are Windows-only; unsupported systems
fail before repository, lock, reset-artifact, or SQLite access. Implementation
and tests do not authorize a live plan or reset. After the
implementation is audited, committed, and pushed, a separate instruction is
required to generate the filesystem-only plan:

```powershell
python -m app.main reset-database --plan --database data/helios.db
```

Planning opens no SQLite connection and changes no database or sidecar. Its
token binds the complete returned `reviewed_plan_manifest`, including Windows
durability evidence, immutable-generation grammar, paths, filesystem
identities, Git state, and recovery mapping. Save the exact canonical plan
output outside the repository if interrupted-first-generation recovery may be
needed. A second explicit authorization naming that exact plan is required
before execution:

```powershell
python -m app.main reset-database --execute --database data/helios.db `
  --expected-plan-token <reviewed-token> `
  --expected-backup-path <reviewed-backup-path> `
  --expected-audit-path <reviewed-audit-path> `
  --confirm-destroy-canonical-history
```

Execution accepts only an exact valid schema 1.2 or 1.3 source, retains a
verified backup and closed audit artifact under `backups/`, and installs a new
schema 1.4 database with no retired rows. Protected mutations are preceded by
content-addressed, predecessor-bound immutable generations; a terminal audit
binds the complete generation chain. Approved legacy v1 journals are converted
through a content-addressed durability-evidence record without rewriting their
historical bytes. If any reset control remains after interruption, ordinary
startup stays closed. After reviewing it, authorize one exact recovery action
separately:

```powershell
python -m app.main reset-database --recover --database data/helios.db `
  --expected-plan-token <reviewed-token> `
  --action <restore-source|complete-fresh> `
  [--reviewed-plan-manifest C:\absolute\path\to\saved-plan.json] `
  --confirm-reset-recovery
```

`--reviewed-plan-manifest` is required only to prove safe cleanup of an
incomplete native generation 1. It is optional for a completed native chain
and forbidden for legacy conversion state.

Do not bootstrap memory, publish the welcome, start live acceptance, or call a
provider until reset success has been reviewed and each later operation is
separately authorized.

The accepted result applies to the snapshot checked during preflight. A later
process can still replace or modify the database, so participant directory,
message history, Trace, Room-post, and Helios-turn services validate their own
current snapshots and fail closed instead of trusting stale startup state.

Read-only startup, participant-directory, message-history, and Trace snapshots
use this exact WAL-sidecar matrix:

- WAL and SHM both present: ordinary SQLite `mode=ro`; the main file and WAL
  remain byte-for-byte unchanged, while SQLite may update only its own lock or
  read-mark state in the existing SHM.
- WAL present without SHM, or SHM present without WAL: fail closed before
  opening SQLite.
- Neither sidecar present on a clean, checkpointed WAL-mode database: use the
  narrow `mode=ro&immutable=1` path and require the same strong main-file
  fingerprint before and after the read.

That fingerprint includes the canonical resolved path, filesystem identity
where the operating system exposes it, byte size, nanosecond modification
time, and SHA-256. The immutable result is discarded if the main file changes
or a sidecar appears. If both sidecars appear during a runtime read, the stale
immutable result may be discarded and the new WAL snapshot validated once;
one-sided states are never retried. Absent sidecars remain absent after a clean
read-only snapshot.

Then open <http://127.0.0.1:8000> in a browser. The server binds to
`127.0.0.1:8000` by default; use `--host` and `--port` to override those
values.

The browser loads its participant panel and destination choices only from the
read-only directory endpoint. Select Helios or Gemini for the corresponding
one-request AI flow, or select Room to save a canonical Peter note without
loading provider configuration, searching memory, constructing a client, or creating an API
event. Press `[` in an otherwise blank composer to open the searchable picker;
historical aliases are lookup terms but the current primary name is displayed.
The selected destination persists after a successful send.

Permanent identity is the internal participant ID plus the stable application
`participant_key`. A display alias is only a label. Every participant has one
current primary alias and append-only historical aliases. Alias comparison is
global, NFKC-normalized, and case-folded; adopted names never change participant
IDs/keys or rewrite old messages. Each message stores immutable sender and
destination alias snapshots, so earlier history retains the names used then.
The current-primary projection must always equal the latest append-only name
event. Every participant begins with one bootstrap event; later adopted events
form an unbroken previous/new alias chain and correlate to the exact canonical
Room notice and immutable Room route. Directly changing a primary projection
without a matching adopted event makes the foundation invalid. Returning to a
historical or bootstrap alias is valid only through a new adopted event.

Foundation validation deliberately permits mature data: additional valid
rooms, participants, active or historical memberships, multiple immutable
configurations, and legitimate completed, open, failed, cancelled, human-only,
Room-only, or stranded turns. Only the unique `main` room receives this
milestone's required Peter/Helios membership checks. Foundation validation is
layered beneath bounded directory, history, and selected-turn Trace checks; it
does not globally require every historical turn to contain a provider event,
AI response, memory retrieval, or currently valid Trace projection.

The current OpenAI provider settings are:

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

Gemini persists its complete bounded typed SDK response as raw database
evidence. Thought-signature bytes are captured before JSON conversion, encoded
as standard padded base64, and replayed on their original ordered model parts
for provider continuity. Thought signatures, private thought text, encrypted
reasoning, unknown provider extensions, and unrestricted errors are never
shown in browser history or visible Trace. Accepted visible text alone becomes
Gemini's canonical reply.

## Install Gemini and publish the reviewed welcome

Fresh schema 1.4 initialization already includes Gemini's identity, alias,
bootstrap name event, and active room membership. The explicit verifier is:

```powershell
python -m app.main install-gemini --database data\helios.db
```

It validates that foundation transactionally and is idempotent. It does not
read environment variables, create a provider client, or call Google.

After installation, publish Helios's committed welcome as one canonical local
Helios-to-Gemini message:

```powershell
python -m app.main publish-gemini-welcome --database data\helios.db
```

The publisher verifies the reviewed source path, Git blob identity, exact
message hash, and existing publication state. It creates no provider event and
makes no provider call. Both commands are idempotent and fail closed on partial
or contradictory state.

## Store a Peter message from the command line

```powershell
python -m app.main store-message --destination-kind room --message "Hello from Peter"
```

This Room-only command creates one completed Peter turn, one exact canonical
message, and one explicit Room route atomically. Participant destinations and
`--participant-key` are rejected; the command never invokes Helios.

## Inspect participants and recorded turns locally

Enter `/participants` to open and refresh the participant panel without
posting a message. Exact and malformed `/participants` forms are intercepted
locally and rejected by the API/CLI as defense in depth.

Trace v3 retains the existing commands:

Enter either local command in the browser message box:

```text
/trace
/trace 17
```

`/trace` opens the latest turn in the `main` room. `/trace <turn_id>` opens the
specified positive decimal turn ID. These commands are intercepted locally:
they are not Peter messages, create no turn, message, API event, or admin event,
and never reach a provider. Malformed `/trace` usage is also rejected before any
canonical write.

The accessible trace panel shows the selected turn and room identity, exact
canonical messages and reply provenance, every referenced participant
configuration, the recorded provider request, the recorded provider outcome,
the ordered API-event timeline, redaction metadata, and recorded memory context
when one exists. Human-only, open, cancelled, failed, and stranded turns are
shown as recorded; a missing request or outcome is not inferred or fabricated.

Trace data comes from one sidecar-aware read-only SQLite snapshot. It never reruns a provider
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

Trace classifies OpenAI and Google event families through immutable recorded
configurations and enforces request/outcome cardinality and message
correlation. The local welcome turn is the provider-free exception. A corrupt
Gemini turn fails only its selected Trace/history domain; it does not poison
startup or unrelated turns.

Trace applies a display-only privacy projection without changing stored JSON.
Secret-like fields and raw or encrypted provider reasoning are omitted. An
explicitly recorded provider-generated reasoning summary may be displayed and
is labelled as a summary, while usage totals can still include aggregate
reasoning-token counts. Omission locations are reported as JSON Pointers.

Trace exposes the shared canonical room history, exact system instructions,
request settings, and operational provenance. For unredacted requests it shows
`Room-wide history`, `room_shared_v1`, effective sequence 1, and
the recorded closed projection version: historical requests retain
`provider_history_v2` and `provider_history_v3`, while current direct-address
and manual-handoff requests use `provider_history_v4`. Redacted requests show
only that request details were
redacted. Keep the server bound to a trusted local interface; Trace v3 is not
designed as a public or multi-user diagnostics API.

## Memory status and boundaries

Helios Room keeps three concepts separate:

- **Canonical history** is the immutable routed chat record for every room
  participant. It is shared with every selected provider and remains the only
  record of events that occurred in the room.
- **Seeded memory** is intended for curated continuity imported from
  conversations that occurred before Helios Room existed. Selected records are
  reference data, not room events, Peter messages, or instructions.
- **Room-created memory** is reserved for future interpretive records produced
  from activity inside the room.

Seeded Memory Retrieval v1 searches only active, nonsuperseded seed records
owned by the selected AI. Helios and Gemini records are private to their owner;
same-topic or same-text records never cross that boundary. `room_memories` is
not queried. There is no automatic memory creation, editing, deactivation,
supersession, or management UI.

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
python -m app.main import-seed-memories --owner-participant-key helios --file data\imports\helios_seed_memories_v1.json
```

A separately reviewed Gemini continuity manifest uses
`--owner-participant-key gemini`; never derive it from Helios-owned records.

Use `--database` to select another initialized schema-v1.4 database. The
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
chat history. For current direct-address turns it is followed by the trusted
`ROOM_RESPONSE_DESTINATION` routing item and then Peter's triggering canonical
message. For a manual handoff, retrieval instead queries the exact canonical
source message for the invoked AI and is followed by the final trusted
`ROOM_HANDOFF_AUTHORIZATION` item; no Peter chat message is fabricated. The
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
- `GET /api/participants` returns directory version 1 without provider or memory access.
- `POST /api/messages` requires exact `message_text` and structured
  `destination` fields. For an AI turn it also accepts optional
  `response_destination`, which defaults to Peter. The server assigns Peter's
  authorship, invokes only `destination`, and routes the resulting AI message to
  `response_destination`. Extra fields are rejected. Room posts reject a
  response destination.
- `POST /api/handoffs` accepts only a positive integer `source_message_id`.
  The server derives the invoked AI and return destination exclusively from
  the latest canonical AI-to-AI message.
- `GET /api/trace/latest` returns the latest recorded `main`-room turn through
  the read-only Trace v3 projection.
- `GET /api/trace/{turn_id}` returns one recorded turn for a canonical positive
  decimal SQLite turn ID.

Both trace endpoints return JSON with `Cache-Control: no-store`. Missing or
incompatible databases, invalid identifiers, missing turns, and invalid
historical data use stable error codes and do not fall back to chat or provider
behavior.

Example request body:

```json
{
  "message_text": "Hello from Peter",
  "destination": {
    "kind": "participant",
    "participant_key": "helios"
  },
  "response_destination": {
    "kind": "participant",
    "participant_key": "gemini"
  }
}
```

A Room destination is exactly `{"kind":"room"}`. Display aliases are never
accepted as server routing authority.

## Direct participant addressing

The browser presents two distinct choices for AI turns: **Ask** selects the
single provider Peter authorizes, and **Reply to** selects where that AI's one
canonical response is addressed. Reply targets may be Peter, the Room, or a
different active non-system participant. The responding AI cannot address
itself. Switching the selected provider resets the reply target to Peter.

Every successful provider-backed submission remains exactly two canonical
messages: Peter to the invoked AI, then that AI to the Phase A-bound reply
destination. The response still replies to Peter's trigger and the turn is
still initiated by Peter. Routing to another AI never invokes it, creates a
follow-up turn, or grants it agency; Peter must explicitly submit a separate
request for that participant to respond.

New request evidence records `explicit_response_destination_v1`, the exact
resolved destination snapshot, v3 provider instructions, and
`provider_history_v4`. Historical v1/v2 instructions and provider-history
v2/v3 requests remain closed, immutable validation contracts. Under v4, a provider's
own recorded responses remain native assistant/model history whether addressed
to Peter, another participant, or the Room. Other participants receive those
messages through the existing `ROOM_PARTICIPANT_MESSAGE` shared-history
envelope.

## Manual participant handoff

When the latest canonical message is one active registered AI addressing a
different active registered AI, the browser enables one global **Push to
participant** button. Peter's click creates a provider-backed turn initiated by
Peter, but it does not create a Peter message or duplicate the source. The
server binds the exact source, responder, return route, configuration, and
`manual_participant_handoff_v1` authorization evidence before making exactly
one provider call.

A successful handoff adds one canonical response whose turn sequence is 1 and
whose `reply_to_id` points to the source message in its earlier turn. The
response is routed back to the source sender. The new recipient remains inert
until Peter presses Push again. Failed attempts add no AI message and may be
retried by another explicit click; concurrent attempts for the same source are
rejected while one is open.

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
node tests/test_trace_ui.js
node --check static/app.js
```

Run the Node commands only when Node is available. If it is unavailable, report
the JavaScript suite as not run; do not treat that as an application runtime
failure and do not remove or weaken the JavaScript tests.

## Live-provider safety

Opening the application and using `/trace` contact neither provider. Submitting
an ordinary nonblank participant-directed message creates canonical history
and can make one billable OpenAI Responses or Google Generate Content request.
Do not perform a live-provider smoke test until Peter explicitly authorizes it
and confirms the intended database, participant, message, and model.

Automated tests use temporary databases, synthetic credentials, and mocked
providers. They are the default verification path and make no live OpenAI or
Gemini request.

## Controlled Gemini live acceptance

This procedure is intentionally manual and requires separate authorization.
Stop the server, create and verify the WAL-safe SQLite backup described above,
then run the installer and welcome publisher. Optionally import a separately
reviewed Gemini-owned manifest:

```powershell
python -m app.main install-gemini --database data\helios.db
python -m app.main publish-gemini-welcome --database data\helios.db
python -m app.main import-seed-memories --owner-participant-key gemini --file data\imports\gemini_seed_memories_v1.json
```

Set `GEMINI_API_KEY` outside committed files, select the intended
`HELIOS_GEMINI_MODEL`, start the server, and verify the directory, welcome
route, and provider-free welcome Trace before any billable call. Only after
explicit approval, select Gemini and send one approved message. Confirm one
Google call, one response, no OpenAI call, and inspect `/trace <turn_id>` for
request, response, model, usage, routes, memory ownership, and reasoning
omissions. Do not send an automatic follow-up. Preserve a failed database and
sidecars for diagnosis; restore only from the verified backup while the server
is stopped.

Official references: [Gemini models](https://ai.google.dev/gemini-api/docs/models),
[text generation](https://ai.google.dev/gemini-api/docs/text-generation),
[thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/gemini-3#thought_signatures),
[API keys](https://ai.google.dev/gemini-api/docs/api-key),
[Google Gen AI Python SDK](https://googleapis.github.io/python-genai/), and the
[pinned google-genai package](https://pypi.org/project/google-genai/2.18.0/).

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
app/       FastAPI entry point, database, identity, provider adapters/history, trace, and memory helpers
data/      Local SQLite database files (ignored by Git)
docs/      Approved implementation-contract amendments
examples/  Synthetic, nonpersonal documentation fixtures
schema/    Authoritative fresh SQLite schema v1.4 and retained legacy source schemas
static/    Browser interface
tests/     Automated tests
```
