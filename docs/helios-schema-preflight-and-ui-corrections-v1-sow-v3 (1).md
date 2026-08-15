# Statement of Work: Schema Preflight and Participant UI Corrections v1

## Revision

This is revision 3. It supersedes revisions 1 and 2 for implementation approval
while preserving both earlier documents unchanged.

## Document status

This is a corrective SOW following the uncommitted working-tree implementation
of Participant Identity and Addressing Foundation v1. That implementation is
currently layered on GitHub `main` commit:

```text
65c932632acad3f1146db8e6cfd397cb9aa67b01
```

The commit above is the Git base of the working tree. It does not itself contain
the identity-foundation implementation.

This SOW does not replace or reopen the approved identity, alias, routing, or
schema v1.3 contracts in Participant Identity and Addressing Foundation v1 SOW
revision 3. It adds deployment-safety and browser-finish requirements exposed
by the first local launch of the implementation.

## Implementation baseline

Implement this correction on top of the current uncommitted working tree that
already contains the completed Participant Identity and Addressing Foundation
v1 implementation.

Before making a correction, the implementer must inspect and record:

```text
git rev-parse HEAD
git status --short
git diff --stat
```

The implementation must preserve every existing foundation change and every
unrelated user change. It must not use checkout, reset, clean, restore, stash,
rebase, or any other operation that could remove, hide, or rewrite the current
working tree. Do not reconstruct the foundation from commit `65c9326`.

This SOW does not authorize a commit. If Peter separately commits the
foundation before corrective work begins, record that new commit and the clean
or remaining working-tree state as the actual baseline. Otherwise, report
`65c9326` as the Git base and explicitly report that the correction was built
on the preserved uncommitted foundation working tree.

## Observed problem

The v1.3 application was started while the live database was still schema
v1.2. The browser loaded the new static interface, but both the participant
directory and message-history requests failed because those read services
correctly require this exact migration history:

```text
(1, "1.2")
(2, "1.3")
```

This produced a partially loaded room with generic errors instead of preventing
an invalid server launch. The new participant panel, Refresh control, and
destination picker also appear visually unfinished compared with the existing
Helios Room interface.

The diagnosis does not indicate canonical-data corruption. It indicates a
missing startup compatibility gate and incomplete presentation polish.

## Objective

Reject an incompatible database observed by a consistent startup preflight,
make runtime read failures understandable without leaking internal data, and
finish the visual integration of the participant and destination controls.

After this correction:

1. `serve` starts only when the selected database is a compatible schema v1.3
   Helios Room database.
2. A schema v1.2 database produces an exact migration-required CLI error before
   Uvicorn binds a socket or serves static files.
3. Startup validation never performs database-content, schema, journal-mode,
   checkpoint, initialization, migration, or repair writes.
4. Browser read failures use stable, accessible status presentation and do not
   expose exceptions or submitted data.
5. The participant panel and destination picker look like intentional parts of
   the existing interface.
6. Node remains optional developer tooling and is not required to run Helios
   Room.

Startup acceptance applies to the database snapshot validated during
preflight while the selected database file remains unchanged. Runtime read
services continue to validate and fail closed on their own later snapshots.

## Live-database protection

Implementation and automated tests must not open, inspect, migrate, copy,
reset, or modify:

```text
data/helios.db
data/helios.db-wal
data/helios.db-shm
```

All database tests must use newly created temporary databases. Do not run the
application against the live database as part of implementation verification.

This SOW does not authorize migration of the live database. After the
correction is reviewed and approved, Peter will separately stop the server,
create and integrity-check the required SQLite online backup, run the explicit
v1.2-to-v1.3 migration, and perform the manual acceptance checks.

Do not use `init-db` on the live database. Do not add automatic migration to
startup.

## Scope

### Included

- read-only database compatibility preflight for the `serve` command
- exact safe CLI errors for missing, migration-required, and incompatible
  databases
- complete shared, pure, read-only v1.2 source and v1.3 foundation validators
- clearer browser handling for message-history and participant-directory read
  failures
- accessible status presentation
- visual polish for participant and destination controls
- cache-safe delivery of the corrected static assets
- automated Python and JavaScript coverage for the corrections
- documentation of the startup, migration boundary, and optional Node tooling

