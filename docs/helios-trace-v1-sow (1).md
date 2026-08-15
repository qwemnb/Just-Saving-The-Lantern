# Statement of Work: Helios Room Trace v1

## Objective

Add a local, read-only `/trace` command to `qwemnb/Helios-Room` so Peter can inspect what happened during a recorded turn without creating another turn or contacting OpenAI.

The milestone is complete when:

1. Peter enters `/trace` to inspect the latest turn, or `/trace <turn_id>` to inspect a specific turn.
2. The browser intercepts the command locally and opens a readable trace panel.
3. The trace is reconstructed only from the historical rows already recorded for that turn.
4. The command creates no canonical message, turn, room sequence, API event, admin event, or provider call.
5. The panel shows canonical messages, exact configuration provenance, the sanitized recorded provider request, the response or error event, timing, provider status, and token usage when available.

Trace v1 is observability for the room's existing flight recorder. It is not a new source of room history and it must never rerun the operation it is inspecting.

## Starting Point

Begin from the latest `main` branch. The reviewed baseline is:

```text
102f6c59b9fdbe069a006ae14070ffb106f700e1
```

If `main` has advanced, inspect and preserve the newer work. Do not reset, rewrite history, or discard local changes.

The baseline already contains:

- SQLite schema v1.2
- canonical `turns` and `messages`
- immutable `participant_configs` once used
- request, response, and error records in `api_events`
- a three-phase, one-request OpenAI turn service
- a browser UI that submits Peter messages to `POST /api/messages`

## Schema Decision

Keep schema v1.2 unchanged.

Trace v1 needs only existing data from:

- `rooms`
- `participants`
- `turns`
- `messages`
- `participant_configs`
- `api_events`

Do not add a migration, table, column, trigger, index, or view.

Although schema v1.2 contains `admin_events`, Trace v1 must not write an admin event. A read-only inspection command must be side-effect-free. Future slash commands that mutate state should use the appropriate audit mechanism.

## Settled Behavioral Decisions

- `/trace` means the newest turn in room `main`, including an open, failed, completed, or cancelled turn.
- `/trace <turn_id>` means that exact canonical decimal turn ID from `1` through `9223372036854775807`, but only if it belongs to room `main`.
- Resolve the newest turn deterministically with `ORDER BY turns.id DESC LIMIT 1`.
- Trace commands are local control input, not canonical conversation.
- Read historical request input from the recorded request API event. Never reconstruct it from current room history.
- Read historical provider output or error from the recorded terminal API event. Never contact the provider to fill missing data.
- Support completed, failed, cancelled, open, stranded, and older human-only turns, including turns with no configuration or API events.
- Use one unconditionally read-only SQLite snapshot so all returned sections describe the same database state.
- Run SQLite work through a thread boundary so the FastAPI event loop is not blocked.
- Do not load `.env`, instantiate an OpenAI client, call `run_helios_turn`, retrieve memory, or allocate sequence numbers.
- Do not expose raw chain-of-thought. Reasoning token counts and explicitly recorded provider reasoning summaries may be shown, but raw or opaque reasoning content must be omitted.
- Trace v1 inherits the application's local, single-user trust model. Do not add authentication in this milestone, but document that trace endpoints expose private room history and must not be placed on an untrusted network.

## Required Implementation

### 1. Trace command grammar

Recognize these forms after trimming only for command parsing:

```text
/trace
/trace 17
```

Use behavior equivalent to this grammar:

```text
^/trace(?:\s+([1-9][0-9]*))?\s*$
```

Requirements:

- Leading and trailing whitespace may be ignored for recognizing the command.
- Recognition is case-sensitive. Only lowercase `/trace` is reserved; `/TRACE` and other case variants remain ordinary message text.
- A specific turn ID must be a positive base-10 integer.
- `/tracefoo` is not a trace command and must retain normal message behavior.
- If the first whitespace-delimited token is exactly `/trace` but its arguments are invalid, show this local usage message and do nothing else:

