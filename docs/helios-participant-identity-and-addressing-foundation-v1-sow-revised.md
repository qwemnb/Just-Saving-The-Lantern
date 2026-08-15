# Statement of Work: Participant Identity and Addressing Foundation v1

## Objective

Add stable participant identity, unique aliases, immutable name history, explicit message destinations, and participant-selection controls to `qwemnb/Helios-Room`.

This milestone must let Peter:

1. See who is currently in the room.
2. Select an addressable participant from a controlled list.
3. Open the same selector by typing `[` at the beginning of an otherwise empty composer.
4. Use `/participants` to open the participant panel without creating a canonical message.
5. Post a canonical message to `Room` without invoking an AI.
6. Address Helios through structured routing metadata without adding `[Helios]` to the canonical message text.

The milestone must also establish the identity and alias structures that a later Gemini integration can use. It must not add Gemini, another provider, or automated AI-to-AI conversation.

## Starting Point

Begin from the approved strict inherited-memory audit implementation commit:

```text
65c932632acad3f1146db8e6cfd397cb9aa67b01
```

If `main` has advanced, inspect and preserve newer work. Do not reset, rewrite Git history, or discard unrelated local changes. Record the actual starting commit in the implementation report.

The current room already has:

- stable numeric participant IDs
- stable participant keys for `peter` and `helios`
- active room-membership history
- immutable canonical messages
- participant configuration history
- the Peter-to-Helios three-phase turn flow
- Seeded Memory Retrieval v1
- Trace v1 with the strict inherited-memory audit amendment

The current browser and POST contract still assume that every Peter message is addressed to Helios. The current `participants.name` value is also read as though it were a permanent display name. This milestone replaces those assumptions without altering existing canonical message text.

## Preserve the Live Room First

The live database contains canonical Helios Room history and uses SQLite WAL mode. Before applying the schema migration or performing a manual live smoke test, stop the server and create a self-contained backup with SQLite's online backup API. Copying only `helios.db` is not sufficient because committed state may still be present in `helios.db-wal`.

From Windows Command Prompt in `C:\Helios-Room`:

```cmd
if not exist data\backups mkdir data\backups
python -c "from pathlib import Path; import sqlite3; source_path=Path('data/helios.db'); backup_path=Path('data/backups/helios-before-participant-addressing-v1.db'); assert source_path.is_file(), 'source database is missing'; assert not backup_path.exists(), 'backup destination already exists'; source=sqlite3.connect('file:data/helios.db?mode=ro', uri=True); backup=sqlite3.connect(backup_path); source.backup(backup); rows=backup.execute('PRAGMA integrity_check').fetchall(); assert rows == [('ok',)], rows; backup.close(); source.close(); print('Backup created and integrity_check passed:', backup_path)"
```

Do not replace this with `copy`, `shutil.copyfile`, or another raw single-file copy.

Automated implementation and tests must use temporary databases. They must not read, migrate, reset, or modify `data/helios.db`. Creating the backup is a manual prerequisite for Peter, not permission for Codex to change the live database.

## Scope Boundaries

### Included

- SQLite schema v1.3 and a transactional v1.2-to-v1.3 migration
- immutable participant keys and participant types
- globally unique participant aliases
- one current primary alias per participant
- append-only alias and primary-name history
- a room-owned system identity for canonical room events
- immutable routing metadata for every canonical message
- migration of existing Peter and Helios history to explicit routing records
- a read-only current-participant endpoint
- an explicit POST destination contract
- a `Room` destination that creates no provider request
- participant sidebar or drawer
- participant dropdown beside the composer
- `[` participant autocomplete
- `/participants` as a browser-only command
- stable error behavior and offline tests
- route visibility in message history and trace output

### Explicitly excluded

- Gemini registration or API integration
- Google credentials, models, SDKs, requests, responses, or traces
- adding any second provider-backed AI participant
- automatic or manual AI-to-AI handoffs
- conversation-cycle counters or the five-response automation limit
- parallel provider calls
- `Everyone`, `All`, broadcast, or multiple-recipient messages
- provider-visible participant-roster injection
- model tools for listing participants or requesting a rename
- a browser or public HTTP control for renaming participants
- participant join, leave, enable, disable, or configuration-management UI
- changes to seeded-memory ownership or retrieval
- private Gemini memory
- changes to Helios's model, reasoning settings, instructions, or configuration identity

This SOW creates the storage and service boundary needed for a future participant to adopt a name. The later Gemini SOW will decide how a Gemini response invokes that boundary safely.

## Settled Identity Decisions

### Permanent identity

- `participants.id` remains the immutable internal database identity.
- `participants.participant_key` remains the immutable stable application identity.
- Names and aliases are labels. Renaming must never change either stable identity.
- `participants.participant_type` is immutable after creation.
- The existing `participants.name` column becomes the participant's bootstrap display name. It must no longer be treated as the current name after schema v1.3.
- Existing Peter and Helios participant IDs and participant keys must remain unchanged.

