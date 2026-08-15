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
- `/trace <turn_id>` means that exact positive integer turn ID, but only if it belongs to room `main`.
- Resolve the newest turn deterministically with `ORDER BY turns.id DESC LIMIT 1`.
- Trace commands are local control input, not canonical conversation.
- Read historical request input from the recorded request API event. Never reconstruct it from current room history.
- Read historical provider output or error from the recorded terminal API event. Never contact the provider to fill missing data.
- Support completed, failed, cancelled, open, stranded, and older human-only turns, including turns with no configuration or API events.
- Use one SQLite read snapshot so all returned sections describe the same database state.
- Run SQLite work through a thread boundary so the FastAPI event loop is not blocked.
- Do not load `.env`, instantiate an OpenAI client, call `run_helios_turn`, retrieve memory, or allocate sequence numbers.
- Do not expose raw chain-of-thought. Reasoning token counts and an explicitly recorded provider reasoning summary may be shown, but opaque encrypted reasoning content must be omitted.
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
- A specific turn ID must be a positive base-10 integer.
- `/tracefoo` is not a trace command and must retain normal message behavior.
- If the first whitespace-delimited token is exactly `/trace` but its arguments are invalid, show this local usage message and do nothing else:

```text
Usage: /trace [positive turn ID]
```

- A recognized or malformed trace command must never be sent to `POST /api/messages`.
- Do not trim or otherwise change ordinary non-command Peter messages.

### 2. Defense at the canonical turn boundary

The browser is the normal command dispatcher, but the server must also prevent a trace command from becoming canonical if a client bypasses or breaks the UI.

At the earliest shared Peter-message acceptance boundary:

1. Preserve the existing blank-message behavior first.
2. Detect a first token of exactly `/trace` after whitespace trimming.
3. Reject it with HTTP `400` and stable error code `local_command_only` before environment loading, database preflight, configuration creation, or provider construction.

The response message should tell the client to use the local trace interface. This guard applies to valid and malformed `/trace` arguments. It must create no row in any table and make no provider call.

Do not reserve every slash-prefixed message. Unknown commands and text such as `/tracefoo` remain outside this milestone.

### 3. Read-only HTTP endpoints

Add:

```text
GET /api/trace/latest
GET /api/trace/{turn_id}
```

Requirements:

- Constrain `{turn_id}` to a positive integer.
- Both endpoints inspect only room key `main`.
- `latest` selects the highest turn ID in that room, regardless of status.
- Register the literal `/latest` route before the dynamic route, or otherwise use explicit route matching so `latest` cannot be misparsed as a turn ID.
- A turn ID from another room must be treated as not found.
- Use a shared trace service rather than duplicating query and projection logic across routes.
- Call the synchronous SQLite reader through `asyncio.to_thread` or an equivalent thread boundary.
- Return JSON only. Do not render server-side HTML.

Stable error behavior:

| Condition | HTTP | Error code |
| --- | ---: | --- |
| No turns exist for `/api/trace/latest` | 404 | `trace_not_found` |
| Requested turn does not exist in `main` | 404 | `trace_not_found` |
| Database cannot be opened or required schema is unavailable | 503 | `trace_database_unavailable` |
| Recorded JSON or relational trace data is internally inconsistent | 500 | `trace_data_invalid` |

Do not include unrestricted exception text, SQL, paths, environment values, or credentials in errors.

### 4. Consistent historical snapshot

For one trace request:

1. Refuse a missing database instead of allowing a trace read to create an empty file.
2. Open one SQLite connection, enable `PRAGMA query_only = ON`, and use read-only connection mode where it is reliable on the supported platform.
3. Begin one read transaction before resolving the target turn.
4. Load the turn and its room and initiator identity.
5. Load every canonical message whose `turn_id` and `room_id` match the turn.
6. Load every participant configuration referenced by those messages or by the turn's API events.
7. Load every API event for the turn.
8. End the read transaction without committing application changes and close the connection.

Ordering must be deterministic:

- messages by `turn_sequence_no`, then `id`
- configurations by `id`
- API events by `sequence_no`, then `id`