```text
Usage: /trace [positive turn ID]
```

- A recognized or malformed trace command must never be sent to `POST /api/messages`.
- Do not trim or otherwise change ordinary non-command Peter messages.

Implement one pure command-classification function with no environment, database, network, or UI side effects. It must distinguish:

- non-command input
- a valid latest-turn trace command
- a valid specific-turn trace command and its decimal ID text
- a malformed trace command whose first whitespace-delimited token is exactly `/trace`

Reuse this classifier in the browser-equivalent server guard and the CLI canonical-write guard. The browser may implement equivalent parsing in JavaScript, but its behavior must be tested against the same cases.

### 2. Defense at every canonical write boundary

The browser is the normal command dispatcher, but the server must also prevent a trace command from becoming canonical if a client bypasses or breaks the UI.

In `run_helios_turn()`, immediately after the existing blank-message handling and before environment loading:

1. Preserve the existing blank-message behavior first.
2. Detect a first token of exactly `/trace` after whitespace trimming.
3. Reject it with HTTP `400` and stable error code `local_command_only` before environment loading, database preflight, configuration creation, or provider construction.

The response message should tell the client to use the local trace interface. This guard applies to valid and malformed `/trace` arguments. It must create no row in any table and make no provider call.

The `store-message --message ...` CLI path bypasses `run_helios_turn()`. Before it opens or initializes the database, it must call the same pure classifier and reject both valid and malformed `/trace` input with a stable nonzero exit and a concise local-command message. It must not execute trace from the CLI, create a database or parent directory, create any row, or consume a sequence number.

Do not reserve every slash-prefixed message. Unknown commands and text such as `/tracefoo` remain outside this milestone.

### 3. Read-only HTTP endpoints

Add:

```text
GET /api/trace/latest
GET /api/trace/{turn_id}
```

Requirements:

- Accept `{turn_id}` only when the raw path segment is canonical decimal text for an integer from `1` through SQLite's maximum row ID, `9223372036854775807`.
- Receive and validate the dynamic path segment as raw text in application code. Do not rely on FastAPI integer coercion, whose generic validation responses would violate the stable error contract.
- Both endpoints inspect only room key `main`.
- `latest` selects the highest turn ID in that room, regardless of status.
- Register the literal `/latest` route before the dynamic route and verify through the real ASGI router that it takes precedence over `{turn_id}`.
- A turn ID from another room must be treated as not found.
- Use a shared trace service rather than duplicating query and projection logic across routes.
- Call the synchronous SQLite reader through `asyncio.to_thread` or an equivalent thread boundary.
- Return JSON only. Do not render server-side HTML.
- Resolve the database path for both routes exclusively from `request.app.state.database_path`, matching the server-selected database used by the existing application routes.
- Set `Cache-Control: no-store` on every success and error response from both trace routes.

Stable error behavior:

| Condition | HTTP | Error code |
| --- | ---: | --- |
| No turns exist for `/api/trace/latest` | 404 | `trace_not_found` |
| Requested turn does not exist in `main` | 404 | `trace_not_found` |
| Turn ID is zero, negative, non-decimal, non-canonical, or above `9223372036854775807` | 400 | `invalid_trace_turn_id` |
| Database cannot be opened or required schema is unavailable | 503 | `trace_database_unavailable` |
| Recorded JSON or relational trace data is internally inconsistent | 500 | `trace_data_invalid` |

Do not include unrestricted exception text, SQL, paths, environment values, or credentials in errors.

### 4. Server-selected database integration

The baseline parses `serve --database`, but the `serve` branch does not assign that value to application state. This milestone explicitly authorizes the smallest integration fix required so all server routes inspect and write the same operator-selected database:

- Before Uvicorn begins serving requests, assign the resolved `--database` path to `app.state.database_path` through the application's established configuration path.
- Preserve the current default when `--database` is omitted.
- Do not open, initialize, migrate, copy, or reset the database as part of assignment.
- Add an integration test proving `serve --database <temporary path>` makes the trace routes use that exact path.
- Do not broaden this into a configuration redesign.