### Aliases and current names

- Every participant has at least one alias.
- Exactly one alias is the participant's current primary alias.
- Primary and historical aliases are unique across all participants, not merely within one room.
- An alias can never be reassigned to a different participant, including after its owner leaves a room.
- A participant may later return to one of its own historical aliases.
- Historical aliases remain valid lookup terms for the same participant.
- Selecting a historical alias resolves to the same stable participant key, while the UI displays the participant's current primary name.
- Renaming never rewrites an earlier message, routing snapshot, or canonical room event.

### Alias validation and comparison

Implement one shared alias validator used by migration, service code, and tests.

For an accepted display alias:

1. The input must be a string.
2. Remove surrounding Unicode whitespace with Python `str.strip()`.
3. The resulting display value must contain between 1 and 64 Unicode code points.
4. Reject any Unicode code point whose general category begins with `C`.
5. Reject `[` and `]` because brackets are reserved for the participant-picker gesture.
6. Reject a display value whose first character is `/` because slash-prefixed text is reserved for browser commands.
7. Preserve every other accepted display code point exactly. Do not normalize the stored display alias.

Compute `alias_key` from the accepted display value as:

```python
unicodedata.normalize("NFKC", display_alias).casefold()
```

Uniqueness and lookup use the complete `alias_key`. Display uses the preserved alias text. This makes case and Unicode compatibility variants collide without rewriting the chosen spelling.

The normalized keys for `all`, `everyone`, `system`, and `participants` are reserved and cannot be adopted. `Room` is reserved by assigning that alias to the room-owned system identity created by this milestone.

Routine conflict errors must not echo an unrestricted requested alias or another participant's private configuration.

## Schema v1.3

Create a new master schema for fresh databases and a separate ordered migration for existing schema v1.2 databases. Preserve the append-only migration history:

```text
(1, "1.2")
(2, "1.3")
```

The implementation may choose equivalent table names, but it must implement all constraints and relationships below.

### 1. Participant aliases

Add an append-only `participant_aliases` table containing at least:

- immutable alias ID
- participant ID
- preserved display alias
- normalized `alias_key`
- creation timestamp

Require:

- a foreign key to `participants`
- a database-wide unique constraint on `alias_key`
- a composite unique key that can prove an alias belongs to its participant
- no update trigger
- no delete trigger

The application computes and validates `alias_key` before insertion. The schema is still the final uniqueness guard.

### 2. Current primary alias

Add a current-primary projection containing exactly one alias for every participant. It must enforce that the selected alias belongs to that participant.

Changing the projection is allowed only through the name-adoption service described below. It is mutable current state, not canonical history.

### 3. Append-only name events

Add an append-only participant-name event table containing at least:

- event ID
- room ID when the event belongs to a room
- participant ID
- new alias ID
- previous primary alias ID when applicable
- actor participant ID
- event type, limited to `bootstrap` and `adopted`
- canonical room-event message ID for an adopted name
- timestamp

Bootstrap events created by schema installation or migration do not add messages to canonical room history and have no canonical message ID. Every later `adopted` event must link to exactly one canonical room event created in the same transaction.

The exact bootstrap event shape is:

- `event_type` is `bootstrap`
- `room_id` is null because bootstrap identity installation is not a room event
- `actor_participant_id` equals the subject `participant_id`
- `previous_primary_alias_id` is null
- `new_alias_id` is the participant's bootstrap alias
- `canonical_message_id` is null

The exact adopted event shape is:

- `event_type` is `adopted`
- `room_id`, actor, subject, previous alias, and new alias are all non-null
- actor equals subject under the v1 self-adoption rule
- `canonical_message_id` is non-null and identifies the same-transaction
  room-system message

Database constraints must enforce the event-type-dependent nullability and
relationships wherever SQLite can express them reliably. Service validation
must fail closed on any remaining inconsistency.

The event table must reject updates and deletes.

### 4. Room system identity

Create one stable participant with:

```text
participant_key: room-system
participant_type: system
primary alias: Room
```

This identity authors canonical room events such as a future name-adoption notice. It is not a provider-backed participant and must never be selectable as a participant destination. It does not require an active `room_participants` membership.

Creating this identity during migration must not itself append a visible canonical message.

### 5. Immutable message routing

Add one immutable routing record for every canonical message. Each record must preserve at least:

- message ID
- room ID
- sender participant ID
- sender alias ID used for display when the message was created
- destination kind, exactly `participant` or `room`
- recipient participant ID when destination kind is `participant`
- destination alias ID used for display when the message was created; this is
  the recipient participant's alias for a participant destination and the
  immutable `Room` alias owned by `room-system` for a room destination
- routing mode, exactly `legacy_implicit` or `explicit`

Constraints must enforce:

- the sender alias belongs to the sender participant
- a participant destination has a recipient participant and a destination
  alias belonging to that participant