### Excluded

- any live-database migration or backup operation
- changes to schema v1.3 or the approved v1.2-to-v1.3 migration contract
- changes to canonical messages, message routing, aliases, name events, or
  participant identities
- Gemini or another provider integration
- provider configuration changes or live provider requests
- automatic migration, repair, rollback, or backup during `serve`
- a general redesign of Helios Room
- adding a frontend framework, bundler, package manager, or Node runtime
  dependency

## Required corrections

## 1. Read-only startup schema preflight

Add one startup-preflight service backed by shared, pure, read-only schema
validators and call it synchronously from the `serve` CLI path before
`uvicorn.run()`.

The preflight must:

1. Resolve the requested database path without creating the file or its parent
   directory.
2. Require an existing regular database file.
3. Open it using a SQLite `mode=ro` URI and set `PRAGMA query_only = ON`.
4. Apply the existing busy timeout and enable foreign-key enforcement.
5. Begin one read transaction before reading schema history or application
   state. Every schema, identity, integrity, and cardinality decision must come
   from that one consistent snapshot.
6. Read and validate `schema_migrations` without invoking initialization or
   migration code.
7. Accept only the exact ordered migration history required by the current
   application:

   ```text
   (1, "1.2")
   (2, "1.3")
   ```

8. Run the shared v1.3 foundation validator defined below, followed by the
   startup-only full-database integrity and foreign-key checks.
9. End the read transaction and close the connection before Uvicorn starts.

### Shared pure validators

Create or extract two pure, read-only validators. They must accept an existing
SQLite connection and must not open, close, commit, roll back, initialize,
migrate, repair, or mutate a database. Transaction ownership remains with the
caller.

#### Complete v1.2 source validator

The v1.2 validator must be the same validator used by the approved migration,
not a second weaker startup approximation. Refactor existing migration checks
as needed so startup classification and migration eligibility cannot diverge.

It must apply every approved source check, including:

- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check`
- exact v1.2 schema migration history
- complete required v1.2 schema shape
- main-room identity and cardinality
- Peter and Helios stable identity, type, and membership rules
- bootstrap-name and alias-migration eligibility
- reserved-name, normalized-alias, and collision rejection
- divergent or pre-existing `room-system` rejection
- participant-configuration referential and immutability invariants without a
  fresh-seed row-count assumption
- turn and canonical-message referential integrity without requiring provider
  completion for every turn
- every historical message shape that the routing backfill accepts
- legacy system-message eligibility
- all other source conditions already required before the migration's first
  write

The v1.2 validator must apply mature-source cardinalities. It must allow
additional valid participants and memberships, historical memberships,
multiple valid immutable participant configurations, and legitimate completed,
open, failed, cancelled, human-only, Room-only, or stranded turns. It must not
require every turn to have a provider event, AI response, memory retrieval, or
successful Trace projection. It must still reject every reserved-name,
identity, alias-migration, message-backfill, or other source conflict that would
make the approved migration unsafe. As already required by the migration
contract, a pre-existing `room-system` remains a source conflict in v1.2.

The validator must inspect the full source condition without executing the
migration, creating projected rows, or making temporary changes to the source
database. Any migration-only derived comparison must be computed in memory or
in read-only query results.

Only a database with exact migration history `[(1, "1.2")]` that passes this
complete shared validator may be classified as `database_migration_required`.
Every other v1.2-like database is incompatible.

#### Shared v1.3 foundation validator

The shared v1.3 validator is a foundation validator, not a projection of every
domain and every historical record. It must be shared by startup,
existing-database initialization/validation, participant-directory reads,
message-history reads, and Trace reads. Each surface then applies only its own
additional domain checks to the rows it is authorized to inspect.

The shared foundation validator must verify:

- exact ordered migration history `[(1, "1.2"), (2, "1.3")]`
- every required table and its required columns, declared types, nullability,
  primary keys, foreign keys, unique constraints, and check constraints
- every required view and the columns and semantics relied upon by services
- every required index, including uniqueness and indexed-column order
- every append-only, immutability, participant guard, routing guard, and other
  safety trigger required by schema v1.3
- the mature main-room and core-membership cardinalities defined below
- the stable `peter`, `helios`, and `room-system` keys and participant types
- the exact hidden `room-system` graph, including `participants.name = Room`,
  the immutable `Room` alias, its primary projection, bootstrap event, and no
  active membership
- at least one valid immutable alias for every participant
- exactly one current-primary projection for every participant, selecting an
  alias owned by that same participant
- exactly one bootstrap name event for every participant, with the approved
  bootstrap shape and an alias owned by the subject
- exactly one immutable route for every canonical message and no orphan route
- valid sender and destination alias ownership for every route

The foundation validator must not globally project or validate:

- supported message-history presentation for every historical message
- provider-event completeness for every turn
- Trace eligibility for every historical turn
- selected-turn Trace positions or selected-turn configuration history
- recorded memory-audit completeness for every turn
- current memory-table rows, memory retrieval, or provider prompt history
- the presence of an AI response or provider event for every turn

Startup preflight and existing-database initialization/validation must compose
the shared foundation validator with `PRAGMA integrity_check` and
`PRAGMA foreign_key_check` in the same caller-owned snapshot. Directory,
message-history, and Trace requests must not run those global PRAGMAs when doing
so would traverse unrelated domain storage. They use the shared schema and core
graph checks plus their own bounded domain validation.

Schema-object validation may inspect SQLite schema metadata for required tables,
including memory and audit tables, but foundation validation must not query
current memory-table data. Directory validation must not query memory data,
provider events, provider prompts, or Trace history. Trace must retain its
strict rule that it never queries current memory tables.

Foundation validation verifies that the adopted-name schema constraints,
foreign keys, and safety triggers are present. It does not globally project
every adopted-name event or require every historical event to satisfy a
current presentation contract at startup.

Peter and Helios may have validly adopted new current primary aliases. The
validator must not require their current primary display names to remain
`Peter` and `Helios`. It must validate their stable participant keys, types,
bootstrap history, alias ownership, and current-primary projections. Their
original bootstrap aliases remain immutable historical aliases. The foundation
does not need to project every adopted event merely to accept a current primary
alias whose ownership and structural history are valid.

Checking only object names, migration rows, or the presence of selected tables
is insufficient. Use SQLite metadata, relevant PRAGMAs, stored schema SQL where
needed for constraints and triggers, and application-row validation to enforce
the complete foundation contract.

### Layered domain validation

After the shared foundation validator passes, each surface adds only its
authorized checks within the same caller-owned snapshot:

- Startup performs no global history, provider, memory-data, or Trace
  projection. It does perform the startup-only SQLite integrity and foreign-key
  checks. A historical turn that cannot satisfy the current Trace projection
  does not block startup.
- Participant directory validates active membership rows and the directory
  response shape. It does not inspect memory data, provider history, message
  presentation, or Trace data.
- Message history validates the routing mode, sender, destination, legacy and
  explicit system-message rules, immutable display snapshots, and supported
  presentation shape for the messages it returns.
- Trace validates only the selected turn, the events and participant
  configurations referenced by that turn, and that turn's recorded memory
  audit. It does not validate unrelated turns and does not query current memory
  tables.
- Room-post and Helios-turn services retain their own write-path and
  provider-history checks for the transaction they are processing.

Selected-domain inconsistencies use that domain's existing sanitized failure
contract. They do not automatically reclassify the entire database as startup
incompatible.

The intentionally preserved historical Turn 11 remains valid canonical data.
Its failure under the strict positional Trace contract must occur only when
Turn 11 is traced. It must not prevent server startup, directory reads, message
history, unrelated Trace reads, Room posts, or later Helios turns.

### Mature-database cardinality contract

Foundation validation must allow a mature database rather than imposing the
fresh-seed row counts.

Require:

- exactly one room with stable `room_key = main` and name `The Room`; its
  existing stable row identity must be preserved
- exactly one participant for each stable key `peter`, `helios`, and
  `room-system`, with the approved participant types
- exactly one active `main` membership for Peter
- exactly one active `main` membership for Helios
- no active membership for `room-system`
- the alias, primary-projection, bootstrap-event, and route foundation rules
  defined above

Allow:

- additional valid participants
- active memberships for additional valid participants
- historical memberships, including joined and left records
- multiple valid immutable participant configurations and configuration
  history; do not require the fresh database's single `initial` configuration
- completed, open, failed, cancelled, human-only, Room-only, and legitimately
  stranded turns
- turns without a provider event or AI response when permitted by their state
  and canonical history
- historical records that remain canonical but fail a narrowly requested
  current domain projection

Every canonical message still requires exactly one valid immutable route, but
no rule may infer that every turn must contain a provider event, AI response,
memory retrieval, or successful Trace projection. Domain-specific
inconsistencies fail only when the corresponding domain reads or processes
them, using its approved sanitized error.

### SQLite read-only and sidecar contract

`mode=ro` and `PRAGMA query_only = ON` prohibit application SQL writes, but a
SQLite connection to a WAL-backed database may still interact with WAL and
shared-memory sidecars. This SOW therefore guarantees:

- main database bytes and logical content remain unchanged
- WAL bytes remain unchanged
- schema, migration history, journal mode, and canonical rows remain unchanged
- no checkpoint request
- no initialization, seed, migration, repair, or cleanup write
- no application-directed creation, deletion, rename, truncation, replacement,
  or explicit write of the selected database or any sidecar

It does not claim that SQLite itself can never access or alter a shared-memory
sidecar as part of opening a supported WAL database. Do not use `immutable=1`
against a database that may have committed state in an uncheckpointed WAL.

An existing SHM file may undergo only SQLite-managed lock or read-mark changes
caused by the read-only connection. Its existence must remain unchanged, and
the application must not open the SHM path directly for writing, truncate it,
replace it, rename it, delete it, or interpret permitted SQLite lock/read-mark
changes as application data.

Creation of a missing SHM file is not acceptable. Before opening a WAL-backed
snapshot, preflight must determine whether the state can be read safely without
creating a missing SHM sidecar. If a safe snapshot requires SHM creation, fail
closed with `database_schema_incompatible` before SQLite creates it. Do not
create and then remove an SHM file as a rollback strategy.

Tests must cover supported rollback-journal and WAL states. They must require
byte equality for the main database and WAL, plus unchanged schema, migration
history, journal mode, and canonical rows. For an existing SHM file, tests must
preserve its existence and distinguish permitted SQLite-managed lock/read-mark
changes from forbidden application-directed mutation; unconditional SHM byte
equality is not required. A missing SHM must remain missing. Any state that
cannot satisfy these rules must fail closed without `immutable=1`, checkpoint,
copy, sidecar creation, or repair.

### Preflight snapshot and post-check race

The preflight accepts one consistent read-transaction snapshot. After the
connection closes and before or while Uvicorn runs, another process could
replace or modify the selected database. Therefore, this SOW does not claim
that preflight alone makes later incompatibility impossible.

The participant directory, message history, Trace, Room-post, and Helios-turn
paths must retain their own schema and domain validation on their own
consistent snapshots and fail closed if the database changes after startup.
Do not remove later validation merely because startup preflight passed.

The preflight must perform no SQL write, create no main SQLite database file,
and invoke none of these behaviors:

- `initialize_database()`
- `migrate_database()`
- schema installation
- seed insertion
- WAL checkpointing
- repair or cleanup

If the preflight fails, `uvicorn.run()` must not be called and no listening
socket may be created.

### Exact startup failures

Write one sanitized JSON object to standard error and exit with status `1`.
Do not print a traceback in routine CLI operation.

Missing path or nonexistent database:

```json
{
  "error": "database_not_initialized",
  "message": "The Helios Room database is not initialized."
}
```

Exact schema v1.2 database that requires the approved migration:

```json
{
  "error": "database_migration_required",
  "message": "Database schema 1.2 must be migrated to 1.3 before the server can start."
}
```

Any empty, malformed, partially migrated, newer, divergent, or otherwise
unsupported database:

```json
{
  "error": "database_schema_incompatible",
  "message": "The Helios Room database schema is incompatible with this application."
}
```

These failures must not expose the requested path, SQL, table or column names,
SQLite exception text, environment variables, secrets, canonical message text,
or participant configuration.

The exact v1.2 classification is allowed only when the migration history is
exactly `[(1, "1.2")]` and the database passes the complete shared v1.2 source
validator. A spoofed or malformed database containing only that migration row
must return `database_schema_incompatible`, not
`database_migration_required`.

## 2. Preserve the explicit migration boundary

Startup must never offer a confirmation prompt and must never migrate
automatically. The only supported upgrade remains the explicit CLI command:

```cmd
python -m app.main migrate-database --database data\helios.db
```

That command remains subject to the backup, stopped-server, source-validation,
transaction, integrity, and rollback requirements in the approved v1.3 SOW.

`init-db` must continue to create a fresh v1.3 database only when the target has
no existing user-defined schema. It must not upgrade v1.2.

## 3. Browser read-failure presentation

Keep the approved safe API error contracts unchanged. Improve only the browser
handling and presentation.

For both `GET /api/messages` and `GET /api/participants`:

1. Attempt to parse the JSON error body when the response is non-successful.
2. Never render the response's unrestricted `message` value.
3. Match only the response `error` code against a local browser allowlist and
   display the exact locally defined literal for that code.
4. For an unrecognized code, missing code, invalid JSON, or non-JSON response,
   display the endpoint's exact generic fallback. An unexpected response
   `message` paired with a recognized code is ignored and cannot alter the
   locally mapped literal.
5. For a network failure, display:

   ```text
   Cannot reach the Helios Room server.
   ```

6. Do not display raw exception text, response bodies, HTML, URLs, paths, SQL,
   submitted message text, participant configuration, or secret values.
7. Do not add a failed request, local command, or error string to canonical
   message history.

The browser's local literal allowlist is exactly:

```text
message_history_invalid
The message history data is invalid.