### 5. Unconditionally read-only historical snapshot

For one trace request:

1. Refuse a missing database or missing parent directory without creating either.
2. Convert the selected database path to a properly escaped SQLite file URI and open the existing file with `mode=ro` and a private cache. Never call the existing `connect_database()`, which may create directories or an empty database.
3. Enable `PRAGMA query_only = ON` on that dedicated trace connection.
4. Execute `BEGIN` before the first schema, migration-marker, target-turn, or trace-data read.
5. Within that same snapshot, validate the schema v1.2 migration marker required by this application. A missing, wrong, or unreadable marker returns `trace_database_unavailable`.
6. Load the turn and its room and nullable initiator identity.
7. Load every canonical message whose `turn_id` and `room_id` match the turn, plus the identity needed to validate any same-room cross-turn message reference.
8. Load every participant configuration referenced by those messages or by the turn's API events.
9. Load every API event for the turn, plus the identity needed to validate any same-room cross-turn related-message reference.
10. End every successful or failed read with `ROLLBACK`, then close the connection. Never call `commit`.

Ordering must be deterministic:

- messages by `turn_sequence_no`, then `id`
- configurations by `id`
- API events by `sequence_no`, then `id`

Every trace query must use that one connection and transaction. The trace loader must issue no `INSERT`, `UPDATE`, `DELETE`, DDL, or sequence-allocation call. It must not attach another database, use a shared cache, call `commit`, or create an `admin_events` record. A direct write attempted through the trace connection must fail.

Validate enough relational consistency to avoid presenting a misleading trace. At minimum:

- every returned record belongs to the selected turn and room where applicable
- referenced participant and configuration identity is retained
- JSON columns parse successfully
- an API event's participant configuration, when present, belongs to that participant
- duplicate API event sequence numbers or duplicate turn message sequence numbers fail safely, even though schema constraints should prevent them
- `messages.reply_to_id` and `api_events.related_message_id` may legally refer to a message in another turn. The target must exist in the selected room; preserve its ID and target turn ID, and mark that it is outside the selected turn rather than returning `trace_data_invalid`.

Do not require a request event or terminal event. Older human-only turns and stranded open turns are valid trace subjects.

### 6. Versioned trace response

Return a stable, versioned JSON object with this shape or a semantically equivalent typed representation:

```json
{
  "trace_version": 1,
  "turn": {
    "id": 17,
    "status": "completed",
    "created_at": "2026-08-11T23:00:00.000Z",
    "completed_at": "2026-08-11T23:00:02.000Z",
    "room": {
      "id": 1,
      "room_key": "main",
      "name": "The Room"
    },
    "initiated_by": {
      "id": 1,
      "participant_key": "peter",
      "name": "Peter",
      "participant_type": "human"
    }
  },
  "messages": [
    {
      "id": 31,
      "room_sequence_no": 31,
      "turn_sequence_no": 1,
      "participant_id": 1,
      "participant_key": "peter",
      "participant_name": "Peter",
      "participant_config_id": null,
      "reply_to_id": null,
      "reply_to_turn_id": null,
      "reply_to_outside_selected_turn": false,
      "message_type": "chat",
      "message_text": "Hello",
      "created_at": "2026-08-11T23:00:00.000Z"
    }
  ],
  "configurations": [
    {
      "id": 3,
      "participant_id": 2,
      "participant_key": "helios",
      "provider": "openai",
      "model": "gpt-5.6-luna",
      "config_label": "minimal-openai-luna-v1",
      "system_instructions": "<exact persisted instructions>",
      "settings": {
        "max_output_tokens": 2048,
        "reasoning": {
          "context": "current_turn",
          "effort": "low"
        },
        "store": false
      },
      "tools": [],
      "omitted_json_pointers": [],
      "created_at": "2026-08-11T23:00:00.000Z"
    }
  ],
  "recorded_request": {
    "event_id": 7,
    "event_sequence_no": 1,
    "request": {
      "model": "gpt-5.6-luna",
      "instructions": "<exact recorded instructions>",
      "input": [
        {
          "role": "user",
          "content": "Hello"
        }
      ],
      "store": false,
      "reasoning": {
        "context": "current_turn",
        "effort": "low"
      },
      "max_output_tokens": 2048,
      "tools": []
    },
    "local_context": {
      "provider": "openai",
      "operation": "responses.create",
      "trigger_message_id": 31,
      "room_sequence_boundary": 31,
      "timeout_seconds": 120,
      "max_retries": 0
    }
  },
  "provider_outcome": {
    "event_id": 8,
    "event_type": "openai.responses.response",
    "response_id": "resp_...",
    "status": "completed",
    "requested_model": "gpt-5.6-luna",
    "resolved_model": "<provider returned model>",
    "usage": {
      "input_tokens": 100,
      "output_tokens": 30,
      "total_tokens": 130,
      "output_tokens_details": {
        "reasoning_tokens": 10
      }
    },
    "incomplete_details": null,
    "error": null
  },
  "api_events": [
    {
      "id": 7,
      "sequence_no": 1,
      "event_type": "openai.responses.request",
      "participant_id": 2,
      "participant_key": "helios",
      "participant_config_id": 3,
      "related_message_id": 31,
      "related_message_turn_id": 17,
      "related_message_outside_selected_turn": false,
      "tool_invocation_id": null,
      "is_redacted": false,
      "redacted_at": null,
      "redaction_reason": null,
      "created_at": "2026-08-11T23:00:00.000Z",
      "payload": {
        "request": {},
        "local_context": {}
      },
      "omitted_json_pointers": []
    }
  ]
}
```

Response rules:

- `messages` is the canonical evidence for the selected turn.
- `configurations` contains each distinct referenced configuration once. It may be empty.
- `api_events` contains the trace-safe historical events and is authoritative.
- `turn.initiated_by` is nullable. Preserve `null` when `initiated_by_participant_id` is null.
- Configuration `provider`, `model`, `config_label`, `system_instructions`, `settings`, and `tools` are nullable exactly as schema v1.2 permits. Do not coerce stored JSON `null` into `{}` or `[]`.
- `settings_json`, `tools_json`, and `payload_json` may contain any valid JSON value, including null, booleans, numbers, strings, arrays, and objects. Preserve the value's JSON type in the trace-safe projection.
- API-event participant, configuration, related-message, and tool-invocation references are nullable. Include `tool_invocation_id` even when null.
- Preserve unknown event types and valid JSON scalars for generic timeline display.
- For every `reply_to_id` and `related_message_id`, include the target turn ID and an explicit boolean indicating whether the target is outside the selected turn. A same-room cross-turn target is valid history.
- `recorded_request` is a convenience projection of the sole recognized `openai.responses.request` event, or `null` when none exists.
- `provider_outcome` is a convenience projection of the sole recognized `openai.responses.response` or `openai.responses.error` event, or `null` when none exists.
- Build both convenience projections from the already trace-safe event payloads so omitted or redacted fields cannot reappear there.
- Do not fabricate missing fields or turn an open turn into an inferred failure.
- Preserve recorded request ordering, message text, instructions, settings, model identifiers, and event sequence numbers.
- Keep stored `payload_json` unchanged. Trace redaction is an outbound display projection, not a database rewrite.

Configuration and event omission paths are reported separately on the object whose JSON was filtered. Do not merge configuration pointers into an unrelated event's omission list.

### 7. Deterministic request and outcome selection

For the current one-provider-request milestone, enforce these historical cardinality rules after loading all API events in deterministic sequence order:

- Zero or one event with type `openai.responses.request` is valid.
- Zero or one terminal event with type `openai.responses.response` or `openai.responses.error` is valid.
- Multiple recognized request events, multiple recognized terminal events, or both a recognized response and recognized error event fail with `trace_data_invalid`.
- A recognized, unredacted event must have the expected payload shape. Do not skip a malformed recognized event and silently choose a later event.
- A deliberately redacted recognized event still counts toward cardinality, is represented honestly as redacted, and is not itself corruption merely because its payload was removed or replaced by the established redaction mechanism.
- A terminal event without a request event is valid historical data. Show the terminal event and keep `requested_model` null.
- Unknown event types remain in `api_events` and do not participate in request or terminal cardinality.

Recognize both baseline error payload shapes without guessing:

- provider or serialization failures: `{"error": {...}}`
- unusable provider results: `{"reason": ..., "response": ...}`

If a recognized unredacted error payload matches neither established shape, return `trace_data_invalid`. Project both formats through the explicit error allowlist defined below.

### 8. Defense-in-depth trace projection

The existing turn service sanitizes provider data before storage. Apply one recursive, display-only projection to every source of arbitrary JSON that can reach the browser:

- API-event payloads
- configuration settings and tools
- `recorded_request` and `provider_outcome` convenience projections
- future recorded `local_context.memory_retrieval` objects

Omit sensitive fields entirely. Do not replace them with placeholder values. Do not mutate or reserialize the stored JSON.

For secret-key matching, normalize each object key by Unicode-safe lowercasing and removing separators and punctuation so snake_case, kebab-case, HTTP header spelling, and camelCase compare consistently. At minimum, omit normalized forms corresponding to:

- `authorization` and `proxy-authorization`
- `headers`
- `cookie` and `set-cookie`
- `api_key`, `openai_api_key`, `x-api-key`, and `apiKey`
- `client_secret` and `clientSecret`
- `access_token` and `refresh_token`
- bare `password`, `secret`, and `token`
- keys ending in the normalized equivalents of `api_key`, `secret`, `password`, or singular `token`

Thus fields such as `Proxy-Authorization`, `Set-Cookie`, `X-API-Key`, `apiKey`, and `clientSecret` must be caught regardless of case. Ordinary non-secret keys such as `input_tokens`, `output_tokens`, `reasoning_tokens`, and `total_tokens` must remain visible.

Opaque or raw provider reasoning must also be excluded:

- Omit every `encrypted_content` and `reasoning_text` field recursively, regardless of spelling style or case.
- When an object is a recorded provider reasoning output item, omit its raw `content` field as well as `encrypted_content` and `reasoning_text`.
- A reasoning item may expose only explicitly recorded provider `summary` data after recursive filtering, safe structural metadata such as item type, ID, and status, and aggregate token counts recorded elsewhere.
- Label retained summary data as a provider-generated reasoning summary, never raw reasoning.
- Never decrypt, decode, summarize, transform, or infer omitted reasoning.

For every omission, record its location as an escaped RFC 6901 JSON Pointer rooted at the JSON value being projected. Escape `~` as `~0` and `/` as `~1`; address array entries by zero-based index. Include pointers for keys containing `/` or `~` and for omissions nested inside arrays. Each configuration and each API event must carry its own `omitted_json_pointers`; convenience projections must expose the omission metadata of their source rather than recomputing a less restrictive view.

Recognized error events require an additional explicit output allowlist. Return only the stable reason, error class, provider HTTP status, provider error code, provider request ID, safe summary, and allowlisted unusable-response metadata such as response ID, status, resolved model, usage, and incomplete details. Stack traces, unrestricted exception strings, local paths, SQL, environment dumps, request headers, and arbitrary provider objects must never enter the trace response even when their field names are unfamiliar.

Do not load the current API key for trace filtering. The projection is deterministic from names, shapes, and explicit allowlists. Trace v1 must not make a provider call to generate a reasoning summary.

### 9. Provider outcome projection

Build `provider_outcome` only from the recorded terminal event, not from current SDK behavior.

For `openai.responses.response`, expose when recorded:

- response ID
- status
- requested model from the request event
- provider-resolved model from the response event
- complete usage object
- incomplete details
- service tier if present
- visible output text only if already present in the recorded response

For `openai.responses.error`, expose when recorded:

- stable recorded reason
- error class
- provider HTTP status
- provider error code
- provider request ID
- safe summary
- only allowlisted sanitized unusable-response metadata already retained by the event

Do not persist or return unrestricted exception strings.

### 10. Browser trace panel

Intercept the command before the ordinary send flow displays `Helios is responding...` or calls `sendMessage`.

Command behavior:

- `/trace` fetches `GET /api/trace/latest`.
- `/trace 17` fetches `GET /api/trace/17`.
- A valid command clears the input after dispatch.
- An invalid trace command shows the usage message locally and leaves canonical history unchanged.
- Closing the panel restores focus to the message input.
- Each valid command performs exactly one GET. It performs no POST, automatic retry, polling, pending-Helios indicator, or canonical chat-history refresh.

Add an accessible modal or side panel with a visible title, close control, and scrollable body. It must:

- expose `role="dialog"`, `aria-modal="true"`, and an accessible name through `aria-labelledby` or an equivalent mechanism
- move focus to the dialog or its close control when opened
- contain keyboard interaction within the dialog while open, or make the background inert
- close on Escape
- restore focus to the message input when closed

Render these sections:

1. **Turn**: ID, status, room, initiator, timestamps, and elapsed duration when terminal.
2. **Canonical messages**: exact text, participant, message type, IDs, room sequence, turn sequence, reply target, configuration ID, and timestamp.
3. **Configurations**: every referenced configuration, including nullable provider, model, label, instructions, settings, tools, omission metadata, and creation time.
4. **Recorded provider request**: exact sanitized instructions, ordered input, request settings, and local audit context.
5. **Provider outcome**: response ID, provider status, resolved model, usage including reasoning-token count, incomplete details, or safe error data.
6. **API event timeline**: sequence number, type, timestamp, related message, redaction state, and collapsible trace-safe JSON.
7. **Memory context**: for now, show `No inherited memory retrieval was recorded for this turn.` If a future request event contains `local_context.memory_retrieval`, show the recorded object generically without querying memory tables. A dedicated memory presentation will be added by Seeded Memory Retrieval v1.

Use DOM `textContent` or equivalent safe text nodes for every database and event value. Preserve visible whitespace with `<pre>` or CSS `white-space: pre-wrap`. Do not render message text, instructions, keys, values, or JSON with `innerHTML`.

No database-derived value may be used as HTML, a URL, inline style, stylesheet selector, class name, event-handler source, DOM property name, or any other executable or structural attribute. Database-derived values are text only.

Trace usage messages and trace errors must render outside the canonical messages container so they cannot be mistaken for room history.

The panel must handle empty configurations, no API events, open turns, failed turns, and missing provider outcome without crashing or implying data exists.

### 11. Failure behavior

- A trace failure is a local UI error, never a canonical system message.
- Show a concise message in or beside the trace panel.
- Do not replace or clear the visible canonical chat history.
- Do not fall back to a provider call or reconstruct current history.
- Do not retry automatically. A manual second GET is harmless, but the application should not create a polling loop in this milestone.

### 12. Documentation

Update the README to document:

- `/trace` and `/trace <turn_id>` syntax
- the two read-only HTTP endpoints
- exactly what the panel shows
- that trace input is not canonical and creates no database event
- that traces come from recorded historical snapshots and never rerun provider or memory work
- that raw reasoning is unavailable and raw or encrypted reasoning content is omitted, while explicitly recorded provider summaries may be shown as summaries
- that usage can show reasoning-token counts without exposing reasoning text
- that trace exposes private messages and system instructions and should remain on the trusted local interface
- current limitations for human-only and stranded turns
- that `serve --database` selects the database used by both chat and trace routes

## Tests