The trace loader must issue no `INSERT`, `UPDATE`, `DELETE`, DDL, or sequence-allocation call. It must not call `commit` on application data or create an `admin_events` record.

Validate enough relational consistency to avoid presenting a misleading trace. At minimum:

- every returned record belongs to the selected turn and room where applicable
- referenced participant and configuration identity is retained
- JSON columns parse successfully
- an API event's participant configuration, when present, belongs to that participant
- duplicate API event sequence numbers or duplicate turn message sequence numbers fail safely, even though schema constraints should prevent them

Do not require a request event or terminal event. Older human-only turns and stranded open turns are valid trace subjects.

### 5. Versioned trace response

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
- `recorded_request` is a convenience projection of the first valid `openai.responses.request` event, or `null` when no such event exists.
- `provider_outcome` is a convenience projection of the terminal response or error event, or `null` when none exists.
- Build both convenience projections from the already trace-safe event payloads so omitted or redacted fields cannot reappear there.
- Do not fabricate missing fields or turn an open turn into an inferred failure.
- Preserve recorded request ordering, message text, instructions, settings, model identifiers, and event sequence numbers.
- Keep stored `payload_json` unchanged. Trace redaction is an outbound display projection, not a database rewrite.

### 6. Defense-in-depth trace projection

The existing turn service sanitizes provider data before storage. Apply a second, display-only defense before returning any event payload to the browser.

Recursively omit or replace values whose case-insensitive key is credential-shaped, including at minimum:

- `authorization`
- `proxy_authorization`
- `headers`
- `cookie`
- `set_cookie`
- `api_key`
- `openai_api_key`
- `x_api_key`
- `client_secret`
- `access_token`
- `refresh_token`
- keys ending in `_api_key`, `_secret`, `_password`, or singular `_token`

Opaque provider reasoning must also be excluded:

- omit every `encrypted_content` key and value from the returned payload
- record the corresponding JSON Pointer in `omitted_json_pointers`
- never attempt to decrypt, decode, summarize, or infer omitted reasoning

Do not remove ordinary usage fields such as `input_tokens`, `output_tokens`, `reasoning_tokens`, or `total_tokens`.

If an explicitly requested provider reasoning summary is already present in a sanitized historical event, it may remain visible and must be labeled as a provider summary, not raw reasoning. Trace v1 must not make a provider call to generate a summary.

Do not load the current API key for trace redaction. Current event construction remains allowlisted and secret-safe; the trace projection is defense in depth for sensitive field names and opaque reasoning data.

### 7. Provider outcome projection

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
- any sanitized unusable response metadata already retained by the event

Do not persist or return unrestricted exception strings.

### 8. Browser trace panel

Intercept the command before the ordinary send flow displays `Helios is responding...` or calls `sendMessage`.

Command behavior:

- `/trace` fetches `GET /api/trace/latest`.
- `/trace 17` fetches `GET /api/trace/17`.
- A valid command clears the input after dispatch.
- An invalid trace command shows the usage message locally and leaves canonical history unchanged.
- Closing the panel restores focus to the message input.

Add an accessible modal or side panel with a visible title, close control, Escape-key behavior, and a scrollable body. Render these sections:

1. **Turn**: ID, status, room, initiator, timestamps, and elapsed duration when terminal.
2. **Canonical messages**: exact text, participant, message type, IDs, room sequence, turn sequence, reply target, configuration ID, and timestamp.
3. **Helios configuration**: provider, requested model, label, configuration ID, system instructions, settings, tools, and creation time.
4. **Recorded provider request**: exact sanitized instructions, ordered input, request settings, and local audit context.
5. **Provider outcome**: response ID, provider status, resolved model, usage including reasoning-token count, incomplete details, or safe error data.
6. **API event timeline**: sequence number, type, timestamp, related message, redaction state, and collapsible trace-safe JSON.
7. **Memory context**: for now, show `No inherited memory retrieval was recorded for this turn.` If a future request event contains `local_context.memory_retrieval`, show the recorded object generically without querying memory tables. A dedicated memory presentation will be added by Seeded Memory Retrieval v1.

