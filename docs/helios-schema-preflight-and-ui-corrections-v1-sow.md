# Statement of Work: Schema Preflight and Participant UI Corrections v1

## Document status

This is a corrective SOW following implementation of Participant Identity and
Addressing Foundation v1 against GitHub `main` commit:

```text
65c932632acad3f1146db8e6cfd397cb9aa67b01
```

It does not replace or reopen the approved identity, alias, routing, or schema
v1.3 contracts in Participant Identity and Addressing Foundation v1 SOW
revision 3. It adds deployment-safety and browser-finish requirements exposed
by the first local launch of the implementation.

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

Make an incompatible database impossible to serve accidentally, make runtime
read failures understandable without leaking internal data, and finish the
visual integration of the participant and destination controls.

After this correction:

1. `serve` starts only when the selected database is a compatible schema v1.3
   Helios Room database.
2. A schema v1.2 database produces an exact migration-required CLI error before
   Uvicorn binds a socket or serves static files.
3. Startup validation never initializes, migrates, repairs, or otherwise writes
   the selected database.
4. Browser read failures use stable, accessible status presentation and do not
   expose exceptions or submitted data.
5. The participant panel and destination picker look like intentional parts of
   the existing interface.
6. Node remains optional developer tooling and is not required to run Helios
   Room.

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
- shared schema-version validation where appropriate
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

Add one shared startup-preflight service and call it synchronously from the
`serve` CLI path before `uvicorn.run()`.

The preflight must:

1. Resolve the requested database path without creating the file or its parent
   directory.
2. Require an existing regular database file.
3. Open it using a SQLite read-only URI and set `PRAGMA query_only = ON`.
4. Apply the existing busy timeout.
5. Read and validate `schema_migrations` without invoking initialization or
   migration code.
6. Accept only the exact ordered migration history required by the current
   application:

   ```text
   (1, "1.2")
   (2, "1.3")
   ```

7. Verify that the minimal required v1.3 objects used at startup exist. At a
   minimum this includes the tables needed for rooms, participants, active
   membership, aliases, primary aliases, name events, messages, routes, turns,
   configurations, memory, and trace reads.
8. Verify the required `main`, `peter`, `helios`, and `room-system` bootstrap
   identities using the same fail-closed identity rules already used by the
   v1.3 services.
9. Close the connection before Uvicorn starts.

The preflight must perform no SQL write, create no SQLite file, and invoke none
of these behaviors:

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
exactly `[(1, "1.2")]` and the database satisfies the approved v1.2 source
shape. A spoofed or malformed database containing only that migration row must
return `database_schema_incompatible`, not `database_migration_required`.

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
2. Display only a recognized server `message` from an approved error code.
3. For an unrecognized payload or invalid JSON, display the existing stable
   generic message.
4. For a network failure, display:

   ```text
   Cannot reach the Helios Room server.
   ```

5. Do not display raw exception text, response bodies, HTML, URLs, paths, SQL,
   submitted message text, participant configuration, or secret values.
6. Do not add a failed request, local command, or error string to canonical
   message history.

Use the existing status region or a small dedicated room-status region. It
must have an appropriate accessible live-region role so a screen reader
announces a newly displayed failure without moving keyboard focus.

On a later successful refresh, clear the corresponding stale error. A
participant-directory failure must keep sending disabled because no
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

## Implementation guidance

Likely affected files include:

- `app/main.py`
- `app/database.py` or a narrowly scoped new preflight module
- `static/index.html`
- `static/app.js`
- `static/style.css`
- relevant Python API, CLI, and database tests
- `tests/test_trace_ui.js` or a separate focused UI test file
- `README.md`

Prefer one schema-compatibility implementation shared by startup and existing
read services, while preserving each surface's approved public error contract.
Do not create a permissive generic connection helper that can accidentally
create a missing database during validation.

## Required automated tests

Use only temporary databases.

### Startup and database tests

- a valid fresh v1.3 database passes preflight and permits the mocked
  `uvicorn.run()` call
- a valid migrated temporary v1.3 database passes preflight
- exact valid v1.2 returns the exact `database_migration_required` JSON and
  exit status `1`
- v1.2 refusal leaves the entire database byte-for-byte unchanged when SQLite
  permits a stable byte comparison and otherwise proves no schema, row,
  migration, journal-mode, or data-version change
- missing database returns `database_not_initialized` and creates no file or
  parent directory
- empty SQLite database returns `database_schema_incompatible`
- malformed migration history returns `database_schema_incompatible`
- partial v1.3 schema returns `database_schema_incompatible`
- unsupported newer migration history returns `database_schema_incompatible`
- spoofed v1.2 migration metadata without the complete v1.2 source shape
  returns `database_schema_incompatible`
- busy or unreadable database returns `database_schema_incompatible` without
  exception leakage
- every failure proves `uvicorn.run()` was not called
- secret sentinels in paths, exception text, and malformed database values do
  not appear in standard output or standard error

### Real-ASGI browser/API contract tests

- history and directory API success responses retain `Cache-Control: no-store`
- approved history and directory failure responses retain their exact status,
  code, message, and `no-store` headers
- root HTML uses `Cache-Control: no-store`
- HTML references the matching corrected JavaScript and stylesheet versions

### JavaScript tests

- recognized API errors display only the approved safe message
- unrecognized and non-JSON errors display the stable generic message
- network failure displays `Cannot reach the Helios Room server.`
- a later successful refresh clears the stale corresponding error
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
3. Confirm the temporary v1.2 database remains unchanged.
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

No live provider request is required for these checks.

## Documentation requirements

Update `README.md` to state clearly:

- `serve` is read-only with respect to schema readiness and refuses
  incompatible databases
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

- actual starting commit
- files changed
- startup-preflight behavior implemented
- test commands and exact results
- confirmation that no live provider request occurred
- confirmation that `data/helios.db`, its WAL/SHM files, and live backups were
  not opened, inspected, migrated, or modified
- any unavailable verification environment
- confirmation that no commit, push, or live migration was performed unless
  Peter separately authorized it

## Acceptance criteria

This corrective milestone is complete only when all of the following are true:

- `serve` cannot bind or serve static files against schema v1.2
- valid v1.2 receives the exact sanitized migration-required CLI contract
- missing, malformed, partial, divergent, and newer databases fail closed
- startup validation performs no database or filesystem mutation
- valid v1.3 still starts normally
- browser read errors are safe, stable, accessible, and clear
- participant and destination controls have a finished, consistent visual
  treatment
- static JavaScript and CSS revisions cannot be accidentally mixed after an
  ordinary hard refresh
- Node remains optional and absent from runtime dependencies
- all existing identity, routing, history, Trace, memory, API, and migration
  tests continue to pass
- the live database remains untouched during implementation and automated
  verification