All tests must use temporary databases. No test may read or modify `data/helios.db`, load a real API key, contact OpenAI, or make a billable request.

Add coverage for the following.

### Command and side-effect tests

1. The pure classifier distinguishes latest, valid specific ID, malformed `/trace`, and non-command input without opening the environment or database.
2. Recognition is case-sensitive: lowercase `/trace` is reserved; `/TRACE` and `/tracefoo` are ordinary messages.
3. The server-side Peter-message boundary rejects `/trace`, `/trace 17`, and malformed `/trace` arguments immediately after blank handling and before environment or provider setup.
4. The `store-message` CLI path rejects the same valid and malformed commands before opening or initializing a database and exits nonzero.
5. Rejected server and CLI commands create no directory, database, turn, message, participant configuration, API event, admin event, or sequence gap.
6. A subsequent valid Peter message receives the same next room sequence it would have received without the rejected command.
7. Blank input retains its settled no-op precedence even when API configuration is absent.

### Trace endpoint tests

Exercise routes through the real ASGI router, not by calling endpoint functions directly.

1. Literal `/api/trace/latest` takes precedence and selects the highest turn ID in `main`, including when that turn is open.
2. A specific valid ID returns that exact turn.
3. `/api/trace/0`, a negative ID, non-decimal text, non-canonical decimal text, and an integer above `9223372036854775807` return HTTP `400` with `invalid_trace_turn_id`, never a framework-generic `404`/`422` or SQLite overflow.
4. An unknown valid ID, a turn in another room, and a room with no turns return stable `404` errors.
5. Every trace success and error response contains `Cache-Control: no-store`.
6. A successful API-backed turn returns its canonical messages, exact referenced configurations, request event, response event, request boundary, provider response ID, resolved model, and usage.
7. A failed turn maps both baseline error shapes without inventing a Helios message or exposing non-allowlisted data.
8. An open or stranded turn with only a request event remains visibly open and has no fabricated outcome.
9. Cancelled and deliberately redacted turns are represented honestly.
10. An older human-only turn with a nullable initiator, no configuration, and no API event returns a valid trace with empty collections and null projections.
11. Nullable configuration and event identities remain null. Configuration and payload JSON retain arbitrary valid scalar, array, object, and null values without coercion.
12. Unknown event types remain generically available.
13. Legal same-room cross-turn `reply_to_id` and `related_message_id` references retain the target ID and target turn and are marked as outside the selected turn.
14. Messages, configurations, and events are deterministically ordered.
15. The endpoint returns the exact recorded request input even if later canonical messages have been appended to the room.
16. Multiple recognized request events, multiple terminal events, or an unredacted recognized event with the wrong shape fail with `trace_data_invalid`; a terminal event without a request remains displayable with `requested_model: null`.
17. `serve --database <temporary path>` makes trace use that exact application-state path.
18. The endpoint performs no provider construction, environment loading, memory retrieval, or database write.

### Read-only and snapshot guarantee tests

1. A missing database and its missing parent directory remain nonexistent after a trace request.
2. A missing or wrong v1.2 migration marker returns the safe `503 trace_database_unavailable` response.
3. A write attempt issued through the dedicated trace connection fails under `mode=ro` and `PRAGMA query_only = ON`.
4. The trace loader never calls `connect_database()` or `commit()`.
5. A controlled concurrent writer in WAL mode commits a change between trace queries, while the trace still returns data entirely from the snapshot established by its earlier `BEGIN`.
6. Instrumentation proves synchronous SQLite work actually executes across the configured thread boundary rather than on the ASGI event-loop thread.
7. Stored configuration and event JSON bytes remain byte-for-byte unchanged after successful and failed trace reads.

### Privacy and rendering tests