- a room destination has no recipient participant and has the immutable
  `Room` alias owned by `room-system` as its destination alias
- the routing row refers to the same message, sender, and room
- routing records cannot be updated or deleted

All application message-write paths must create the message and its routing record in the same transaction. A missing or inconsistent routing record is invalid canonical data. Trace and message-history reads must fail closed rather than infer routing for a new schema v1.3 message.

Do not add `[Name]` to `messages.message_text`. Addressing is metadata, not message content.

### 6. Participant immutability

Add schema guards that reject updates to existing participant IDs, participant keys, participant types, bootstrap names, and creation timestamps. Reject participant deletion after schema v1.3.

Future current-name changes occur only through alias and name-event structures.

## Transactional Migration

Provide a local command equivalent to:

```cmd
python -m app.main migrate-database --database data\helios.db
```

The command must:

1. Open the selected database through the existing connection helper.
2. Refuse to run while another application connection holds an incompatible lock.
3. Begin a single migration transaction before inspecting or changing migration-dependent data.
4. Accept only the exact schema v1.2 starting history or an already-complete exact v1.3 history.
5. Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` before migration.
6. Validate the existing `peter` and `helios` identities and the main room.
7. Create the v1.3 structures and guards.
8. Create one bootstrap alias and primary projection from every existing participant's current `participants.name` value.
9. Fail atomically if two existing names collide under the v1 alias normalization rule.
10. Fail atomically if an existing participant bootstrap name normalizes to
    any reserved key: `all`, `everyone`, `system`, `participants`, or
    `room`.
11. Create the room-owned system identity and its `Room` alias. If
    `participant_key = room-system` or the normalized `Room` alias already
    exists with any divergent ownership, type, name, or identity data, fail
    closed rather than adopting or repairing it.
12. Backfill exactly one immutable routing record for every existing canonical message.
13. Append schema migration 2 only after every v1.3 invariant is satisfied.
14. Run integrity, foreign-key, alias-cardinality, and message-routing-cardinality checks before commit.
15. Commit only if all checks pass.

Backfill routing as historical fact under the old universal contract:

- An existing Peter chat message is `legacy_implicit` to Helios.
- An existing Helios chat message is `legacy_implicit` to the turn initiator, which must be Peter for the current v1.2 room.
- An existing system message is `legacy_implicit` to `room`.
- Any other existing sender, recipient implication, missing participant, duplicate sequence, or unsupported shape fails the entire migration instead of guessing.

For every backfilled route, use the bootstrap alias IDs as the sender and recipient display snapshots. Do not update any existing message row, message text, room sequence number, turn, API event, participant configuration, seed memory, or memory record.

Migration idempotency:

- An exact completed v1.3 database returns `already_current` with zero writes.
- A partially applied, structurally divergent, unknown, or ambiguous migration state fails closed.
- A failed migration leaves the database at exact v1.2 state.
- The command returns a concise machine-readable report without canonical message text or secrets.

Fresh database initialization must produce the same v1.3 graph without first
installing v1.2 and then mutating it.

`init-db` may create or validate a fresh/current v1.3 database, but it must
never migrate an existing v1.2 database automatically. Only the explicit
`migrate-database` command may upgrade v1.2. When `init-db` encounters exact
v1.2 history, it must stop with a stable migration-required error and perform
zero writes.

## Name-Adoption Service Boundary

Implement a separately testable internal service function named or equivalent to `adopt_participant_name`.

Inputs:

- database connection
- room ID
- actor participant ID
- subject participant ID
- requested display alias

Required behavior:

1. Require actor participant ID to equal subject participant ID. This milestone permits only self-adoption.
2. Require the subject to be an active member of the selected room.
3. Validate and normalize the requested alias with the shared v1 algorithm.
4. Require the caller to have already opened a `BEGIN IMMEDIATE` transaction.
   The service must fail without writing if no transaction is active.
5. Resolve the subject's current primary alias.
6. If the normalized alias is already the current primary alias, return `unchanged` with zero writes.
7. If the alias belongs to another participant, fail with stable code `participant_alias_conflict` and zero writes.
8. If the alias is one of the subject's historical aliases, reuse its immutable alias row.
9. Otherwise insert one new immutable alias row.
10. Update the current-primary projection.
11. Append one canonical system message authored by `room-system` with exact text:

```text
<previous primary alias> adopted the name <new primary alias>.
```

12. Route that system message to `room` with explicit routing and a `Room` sender-alias snapshot.
13. Append one `adopted` name event linked to that canonical system message.
14. Return the service result without committing.

The caller exclusively owns transaction boundaries: it begins `BEGIN
IMMEDIATE`, calls the service, and commits or rolls back. The service must
never begin, commit, or roll back the caller's transaction. All validation and
conflict checks must complete before the service's first write. Any exception
leaves rollback responsibility with the caller.

Do not expose this operation through a browser control or public HTTP endpoint in this milestone. Do not parse ordinary model prose such as "call me Aster" as a rename request. The future Gemini integration must invoke this service through a deliberate structured control.

## Current Participant Directory

Add a read-only endpoint:

```text
GET /api/participants
```

It returns the current main-room directory using an exact, versioned response shape equivalent to:

```json
{
  "directory_version": 1,
  "room": {
    "room_key": "main",
    "name": "The Room"
  },
  "destinations": [
    {
      "kind": "room",
      "label": "Room",
      "addressable": true
    },
    {
      "kind": "participant",
      "participant_key": "helios",
      "primary_name": "Helios",
      "aliases": ["Helios"],
      "participant_type": "ai",
      "addressable": true
    },
    {
      "kind": "participant",
      "participant_key": "peter",
      "primary_name": "Peter",
      "aliases": ["Peter"],
      "participant_type": "human",
      "addressable": false
    }
  ]
}
```

Requirements:

- Return only participants with a current active membership in the main room, plus the special `room` destination.
- Do not return the `room-system` identity as a participant destination.
- Sort `room` first, then participant destinations by normalized primary alias and stable participant key.
- List the primary alias first, followed by historical aliases in deterministic alias-ID order.
- `addressable=true` means the current server can accept a Peter message for that destination in this milestone.
- `Room` and Helios are addressable.
- Peter and any human, system, inactive, unknown, or unsupported participant are not addressable.
- Do not determine addressability by exposing or testing an API key in this endpoint.
- Do not expose participant configuration instructions, credentials, private memory, database paths, or provider errors.
- Return `Cache-Control: no-store`.
- Reading the directory creates no turn, message, API event, memory query, provider client, or database write.

The directory service becomes the single source of truth for browser selection. A future handoff SOW may reuse it to construct provider-visible routing context, but this milestone must not inject the directory into Helios's provider request.

## Explicit Message Destination Contract

Replace the implicit POST destination with an exact discriminated destination object.

Participant destination:

```json
{
  "message_text": "Hello, Helios.",
  "destination": {
    "kind": "participant",
    "participant_key": "helios"
  }
}
```

Room destination:

```json
{
  "message_text": "I want this to remain in the room without requesting a response.",
  "destination": {
    "kind": "room"
  }
}
```

Validation requirements:

- The top-level request contains exactly `message_text` and `destination`.
- A participant destination contains exactly `kind` and `participant_key`.
- A room destination contains exactly `kind`.
- Continue to forbid browser-supplied authorship, participant configuration, turn IDs, message types, reply IDs, provider settings, and memory controls.
- Preserve `message_text` exactly after JSON decoding. Trimming is used only to reject or ignore whitespace-only text under the existing contract.
- The participant key in the POST body is the stable participant key returned by the directory, not a display name or alias.
- Do not accept an arbitrary alias string as server routing authority.
- Do not silently default a missing destination to Helios.
- Reject unknown fields rather than ignoring them.

The browser resolves primary or historical aliases through the directory and submits the stable participant key. The server independently resolves and validates that key again inside Phase A.

### Request-processing precedence

Apply this exact order:

1. Validate the complete JSON request against the exact discriminated schema.
2. For a structurally valid request, apply the existing whitespace-only
   message behavior without altering `message_text`.
3. Resolve and authorize the structured destination inside the Phase A
   `BEGIN IMMEDIATE` transaction, before canonical acceptance.
4. Only for an authorized Helios destination, load and validate provider
   environment, select or create the semantically exact configuration, run
   memory retrieval, and record the provider request.
5. For a Room destination, never load provider environment, select or create a
   provider configuration, search memory, construct a provider client, or
   create an API event.

An unavailable participant destination must therefore consistently return
`participant_destination_unavailable` without being masked by missing
provider credentials. Room posts remain completely provider-independent.

### Command-line message writer

Revise `store-message` so it cannot create unrouted schema v1.3 messages. It
must require an explicit destination using unambiguous CLI arguments equivalent
to either:

```text
--destination-kind participant --participant-key helios
--destination-kind room
```

It must apply the same destination schema, authorization, command interception,
exact-message preservation, and atomic message-plus-route rules as the HTTP
path. It must not default a missing destination to Helios and must reject
unsupported or ambiguous combinations before opening a write transaction.
The machine-readable result may contain canonical IDs and stable status fields
but must not echo unrestricted message text or secrets.

## Canonical provider-history contract

Room-directed Peter chat messages are canonical shared-room history and remain
visible to Helios on later Helios-directed turns in room-sequence order. They
are serialized as ordinary Peter `user` items without destination labels,
alias text, or participant-directory injection.

Name-adoption messages authored by `room-system` are validated as canonical
system history and omitted from Helios provider input. They must never cause a
later valid Helios turn to fail merely because their message type or sender is
system-owned.

The history loader must validate routing before deciding whether to include or
omit a message:

- supported Peter chat messages directed to Helios or Room are included as
  `user` items
- supported Helios chat responses directed to Peter are included as
  `assistant` items
- valid room-system adopted-name system messages directed to Room are omitted
- unsupported system messages, correction or retraction messages, unsupported
  participants, missing routes, duplicate routes, cross-participant aliases,
  contradictory destinations, or any other unsupported shape fail closed

The final triggering Peter message and inherited-memory positioning contract
remain unchanged.

## Participant-Directed Helios Turn

When the destination is Helios:

1. Run the existing preflight and three-phase turn flow.
2. In Phase A, validate that Helios exists, is an active AI member of the room, and is addressable by this implementation.
3. Create Peter's canonical message with an explicit route to Helios.
4. Snapshot Peter's current primary alias as sender alias and Helios's current primary alias as recipient alias.
5. Continue existing seeded-memory retrieval, configuration selection, API-event recording, request serialization, one-provider-call rule, `store=False`, and finalization behavior.
6. Do not prepend, append, or otherwise insert `[Helios]` into Peter's canonical message or provider input.
7. On successful finalization, create Helios's canonical response with an explicit route to Peter and current alias snapshots for both participants.

This milestone must not generalize Helios-specific memory or provider code to an arbitrary AI merely because another AI row exists. Until a provider integration explicitly supports another participant, that participant is not addressable.

The exact Seeded Memory Retrieval v1 request contract remains unchanged:

- retrieval is based on Peter's exact accepted message
- selected inherited context remains exactly at `request.input[-2]`
- Peter's triggering message remains exactly at `request.input[-1]`
- no participant-directory context item is inserted
- strict raw-event validation remains in force

## Room-Directed Post

When the destination is `room`:

1. Open one `BEGIN IMMEDIATE` transaction.
2. Resolve Peter, Peter's current primary alias, and the main room.
3. Create one turn initiated by Peter.
4. Create exactly one canonical Peter chat message.
5. Create its explicit route with destination kind `room`.
6. Mark the turn completed in the same transaction.
7. Commit atomically.

A successful room-directed post creates:

- one completed turn
- one canonical Peter message
- one immutable routing record
- zero provider configurations
- zero API events
- zero memory queries
- zero provider clients or calls
- zero AI messages

If any part fails, roll back the entire transaction. Return a stable error without reflecting unrestricted message text or an internal exception.

## Invalid or Unavailable Destination

For a syntactically valid participant destination, resolve and validate the destination inside the same Phase A transaction that would accept the message.

Reject the request before canonical acceptance if the participant:

- does not exist
- is not an active member of the main room
- is human or system
- has no supported participant execution path in this milestone
- is otherwise not addressable according to the same directory service

Return stable code `participant_destination_unavailable` with a generic message. Do not echo the submitted key in the response.

The rejection must create:

- zero turns
- zero messages
- zero routing rows
- zero API events
- zero memory searches
- zero provider-client constructions
- zero provider calls

A race in which a destination becomes unavailable after the directory was loaded must fail the same way. The browser list improves the experience; the server remains authoritative.

## Sanitized HTTP and read-service errors

Install an application-level validation error contract for the new exact POST
schema rather than returning FastAPI/Pydantic's default field-by-field body.
Malformed JSON, missing fields, wrong types, discriminator errors, and unknown
top-level or nested fields return a stable generic response:

```text
HTTP 422
error: invalid_message_request
message: The message request is invalid.
Cache-Control: no-store
```

The response must not contain rejected field names, rejected values,
unrestricted participant keys, validation internals, request fragments,
provider settings, memory controls, SQL, paths, or secrets.

The `GET /api/participants` and `GET /api/messages` routes must also return
`Cache-Control: no-store` on success and failure. Directory or history data
inconsistency returns a stable generic 500 code and message defined in code and
tests; unrestricted exception text, SQL, paths, aliases from a rejected row,
credentials, configurations, or private memory must not be exposed.

Add real-ASGI tests with distinctive secret sentinels in an unknown top-level
field and an unknown nested destination field. Assert the stable status, code,
message, `no-store` header, and absence of both rejected names and values
from response bodies and headers.

## Message History and Display Snapshots

Extend `GET /api/messages` so every returned message includes immutable sender and destination display information from its routing record.

Keep existing identity and canonical fields needed by the browser. Add an exact routing object equivalent to:

```json
{
  "routing_mode": "explicit",
  "sender": {
    "participant_key": "peter",
    "display_name": "Peter"
  },
  "destination": {
    "kind": "participant",
    "participant_key": "helios",
    "display_name": "Helios"
  }
}
```

For a room destination:

```json
{
  "routing_mode": "explicit",
  "sender": {
    "participant_key": "peter",
    "display_name": "Peter"
  },
  "destination": {
    "kind": "room",
    "display_name": "Room"
  }
}
```

Historical v1.2 messages use `routing_mode: "legacy_implicit"`.

The browser renders the saved snapshot, for example:

```text
Peter -> Helios • 12:20 PM
Helios -> Peter • 12:20 PM
Peter -> Room • 12:21 PM
```

After a participant adopts a new name, earlier messages continue displaying the alias snapshot saved with those messages. New messages display the new current primary alias.

If a schema v1.3 message lacks valid routing, `GET /api/messages` must fail safely instead of substituting the participant's current name or guessing a recipient.

## Trace Integration

Trace must expose the same immutable routing snapshot for every selected canonical message. Because this changes the trace response contract, increment the trace response version rather than silently changing the meaning of Trace v1.

Trace v2 must preserve all Trace v1 guarantees, including:

- server-selected database only
- read-only snapshot
- `Cache-Control: no-store`
- exact provider request and outcome selection
- defense-in-depth privacy projection
- strict inherited-memory raw-event validation
- no memory query, provider construction, provider call, canonical write, or configuration mutation

For a room-directed turn, Trace v2 must show:

- the completed turn
- Peter's exact canonical message
- the explicit room route
- no recorded provider request
- no inherited-memory retrieval
- no provider outcome
- no API events

The trace browser panel must render route kind, routing mode, sender display snapshot, and recipient display snapshot using `textContent`, never HTML interpolation.

Do not add alias text, participant lists, or routing instructions to recorded provider payloads merely to make trace display easier. Trace obtains routing from canonical routing records.

## Browser Requirements

### 1. Participant panel

- Add a participant panel on the left at desktop widths.
- At narrow widths, provide the same content in a collapsible drawer.
- Load it only from `GET /api/participants`.
- Show current primary names prominently.
- Show historical aliases as secondary text when present.
- Show non-addressable current participants as disabled, not selectable.
- Do not display the hidden `room-system` identity.
- A directory-loading failure must not fabricate participants or allow an unvalidated send.

### 2. Composer destination control

- Add a destination control immediately to the left of or directly above the message input.
- The control contains only addressable directory entries.
- The selected destination is visibly rendered as a chip or labeled dropdown value.
- The initial selected destination is Helios after a successful directory load.
- The selected destination persists after a successful send until Peter changes it.
- Disable destination changes while a POST is in flight.
- Disable Send when message text is blank, the directory is unavailable, or no valid destination is selected.
- Never provide a free-form destination field.

### 3. `[` participant picker gesture

When the message input is empty except for optional whitespace and is not in an IME composition, pressing `[` must:

1. prevent insertion of the bracket
2. open the same participant picker used by the destination control
3. focus its search field
4. search current primary names and historical aliases

Selection sets the structured destination and returns focus to the message input. It does not insert `[Name]` into message text.

Support mouse and keyboard selection, including Arrow keys, Enter, and Escape. Escape closes the picker without changing the existing destination. Pasted bracketed prose and brackets typed after non-whitespace message content remain ordinary message text.

An unknown typed search term produces an empty result list. It must never become a destination value.

### 4. `/participants` local command

Recognize exact `/participants` with optional surrounding whitespace as a
browser-only command. It refreshes and opens the participant panel or drawer.

- Clear or retain the composer consistently with the existing `/trace` command behavior.
- Do not POST `/participants` to `/api/messages`.
- Do not create a turn, message, routing row, API event, memory query, or provider call.
- Treat `/participants` followed by arguments as malformed and show concise usage text.
- Preserve the existing `/trace` and `/trace <turn ID>` grammar and behavior.

Defense in depth is mandatory. Extend the shared server/CLI local-command
classifier so exact and malformed `/participants` forms are reserved before
database preflight, provider-environment loading, configuration selection,
memory retrieval, provider construction, or any canonical write. A direct
HTTP POST or `store-message` attempt must return/reject with a stable generic
`local_command_only` contract and zero side effects. Browser, API, classifier,
and CLI tests must cover exact, surrounding-whitespace, malformed-with-
arguments, case-sensitive near-miss, and non-command slash text.

### 5. Safe rendering and failure recovery

- Render aliases, names, message text, and status text with `textContent`.
- Do not use `innerHTML` for participant-controlled text.
- If the server rejects a stale destination before acceptance, preserve Peter's draft, refresh the directory, and require a new valid selection.
- If a failure occurs after canonical Peter-message acceptance on a Helios turn, preserve the existing post-acceptance reload behavior.
- For a room post, do not show `Helios is responding...`; show a neutral saving state.
- For a participant post, use the selected participant's saved display label in the pending state rather than a hard-coded Helios string.

## Required Tests

All tests use temporary databases and fake providers. No test may read or mutate the live database or make a real network request.

### Schema and migration

- Fresh initialization produces exact schema history 1.2 then 1.3.
- Peter and Helios retain their existing numeric IDs and stable keys after migration.
- Existing canonical message text, ordering, turns, configurations, API events, seeded memories, and room memories remain unchanged.
- Every existing participant receives one bootstrap alias and one primary alias.
- Every existing canonical message receives exactly one valid `legacy_implicit` route.
- Existing Peter messages route to Helios and existing Helios messages route to Peter.
- Existing system messages route to Room using the immutable `Room`
  destination-alias snapshot.
- The room-system participant and `Room` alias are created without a canonical message.
- Migration rejects a normalized alias collision atomically.
- Migration rejects reserved bootstrap names, a divergent pre-existing
  `room-system` identity, and divergent pre-existing `Room` alias ownership.
- `init-db` refuses exact v1.2 with zero writes; only
  `migrate-database` upgrades it.
- Migration rejects an unsupported historical message shape atomically.
- Re-running migration returns `already_current` with zero writes.
- Partial or unknown migration history fails closed.
- Integrity and foreign-key checks pass after fresh initialization and migration.

### Alias and name history

- Alias comparison is NFKC and case-insensitive by exact `casefold()` output.
- `Aster`, `aster`, and compatibility-equivalent spellings collide.
- Surrounding Unicode whitespace is removed before validation.
- Control characters, brackets, slash-prefixed names, blank names, overlong names, and reserved names are rejected.
- A new alias cannot be assigned to two participants.
- An alias remains unavailable to another participant after its owner leaves.
- A participant can return to its own historical alias.
- Requesting the current normalized alias returns `unchanged` with zero writes.
- Bootstrap and adopted events enforce their exact type-dependent nullability.
- The adoption service requires a caller-owned `BEGIN IMMEDIATE` transaction
  and never begins, commits, or rolls back it.
- All adoption validation completes before the first write.
- A participant cannot rename another participant.
- A successful adoption atomically changes the primary projection, appends one immutable alias when needed, appends one name event, and appends one canonical Room event.
- A failed adoption leaves aliases, primary projection, canonical messages, room sequence, and name events unchanged.
- Earlier message display snapshots remain unchanged after name adoption.
- Direct update or deletion of aliases, name events, routing rows, or stable participant identity is rejected by schema guards.

### Participant directory

- The endpoint returns exact directory version 1 and `Cache-Control: no-store`.
- Room appears once and is addressable.
- Helios appears once and is addressable.
- Peter appears once and is not addressable.
- Room-system is absent.
- Primary and historical aliases are deterministic and correctly associated.
- Inactive participants are absent.
- Repeated reads perform zero writes and create no provider or memory activity.
- No configuration instructions, credentials, private memory, database path, or unrestricted exception text appears.

### Request validation and routing

- Exact Helios destination succeeds through the existing provider flow.
- Peter's exact `message_text` is unchanged in canonical storage and provider input.
- `[Helios]` is not inserted into canonical text or provider input.
- Missing destination is rejected without defaulting.
- Structurally invalid requests return the exact generic
  `invalid_message_request` response with `Cache-Control: no-store`.
- Extra top-level and nested keys are rejected without field-name or value
  leakage, including distinctive secret sentinels.
- A display alias supplied where a stable participant key is required is rejected.
- Unknown, inactive, human, system, and unsupported-AI destinations fail with `participant_destination_unavailable`.
- Every invalid destination case produces zero turns, messages, routing rows, API events, memory searches, provider clients, and provider calls.
- A destination made unavailable between directory read and POST fails the same way.
- Destination authorization precedes provider-environment loading; unavailable
  destinations are not masked by missing provider credentials.
- Room requests never load provider environment or select a configuration.
- Exact and malformed `/participants` forms are blocked through direct API and
  CLI entry points with zero side effects.
- `store-message` requires an explicit valid destination and creates the
  message and route atomically.
- A successful Peter message snapshots Peter's and Helios's current aliases.
- A successful Helios response snapshots Helios's and Peter's current aliases.
- Provider exceptions preserve the existing accepted Peter message and its explicit routing record.
- Finalization failure preserves the existing stranded-turn behavior and does not invent an AI route.

### Room-directed posts

- A valid Room post creates exactly one completed turn, one Peter message, and one explicit room route.
- It creates no provider config selection, API event, memory retrieval, provider client, or AI message.
- Blank-message behavior remains consistent with the current contract.
- A transaction failure leaves no partial turn, message, or route.
- Room message history and trace contain exact text and correct routing.
- A Room-directed Peter message appears as an ordinary `user` history item in
  a later Helios provider request.
- Valid room-system name-adoption messages are omitted from provider input
  without breaking later turns.
- Unsupported system, correction, retraction, participant, or routing history
  fails closed.

### Seeded-memory and trace regression

- A Helios-directed turn still creates exactly one provider request and at most one response.
- The triggering Peter item remains exactly `request.input[-1]`.
- When inherited memory is selected, its exact context item remains `request.input[-2]`.
- Strict raw-event schema and secret-leakage tests continue to pass.
- No participant directory is injected into the provider request.
- Trace v2 shows explicit and legacy routing snapshots.
- Trace v2 rejects missing, duplicate, or cross-participant routing rows as invalid trace data.
- Trace v2 room-only turns contain no fabricated provider or memory activity.
- All trace endpoints remain read-only and no-store.

### Executable browser tests

- The participant directory loads before Send becomes available.
- Desktop panel and narrow-width drawer use the same directory data.
- Clicking Helios selects its stable participant key.
- Clicking Room selects a room destination.
- Peter is visible but disabled.
- Pressing `[` in an empty composer opens the picker without changing message text.
- Primary and historical aliases filter to the same participant.
- An unknown search term cannot be selected or submitted.
- Keyboard navigation and Escape behavior work.
- Pasted bracketed prose remains message content.
- `/participants` opens the panel and performs no message POST.
- Malformed `/participants` shows usage and performs no POST.
- Direct API and CLI attempts to submit exact or malformed `/participants`
  are intercepted before every side effect.
- `/trace` behavior remains unchanged.
- Participant POST JSON contains the exact structured destination and no authorship field.
- Room POST JSON contains no participant key.
- Pending status uses the selected destination label.
- Room posts never display an AI-response pending state.
- Alias and message rendering uses safe text nodes.
- A stale-destination rejection preserves the draft and refreshes the directory.

## Manual Acceptance

After Peter has created the required backup and intentionally run the migration:

1. Start Helios Room normally.
2. Confirm the participant panel shows Peter and Helios, with Peter disabled as a destination.
3. Confirm the composer initially targets Helios.
4. Type `[` into an empty composer and confirm the picker opens without adding a bracket to the message.
5. Select Helios and send a short message.
6. Confirm canonical history shows `Peter -> Helios` and `Helios -> Peter` while preserving exact message text.
7. Select Room and send a short note that should not receive an answer.
8. Confirm the note appears as `Peter -> Room` and no AI response occurs.
9. Run `/participants` and confirm the panel opens without adding a canonical message.
10. Run `/trace` for the Room turn and confirm no provider request, memory retrieval, or provider outcome is shown.
11. Run `/trace` for the Helios turn and confirm the exact route and existing seeded-memory audit remain valid.

Do not use Peter's live database for destructive, mutation-based, or failure-injection tests.

## Documentation

Update project documentation to explain:

- permanent participant ID versus stable participant key versus display alias
- primary and historical aliases
- global alias uniqueness and normalization
- immutable name and routing history
- participant directory and addressability
- structured POST destinations
- `[` picker behavior
- `/participants`
- Room-directed posts
- the manual backup and migration procedure
- how to restore the backup if migration fails before live acceptance
- why Gemini, provider-visible rosters, and automated handoffs remain separate milestones

Do not include real credentials, Peter's private memory text, or Gemini history in committed fixtures or documentation.

## Repository and Authorization Constraints

- Implementation approval for this SOW authorizes only the included scope.
- Do not implement Gemini or automated turn-taking as an anticipatory extra.
- Do not modify the live database.
- Do not rewrite Git history.
- Do not discard unrelated work.
- Do not commit secrets, live database files, WAL files, backups, or Peter's private memory exports.
- Keep all provider tests offline with fakes.
- Preserve the existing one-request, zero-retry Helios policy.
- Preserve canonical message immutability and exact message text.

## Deliverables

1. Schema v1.3 master schema and ordered v1.2 migration.
2. Migration command and machine-readable report.
3. Alias validation, primary-name projection, name-event history, and internal self-adoption service.
4. Immutable message-routing persistence and historical backfill.
5. Read-only participant-directory endpoint.
6. Explicit participant and Room destination POST contract.
7. Participant panel, destination control, `[` picker, and `/participants` command.
8. Message-history and Trace v2 route display.
9. Offline schema, service, API, trace, and executable browser tests.
10. Updated documentation.
11. Completion report containing:
    - actual starting and ending commits
    - files changed
    - migration and schema decisions
    - tests and exact commands run
    - test results
    - any deviations from this SOW
    - confirmation that the live database was not modified

## Acceptance Criteria

This milestone is complete only when:

- permanent participant identity is independent from display names
- aliases are globally unique and never reassigned
- one current primary alias and complete name history are preserved
- existing Peter and Helios message text and ordering remain unchanged
- every canonical message has one immutable sender and destination snapshot,
  including the immutable `Room` alias snapshot for room routes
- Peter can select only valid destinations through the browser
- `[` opens participant selection without becoming message content
- `/participants` produces no canonical or provider activity
- Helios-directed turns preserve the current memory and provider contracts
- Room-directed posts produce no AI or memory activity
- invalid destinations fail before canonical acceptance, provider-environment
  loading, configuration selection, memory retrieval, and provider construction
- request-validation failures use a stable no-store response without rejected
  field-name or value leakage
- Room-directed chat remains later Helios-visible history while valid
  room-system adoption events are omitted
- the adoption service uses caller-owned transaction boundaries exclusively
- `store-message` cannot create an unrouted or implicitly routed message
- historical names remain historically accurate after later renames
- Trace exposes routing without weakening its privacy or read-only guarantees
- Gemini and automated AI-to-AI conversation remain unimplemented