Use DOM `textContent` or equivalent safe text nodes for every database and event value. Do not render message text, instructions, or JSON with `innerHTML`.

The panel must handle empty configurations, no API events, open turns, failed turns, and missing provider outcome without crashing or implying data exists.

### 9. Failure behavior

- A trace failure is a local UI error, never a canonical system message.
- Show a concise message in or beside the trace panel.
- Do not replace or clear the visible canonical chat history.
- Do not fall back to a provider call or reconstruct current history.
- Do not retry automatically. A manual second GET is harmless, but the application should not create a polling loop in this milestone.

### 10. Documentation

Update the README to document:

- `/trace` and `/trace <turn_id>` syntax
- the two read-only HTTP endpoints
- exactly what the panel shows
- that trace input is not canonical and creates no database event
- that traces come from recorded historical snapshots and never rerun provider or memory work
- that raw reasoning is unavailable and opaque encrypted reasoning is omitted
- that usage can show reasoning-token counts without exposing reasoning text
- that trace exposes private messages and system instructions and should remain on the trusted local interface
- current limitations for human-only and stranded turns

## Tests

All tests must use temporary databases. No test may read or modify `data/helios.db`, load a real API key, contact OpenAI, or make a billable request.

Add coverage for the following.

### Command and side-effect tests

1. The server-side Peter-message boundary rejects `/trace`, `/trace 17`, and malformed `/trace` arguments before environment or provider setup.
2. Rejected trace commands create no turn, message, participant configuration, API event, admin event, or sequence gap.
3. A subsequent valid Peter message receives the same next room sequence it would have received without the rejected command.
4. `/tracefoo` is not classified as the trace command.
5. The browser command path does not call `POST /api/messages` and does not show `Helios is responding...`.
6. Invalid trace syntax shows the local usage message and performs no fetch or write.

### Trace endpoint tests

1. `latest` selects the highest turn ID in `main`, including when that turn is open.
2. A specific valid ID returns that exact turn.
3. An unknown ID, a turn in another room, and a room with no turns return stable `404` errors.
4. A successful API-backed turn returns its two canonical messages, exact referenced configuration, request event, response event, request boundary, provider response ID, resolved model, and usage.
5. A failed turn returns Peter's message, the exact request event, the sanitized error event, and failed status without inventing a Helios message.
6. An open or stranded turn with only a request event remains visibly open and has no fabricated outcome.
7. An older human-only turn with no configuration or API event returns a valid trace with empty collections and null projections.
8. Messages, configurations, and events are deterministically ordered.
9. The endpoint returns the exact recorded request input even if later canonical messages have been appended to the room.
10. The endpoint performs no provider construction, environment loading, memory retrieval, or database write.

### Privacy and rendering tests

1. Nested sentinel values under credential-shaped keys do not appear in returned JSON.
2. No `encrypted_content` value or payload field is returned; only its path may appear in `omitted_json_pointers`.
3. Usage fields, including `reasoning_tokens`, remain visible.
4. Safe stored error fields remain available while unrestricted exception text and environment values remain absent.
5. A malicious-looking message or event string such as `<img src=x onerror=alert(1)>` is rendered as text, never HTML.
6. Invalid recorded JSON or inconsistent configuration provenance fails closed with `trace_data_invalid` and no partial trace.

Retain and pass every existing test.

Run:

```text
python -m unittest discover -s tests -v
python -m compileall app tests
git diff --check
```

If browser JavaScript changes, also run a syntax check such as:

```text
node --check static/app.js
```

Do not add a package manager or JavaScript framework solely for this milestone.

## Manual Acceptance Test

Do not make a new OpenAI request for this test. Use the turns already present in the local room.

1. Start Helios Room normally.
2. Note the visible message count.
3. Enter `/trace`.
4. Confirm the latest turn opens with the correct canonical messages, model configuration, exact recorded input, event timeline, outcome, and usage.
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
- a CLI trace command
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