1. Sentinel values under mixed-case, hyphenated, snake_case, and camelCase credential keys do not appear in any event, configuration, memory, or convenience-projection JSON.
2. Cover `Proxy-Authorization`, `Set-Cookie`, `X-API-Key`, `apiKey`, `clientSecret`, bare `password`, bare `secret`, bare `token`, and nested variants.
3. Omission metadata uses correctly escaped RFC 6901 pointers through arrays and keys containing `/` and `~`; configuration and event omission lists remain attached to their own sources.
4. Recorded provider reasoning summaries remain visible and labeled, while reasoning-item `content`, `reasoning_text`, and `encrypted_content` names and values are omitted.
5. Usage fields, including `reasoning_tokens`, remain visible.
6. The recognized-error allowlist retains safe fields while stack traces, exception strings, local paths, environment values, SQL, and unfamiliar arbitrary error fields remain absent.
7. Invalid recorded JSON or inconsistent configuration provenance fails closed with `trace_data_invalid` and no partial trace.

### Executable browser tests

Add a small test using Node's built-in test runner plus minimal DOM and `fetch` stubs. Do not add npm, a package manifest, a browser framework, or third-party JavaScript dependencies solely for this milestone. The executable test must prove:

1. `/trace` and `/trace <id>` are intercepted and make exactly one expected GET.
2. No trace path makes a POST, retries, polls, refreshes canonical history, or shows the pending Helios indicator.
3. Malformed trace input shows usage outside the canonical messages container and makes no request.
4. Server errors render outside the canonical messages container.
5. The dialog has `role="dialog"`, `aria-modal="true"`, an accessible name, initial focus, contained background interaction, Escape behavior, and focus restoration.
6. Message and instruction whitespace is visibly preserved.
7. Hostile messages, instructions, JSON keys, and JSON values such as `<img src=x onerror=alert(1)>` render literally as text.
8. No database value is assigned to HTML, a URL, style, class, event handler, or executable/structural attribute.

Retain and pass every existing test.

Run:

```text
python -m unittest discover -s tests -v
python -m compileall app tests
git diff --check
node --test tests/test_trace_ui.js
node --check static/app.js
```

Do not add a package manager or JavaScript framework solely for this milestone.

## Manual Acceptance Test

Do not make a new OpenAI request for this test. Use the turns already present in the local room.

1. Start Helios Room normally.
2. Note the visible message count.
3. Enter `/trace`.
4. Confirm the latest turn opens with the correct canonical messages, referenced configurations, exact recorded input, event timeline, outcome, and usage.
5. Enter `/trace <known_turn_id>` and confirm that specific turn opens.
6. Inspect a failed or open turn if one exists and confirm missing data is represented honestly.
7. Close the panel and confirm the visible canonical message count is unchanged.
8. Confirm no new turn, message, API event, admin event, or room sequence was created.
9. Confirm no OpenAI request occurred.

## Non-Goals

Do not implement any of the following in Trace v1:

- seeded or room-memory retrieval
- memory creation, revision, deactivation, or deletion
- replaying or retrying a provider call
- stranded-turn reconciliation
- raw chain-of-thought access
- decrypting provider reasoning items
- a new reasoning-summary request
- editing or redacting stored events through the trace panel
- exporting full traces to a committed file
- polling or live streaming
- multiple-room selection
- authentication or remote administration
- mutating slash commands
- executing trace from the CLI; the CLI must still reject trace input before canonical writes
- schema changes
- database reset

## Repository and Authorization Constraints

- Do not alter `workspace.code-workspace`.
- Do not modify unrelated files or discard user changes.
- Do not reset, migrate, copy, or modify the live database during implementation.
- Do not make a live OpenAI request.
- Do not commit or push unless Peter explicitly requests it.
- Stop and report any schema conflict rather than improvising a migration.

## Completion Report

When implementation is finished, report:

- files changed
- exact command and endpoint behavior
- confirmation that schema v1.2 was unchanged
- exact test, compilation, diff, and JavaScript syntax results
- confirmation that `data/helios.db` was untouched
- confirmation that no live OpenAI request occurred
- any remaining limitations
- the steps for Peter's no-cost manual `/trace` acceptance test