message_history_unavailable
The message history is unavailable.

participant_directory_invalid
The participant directory data is invalid.

participant_directory_unavailable
The participant directory is unavailable.
```

The exact generic fallbacks are:

```text
GET /api/messages
Failed to load messages. Please refresh.

GET /api/participants
Participant directory unavailable.
```

Use two independent accessible status elements: one for message-history errors
and one for participant-directory errors. Each must have an appropriate
live-region role so a screen reader announces a newly displayed failure
without moving keyboard focus. Do not reuse the Trace status or command status
for these read failures.

On a later successful history refresh, clear only the history error. On a later
successful participant refresh, clear only the participant error. A successful
request on one endpoint must not clear a still-relevant error from the other.
A participant-directory failure must keep sending disabled because no
authoritative destination can be selected. A message-history failure must not
erase or fabricate canonical messages.

## 4. Participant and destination visual finish

Retain the existing purple gradient, white room container, simple typography,
and responsive behavior. Do not perform a general redesign.

Apply a finished visual treatment to:

- participant panel heading
- Refresh button
- participant entries and disabled states
- participant aliases and availability labels
- destination button
- destination search field
- destination options, hover, active, selected, focus, and disabled states
- mobile participant toggle and drawer
- room-status text

Requirements:

- No new control should appear as an unstyled browser-default button.
- Refresh must be visually secondary and smaller than Send.
- The destination button must align cleanly with the composer and remain
  distinguishable from the message input.
- Peter, Helios, and Room must be readable without relying on color alone.
- All interactive controls need visible `:focus-visible` treatment.
- Disabled controls must remain legible and unmistakably disabled.
- Pointer targets must be at least 40 CSS pixels high on touch layouts.
- Long valid aliases must wrap or truncate safely without expanding the room
  beyond its container.
- The picker must not be clipped by the composer or participant panel.
- Existing desktop and narrow-screen behavior must remain usable at 200%
  browser zoom.
- Do not add decorative animation beyond the existing short drawer transition.

The screenshot exposed presentation quality, not a requirement to change the
underlying participant-directory or destination behavior.

## 5. Static-asset freshness

Ensure an ordinary hard refresh reliably loads a matching `index.html`,
`app.js`, and `style.css` after deployment.

Use one simple buildless mechanism, such as explicit version query strings in
`index.html`, and update the JavaScript and stylesheet tokens together for this
correction. Do not introduce Node, npm, a bundler, generated asset manifests,
or hashed-build output.

The root HTML response must use `Cache-Control: no-store`. API responses must
retain their approved `no-store` behavior.

## 6. Runtime and test tooling boundary

Helios Room runtime requirements remain Python, FastAPI, Uvicorn, SQLite, and a
modern browser. Node must not be added to `requirements.txt`, startup checks,
or runtime instructions.

Node may remain an optional way to execute the standalone JavaScript tests.
Document it as developer-only tooling. Absence of Node must not prevent:

- database initialization
- explicit database migration
- server startup with a compatible database
- use of the browser UI

Do not weaken or delete JavaScript tests because Node was unavailable in one
implementation environment.

## 7. Replace the contradictory existing serve test

The current `tests/test_trace.py` suite contains a baseline test requiring the
`serve` command to select a missing database path without opening it and still
call Uvicorn. That behavior is intentionally superseded by this corrective
SOW.

Replace that assertion with the startup-preflight contract. Do not keep both
expectations and do not weaken the new preflight merely to preserve the old
test.

The retained missing-path invariant is:

- the missing database and parent directory are not created
- the exact `database_not_initialized` JSON is written to standard error
- the process exits with status `1`
- `uvicorn.run()` is never called

All unrelated Trace tests and serve argument-selection coverage must remain.

## Implementation guidance

Likely affected files include:

- `app/main.py`
- `app/database.py` or a narrowly scoped new preflight module
- `static/index.html`
- `static/app.js`
- `static/style.css`
- relevant Python API, CLI, and database tests
- the contradictory `serve` expectation in `tests/test_trace.py`
- `tests/test_trace_ui.js` or a separate focused UI test file
- `README.md`

Use the complete pure v1.2 validator for both migration eligibility and startup
classification. Use the pure v1.3 foundation validator beneath startup,
initialization validation, directory, history, and Trace. Add global integrity
and foreign-key checks only to startup and initialization, then apply each
surface's bounded domain validation and approved public error contract. Do not
create a permissive generic connection helper that can accidentally create a
missing database during validation.

## Required automated tests

Use only temporary databases.

### Startup and database tests

- a valid fresh v1.3 database passes preflight and permits the mocked
  `uvicorn.run()` call
- a valid migrated temporary v1.3 database passes preflight
- the v1.2 migration path and startup classification call the same complete
  pure v1.2 source validator
- startup, initialization validation, directory, history, and Trace call the
  same pure v1.3 foundation validator
- startup and initialization run global integrity and foreign-key checks in
  the same snapshot; directory, history, and Trace do not run those global
  checks or traverse unrelated domain storage
- exact valid v1.2 returns the exact `database_migration_required` JSON and
  exit status `1`
- valid Peter and Helios adopted current aliases pass v1.3 validation when
  ownership and name history are correct
- changing Peter or Helios current primary alias back to the bootstrap alias is
  also accepted when the history is valid
- v1.2 refusal makes no database-content, schema, journal-mode, checkpoint,
  initialization, migration, or repair write
- supported rollback-journal and WAL states preserve exact main-database and
  WAL bytes, schema, migration history, journal mode, and canonical rows
- an existing SHM remains present and any byte changes are limited to
  SQLite-managed lock/read-mark behavior, with no application-directed sidecar
  mutation
- a missing SHM remains missing; any WAL state that would require SHM creation
  fails closed before creation
- unsafe or unsupported WAL/sidecar states fail closed without `immutable=1`,
  checkpointing, sidecar creation, copying, or repair
- missing database returns `database_not_initialized` and creates no file or
  parent directory
- empty SQLite database returns `database_schema_incompatible`
- malformed migration history returns `database_schema_incompatible`
- partial v1.3 schema returns `database_schema_incompatible`
- unsupported newer migration history returns `database_schema_incompatible`
- spoofed v1.2 migration metadata without the complete v1.2 source shape
  returns `database_schema_incompatible`
- v1.2 violations of each migration-source category, including integrity,
  foreign keys, identity, reserved names, alias collisions, room graph, and
  historical message shapes, return `database_schema_incompatible`
- v1.3 missing or divergent tables, columns, views, indexes, constraints, or
  safety triggers return `database_schema_incompatible`
- v1.3 invalid foundation alias, primary-projection, bootstrap-event, route, or
  `room-system` cardinality returns `database_schema_incompatible`
- additional valid participants and active or historical memberships do not
  fail startup
- multiple valid immutable participant configurations do not fail startup
- legitimate completed, open, failed, cancelled, human-only, Room-only, and
  stranded turns do not fail startup
- a turn without a provider event or AI response does not fail startup merely
  for that absence
- intentionally preserved Turn 11 passes startup foundation validation and
  fails closed only when Turn 11 is requested through Trace
- a Trace-invalid unrelated turn does not break directory, message-history, or
  unrelated-turn Trace reads
- directory tests prove no current memory table, provider history, provider
  prompt, Trace-history query, or global integrity/foreign-key scan is issued
- Trace tests prove only the selected turn and its referenced configuration,
  events, and recorded memory audit are validated, and current memory tables
  are never queried
- message-history tests apply supported-history checks without requiring every
  turn to have a provider event, AI response, or successful Trace projection
- each selected-domain inconsistency returns that domain's existing sanitized
  error rather than globally reclassifying startup
- busy or unreadable database returns `database_schema_incompatible` without
  exception leakage
- every failure proves `uvicorn.run()` was not called
- secret sentinels in paths, exception text, and malformed database values do
  not appear in standard output or standard error
- the old missing-database serve assertion is replaced, while unrelated Trace
  behavior remains covered
- after a valid preflight, replacing or corrupting the temporary database makes
  later directory, history, Trace, Room-post, and turn paths fail closed rather
  than trust stale startup acceptance

### Real-ASGI browser/API contract tests

- history and directory API success responses retain `Cache-Control: no-store`
- approved history and directory failure responses retain their exact status,
  code, message, and `no-store` headers
- root HTML uses `Cache-Control: no-store`
- HTML references the matching corrected JavaScript and stylesheet versions

### JavaScript tests

- every recognized API error code displays its exact locally defined literal
- a recognized code paired with attacker-controlled response `message` text
  displays the local literal and never the response text
- unrecognized, missing-code, invalid-JSON, and non-JSON history errors
  display exactly `Failed to load messages. Please refresh.`
- unrecognized, missing-code, invalid-JSON, and non-JSON directory errors
  display exactly `Participant directory unavailable.`
- network failure displays `Cannot reach the Helios Room server.`
- history and directory errors use independent accessible status elements
- a successful history refresh clears only the history error
- a successful directory refresh clears only the directory error
- directory failure clears the selected destination and disables Send
- errors never create a canonical-looking message element
- `/participants`, `[`, destination selection, and Trace behavior remain intact
- focus-visible classes or selectors are present for every new interactive
  control
- narrow-layout drawer and picker state remain keyboard operable

## Manual acceptance checks

All manual checks occur only after implementation review and only with an
approved temporary or safely backed-up and migrated database.

1. With a temporary v1.2 database, run `serve` and confirm that the process
   exits before announcing a server URL.
2. Confirm the exact migration-required error contains no local path or
   traceback.
3. Confirm the temporary v1.2 database has no database-content, schema,
   journal-mode, checkpoint, initialization, migration, or repair change;
   confirm exact main-database and WAL byte preservation and the documented SHM
   allowance.
4. With a temporary v1.3 database, start the server and load the room.
5. Confirm messages and participants load without a provider request.
6. Confirm Peter, Helios, and Room display correctly.
7. Confirm Refresh and the destination control match the established visual
   language and have visible keyboard focus.
8. Confirm `[` opens the picker and Escape closes it.
9. Confirm `/participants` opens the participant panel without writing a
   message.
10. Confirm desktop, narrow-screen, and 200% zoom layouts remain usable.
11. Simulate a safe API failure and confirm the status is understandable,
    accessible, and free of internal details.
12. Hard-refresh and confirm the corrected JavaScript and CSS load together.
13. Start against a mature temporary v1.3 database containing additional
    participants, historical memberships, multiple valid configurations, and
    non-completed turns; confirm startup succeeds.
14. Confirm the intentionally preserved Turn 11 does not block startup or
    unrelated reads, while requesting Trace for Turn 11 still fails closed.
15. Confirm directory reads do not touch memory, provider, or Trace data and a
    selected Trace does not query current memory tables.

No live provider request is required for these checks.

## Documentation requirements

Update `README.md` to state clearly:

- `serve` is read-only with respect to schema readiness and refuses
  incompatible databases
- preflight validates one consistent snapshot and later services continue to
  validate their own snapshots
- the shared v1.3 foundation validator is layered beneath directory, history,
  and selected-turn Trace checks rather than projecting every historical domain
- mature databases may contain additional participants, memberships,
  configurations, and legitimate non-completed turns
- the exact main-database, WAL, existing-SHM, and missing-SHM preflight contract
- startup never migrates automatically
- the exact manual backup and migration sequence remains mandatory
- `init-db` is for fresh databases, not upgrades
- Node is optional developer tooling used only for JavaScript tests
- the application itself requires no Node installation

Do not place secrets, absolute developer-machine paths, or live database
contents in documentation or test fixtures.

## Verification and completion report

Before reporting completion, run every available offline verification:

```text
python -m unittest discover -s tests -v
python -m compileall -q app tests
git diff --check
```

Run the JavaScript suite in an environment with Node when available. If it is
not available, report that limitation exactly and do not describe the
JavaScript suite as passed. Perform the manual browser checks when an
interactive browser is available.

The completion report must include:

- actual Git base commit
- the preserved pre-existing working-tree inventory and confirmation that the
  identity-foundation implementation remained present
- files changed
- startup-preflight behavior implemented
- foundation-versus-domain validator boundaries implemented
- WAL/SHM behavior observed in every tested startup state
- test commands and exact results
- confirmation that no live provider request occurred
- confirmation that `data/helios.db`, its WAL/SHM files, and live backups were
  not opened, inspected, migrated, or modified
- any unavailable verification environment
- confirmation that no checkout, reset, clean, restore, stash, rebase, commit,
  push, or live migration was performed unless Peter separately authorized the
  specific operation

## Acceptance criteria

This corrective milestone is complete only when all of the following are true:

- `serve` cannot bind or serve static files when its consistent preflight
  snapshot is schema v1.2
- valid v1.2 receives the exact sanitized migration-required CLI contract
- missing, malformed, partial, divergent, and newer databases fail closed
- the complete v1.2 source validator and bounded v1.3 foundation validator are
  pure, shared, and read-only; preflight runs the applicable validator within
  one caller-owned read transaction, while migration may run the v1.2 validator
  inside its caller-owned `BEGIN IMMEDIATE` before the first write
- startup and initialization add global integrity and foreign-key checks, while
  directory, history, and Trace do not traverse unrelated domain storage
- v1.3 foundation validation covers required schema objects, constraints,
  guards, core identity and name state, and message-route cardinality and
  ownership, not every historical domain projection
- directory, message history, Trace, Room-post, and Helios-turn validation are
  layered and limited to their authorized data and selected domain
- startup does not require every historical turn to be Trace-valid and the
  intentionally preserved Turn 11 does not block startup
- mature valid databases may include additional participants and memberships,
  multiple configurations, and legitimate completed or non-completed turn
  states
- valid adopted primary aliases for Peter and Helios are accepted
- startup validation performs no database-content, schema, journal-mode,
  checkpoint, initialization, migration, repair, or application-directed
  filesystem write; main and WAL bytes remain unchanged, existing SHM changes
  are limited to SQLite-managed lock/read marks, and missing SHM is never
  created
- valid v1.3 still starts normally
- later read and write services continue to validate their own snapshots and
  fail closed after post-preflight database change
- browser read errors use exact local literal allowlists and independent,
  accessible history and directory statuses
- participant and destination controls have a finished, consistent visual
  treatment
- static JavaScript and CSS revisions cannot be accidentally mixed after an
  ordinary hard refresh
- Node remains optional and absent from runtime dependencies
- all existing identity, routing, history, Trace, memory, API, and migration
  tests continue to pass except the explicitly superseded missing-database
  serve assertion, which is replaced by the new preflight contract
- the pre-existing uncommitted identity-foundation working tree is preserved
- the live database remains untouched during implementation and automated
  verification
