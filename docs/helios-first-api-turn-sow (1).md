# Statement of Work: First API-Backed Helios Turn

## Objective

Implement the smallest complete OpenAI-backed Helios turn in `qwemnb/Helios-Room`.

When Peter submits a valid message through the browser:

1. Apply the specified validation precedence, including blank-message handling, local configuration, and room identity.
2. Atomically accept and store Peter's message and the outbound request event.
3. Close SQLite before contacting OpenAI.
4. Call the OpenAI Responses API exactly once as Helios.
5. Atomically record either the successful result or the failure and terminate the turn.
6. Refresh the browser from canonical history.

Preserve schema v1.2 and the core rule that canonical messages are immutable history. Do not update or delete canonical messages.

## Starting Point

Begin from the latest `main` branch. The reviewed baseline is:

```text
686fbb92db932ecefc5d6702eb15318c3fe8d39b
```

If `main` has advanced, inspect and preserve newer work. Do not reset or rewrite repository history.

## Settled Behavioral Decisions

- Initial testing model: `gpt-5.6-luna`.
- Later higher-capability model: explicit model ID `gpt-5.6-sol`.
- Model switching must require only changing `HELIOS_OPENAI_MODEL` and restarting the server; do not hardcode either model in orchestration code.
- Reasoning effort: `low`.
- Reasoning context: `current_turn`.
- Output cap: `2048` total generated tokens, including reasoning tokens.
- Provider-side storage: disabled with `store=False`.
- Client timeout: 120 seconds.
- SDK automatic retries: zero.
- Conversation replay: Peter and Helios canonical `chat` message text only.
- Provider reasoning items: retain them in the sanitized raw response event, but never replay them as future model state in this milestone.
- Browser authorship: always Peter, assigned server-side.
- Accepted message text: preserve exactly what the browser sends; use `.strip()` only to decide whether it is blank.
- Unusable provider results: fail the turn while preserving the sanitized provider response.
- Stranded-turn recovery: after a provider call begins, never automatically call the provider again for that turn, even if cancellation, process failure, or finalization failure leaves it open.

The 2048-token cap is intentionally conservative for this first test. Because reasoning tokens count toward it, an incomplete response is possible and must be handled as a failure, even when partial visible text exists.

## Required Implementation

### 1. Local configuration and dependencies

- Add and pin the official OpenAI Python SDK.
- Add and pin `python-dotenv` as a direct dependency.
- Load a local `.env` in the application with `load_dotenv(override=False)` before reading configuration. Existing process environment variables must take precedence over `.env` values.
- Use these environment variables:

```text
OPENAI_API_KEY=
HELIOS_OPENAI_MODEL=gpt-5.6-luna
```

- Keep the API key entirely server-side.
- Confirm `.env`, `data/`, SQLite database files, and other runtime data remain ignored by Git.
- Automated tests must not require an API key or network connection.
- Do not make a live OpenAI request during implementation or testing unless Peter explicitly authorizes one.

Construct the production client with behavior equivalent to:

```python
AsyncOpenAI(
    api_key=api_key,
    timeout=120.0,
    max_retries=0,
)
```

The timeout and retry policy are operational metadata, not Responses API request settings.

### 2. Preflight behavior

Apply validation in this exact order:

1. Reject unexpected request fields, including all client-supplied authorship fields.
2. Ignore blank or whitespace-only content using the existing blank-submission behavior.
3. Verify that `OPENAI_API_KEY` exists and is nonblank.
4. Verify that `HELIOS_OPENAI_MODEL` exists and is nonblank.
5. Verify that the target room exists and that Peter and Helios are the expected participants.
6. Begin Phase A only after all preceding checks pass.

Steps 1 through 5 occur before creating any turn, message, configuration record, or API event. A blank submission must remain ignored even when the API key or model is missing; it must not become a `503` configuration error.

A missing or invalid local configuration must return a stable configuration error and create no database history or sequence gap.

Do not attempt a separate model-list or entitlement request. Local preflight cannot prove that the account may use the requested model. An unknown, unavailable, or unauthorized model is a provider failure after Peter's message has been accepted and committed.

For initial development and smoke testing, use `gpt-5.6-luna`. Later, Peter can change the local environment value to:

```text
HELIOS_OPENAI_MODEL=gpt-5.6-sol
```

After restarting the server, the next turn must use a new immutable participant configuration for Sol. Earlier Luna messages must continue pointing to their original Luna configuration. Switching models must never rewrite message history or prior configuration provenance.

### 3. Immutable Helios participant configuration

Create or reuse a `participant_configs` record that exactly describes the model request:

- Provider: `openai`
- Model: the requested model identifier from `HELIOS_OPENAI_MODEL`
- System instructions: the exact text below
- Settings JSON:

```json
{
  "store": false,
  "reasoning": {
    "effort": "low",
    "context": "current_turn"
  },
  "max_output_tokens": 2048
}
```

- Tools JSON: `[]`
- Suggested label for the initial exact configuration: `minimal-openai-luna-v1`

Use this exact system instruction:

> You are Helios, an AI participant in a private, persistent conversation room with Peter. Respond directly and naturally to Peter's latest message, using only the canonical room history supplied in this request. Do not claim access to memories, tools, files, or events that are not present in that history.

Rules:

- Never use a configuration with a null model or null system instructions for an API call.
- Never modify a configuration after it has been used.
- Reuse a configuration only when provider, requested model identifier, instructions, settings, and tools are semantically equal.
- Parse JSON fields and compare the canonical serialization produced with sorted object keys, compact separators `(',', ':')`, and UTF-8 characters preserved, equivalent to `json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`. JSON object key order and insignificant whitespace must not create a new configuration, while array order remains significant.
- Treat `config_label` as a descriptive label only, never as configuration identity or a uniqueness guarantee.
- If any persisted request parameter changes, create a new versioned configuration rather than mutating or falsely reusing `minimal-openai-luna-v1`.
- When `HELIOS_OPENAI_MODEL` later changes to `gpt-5.6-sol`, create or reuse an exact Sol configuration with a distinct label such as `minimal-openai-sol-v1`.
- Persist the requested model identifier in the configuration. Preserve the provider-reported resolved model value in the raw provider response event.

### 4. Browser authorship and exact text preservation

The browser send route must not allow the client to choose the author.

- Prefer a request body containing only the message content.
- Configure the request schema to forbid extra fields rather than silently ignoring them.
- Resolve Peter server-side.
- Reject unexpected authorship fields, including a spoofed `participant_key`, before touching SQLite.
- Helios messages may originate only inside the orchestration service and must carry the exact Helios configuration ID used for the request.

In the browser, validate without changing the accepted text:

```javascript
const messageText = input.value;
if (!messageText.trim()) return;
```

Send `messageText`, not `messageText.trim()`. The backend must likewise use `.strip()` only for blank detection and otherwise preserve the submitted text exactly.

### 5. Text-only canonical chat replay

Build every request from the local canonical `messages` table.

- Load every canonical message in the replay range strictly by `room_sequence_no`; do not filter unsupported rows out in SQL before validating them.
- Validate that every loaded message has `message_type='chat'` and belongs to Peter or Helios. Only after validation may those messages be mapped into provider input.
- Capture the triggering Peter message's ID and room sequence number during acceptance.
- Include history only through that exact sequence number.
- Map Peter messages to `user`.
- Map Helios messages to `assistant`.
- Send the persisted Helios system instruction separately through top-level `instructions`.
- If an unsupported participant appears in the replay range, fail safely rather than silently dropping or misattributing the message.
- If any message in the replay range has a type other than `chat`, including `system`, `correction`, or `retraction`, fail safely rather than dropping it or inventing semantics for it.
- Detect unsupported participants and message types inside Phase A. Roll back the entire acceptance transaction, create no Peter message, turn, configuration, or API event, consume no sequence number, and make no provider call. Return a stable local error such as `unsupported_history_participant` or `unsupported_history_message_type`.
- Do not load provider response state from `api_events` into model input.
- Do not use `previous_response_id`, Conversations API state, tools, memory retrieval, or provider-managed conversation history.

Only Peter and Helios canonical `chat` text is replayed. Raw response items, including encrypted reasoning items produced with `store=False`, remain in the flight recorder but are not supplied to later requests during this milestone.

### 6. Three-phase orchestration

Implement the turn as three explicit phases.

#### Phase A: acceptance transaction

In one SQLite transaction:

1. Find or create the exact immutable Helios configuration.
2. Create one open turn.
3. Store Peter's exact accepted text as turn sequence 1.
   - Set `message_type` explicitly to `chat`.
4. Assign the next canonical room sequence number.
5. Load the ordered canonical replay snapshot ending at Peter's new message.
6. Build the exact provider request payload.
7. Record `openai.responses.request` as API-event sequence 1.
8. Commit.

The request event must retain the turn, Peter's triggering message, Helios participant, and exact Helios configuration provenance. Its JSON must use an explicit envelope that separates actual Responses API arguments from local audit metadata:

```json
{
  "request": {
    "model": "gpt-5.6-luna",
    "instructions": "<exact persisted instructions>",
    "input": [],
    "store": false,
    "reasoning": {
      "effort": "low",
      "context": "current_turn"
    },
    "max_output_tokens": 2048,
    "tools": []
  },
  "local_context": {
    "provider": "openai",
    "operation": "responses.create",
    "trigger_message_id": "<Peter message ID>",
    "room_sequence_boundary": 1,
    "timeout_seconds": 120,
    "max_retries": 0
  }
}
```

The `request` object must be constructed from an allowlist of actual provider arguments and must be replayable as the intended provider call. `local_context` contains only audit metadata and must never be passed to `responses.create`.

Never record the API key, authorization headers, environment contents, or other credentials.

Peter's message and the request event must be committed before contacting OpenAI.

#### Phase B: network call

- Close the acceptance transaction and SQLite connection before beginning this phase.
- Call `AsyncOpenAI.responses.create(...)` exactly once.
- Pass the requested model identifier, instructions, ordered input, `store=False`, `reasoning={"effort": "low", "context": "current_turn"}`, `max_output_tokens=2048`, and no tools.
- Do not automatically retry at the SDK or application layer.
- Hold no SQLite connection or transaction while awaiting the network.

#### Phase C: finalization transaction

Open a new SQLite connection and transaction. Confirm the turn is still open, then atomically finalize exactly one outcome.

All synchronous database work invoked from the async route must run through an appropriate thread boundary. Each worker must open and close its own SQLite connection; do not pass live connections across threads.

If cancellation, a database lock, disk failure, or any other error prevents Phase C from committing, surface a stable local finalization failure without making another provider call. The turn may remain open with an unknown provider outcome and must be handled under the manual stranded-turn policy.

### 7. Successful provider result

A response is eligible to become a canonical Helios message only when:

- The provider response status is `completed`.
- `response.output_text` is nonblank after blank detection.

On success, atomically:

1. Insert `response.output_text` exactly as the canonical Helios message and obtain its message ID.
2. Set the message's type to `chat`, use the same turn as Peter's message, and assign turn sequence 2.
3. Set the message's `participant_config_id` to the exact configuration used.
4. Set the message's `reply_to_id` to Peter's triggering message.
5. Insert `openai.responses.response` as API-event sequence 2, referencing the newly inserted Helios message.
6. Preserve the sanitized complete provider response, including provider response ID, resolved model, usage, output items, and encrypted reasoning fields returned by the SDK.
7. Retain the Helios participant and exact Helios configuration provenance on the response event.
8. Mark the turn `completed` and set `completed_at`.
9. Commit the Helios message, response event, and turn completion together.

This insertion order is required because `api_events.related_message_id` immediately references `messages`. Atomicity is unchanged because all steps occur in the same finalization transaction.

Return structured success data containing at least the turn ID, Peter message ID, Helios message ID, and completed status.

A completed response whose ordinary `output_text` is a natural-language refusal is still a canonical Helios reply. A structured refusal with no nonblank `output_text` is not canonicalized under this milestone and follows the failure path below.

### 8. Failed or unusable provider result

Treat each of the following as a failed turn:

- SDK exception.
- Network failure.
- Timeout.
- Provider rejection, including unknown or unauthorized model.
- Provider status other than `completed`.
- `incomplete` status, even if partial output text exists.
- Completed response with blank `output_text`.
- Structured refusal without nonblank `output_text`.

On failure, atomically:

1. Do not create a Helios canonical message.
2. Record `openai.responses.error` as API-event sequence 2, related to Peter's triggering message.
3. Retain the Helios participant and exact Helios configuration provenance on the error event.
4. Mark the turn `failed` and set `completed_at`.
5. Commit the error event and turn state together.

For an SDK exception or timeout, store only safe diagnostics such as:

- Error class.
- Stable local failure reason.
- Provider HTTP status and error code when available.
- Provider request ID when available.
- Sanitized summary.

Construct safe diagnostics from an allowlist of known fields. Do not persist unrestricted `str(exception)` output. Do not store credentials, request headers, environment data, or a traceback containing secrets.

For a provider response that is blank, incomplete, refused without output text, cancelled, or otherwise unusable, embed the sanitized complete provider response in the error event and include a stable reason such as:

```text
blank_output
incomplete_response
refusal_without_text
provider_status_failed
```

Do not record both a response event and an error event for one provider result. The request remains sequence 1; the single terminal response-or-error event is sequence 2.

Return an appropriate HTTP error with a stable local error code. A post-acceptance error body must include the turn ID and Peter message ID so the browser knows Peter's message is already canonical. Suggested status behavior:

- Local preflight failure: `503`, no database IDs.
- Provider failure or unusable result: `502`.
- Provider timeout: `504`.

### 9. Browser behavior

- Keep the input and Send button disabled while the request is in progress.
- Show a temporary local status such as `Helios is responding...`.
- Do not store UI status or error text as canonical messages.
- On success, clear the accepted input, reload canonical messages, and display Peter and Helios in sequence.
- On a post-acceptance failure, clear the accepted input, reload canonical messages so Peter's committed message remains visible, and display a clear local error.
- On a preflight failure, keep Peter's unsent text available for correction and display a clear configuration error.
- Prevent a second submission from the same browser while the request is pending.

## Tests

Inject or mock the provider boundary. Automated tests must make zero network calls and zero billable requests.

Add regression coverage for all of the following.

### Successful turn

- Exactly one provider method call.
- `max_retries=0` and the 120-second timeout are configured at the client boundary.
- The call includes the requested model identifier, exact instructions, `store=False`, low/current-turn reasoning settings, `max_output_tokens=2048`, and no tools.
- Canonical history is ordered by room sequence and ends at the triggering Peter message.
- Peter's message and request event are committed before the provider fake is invoked.
- No database transaction or connection remains held across the provider await.
- Peter and Helios share one turn with turn sequences 1 and 2.
- Helios has the exact `participant_config_id` and replies to Peter's message.
- The turn becomes completed.
- Request and response API events contain valid JSON and use event sequences 1 and 2.
- The request event uses the required `request` and `local_context` envelope; only the allowlisted `request` object is passed to the provider.
- The Helios message is inserted before the response event that references it, and both are committed atomically.
- The response event relates to the Helios message and retains the Helios participant and exact configuration provenance.
- The raw response retains provider ID, resolved model, usage, output items, and any returned encrypted reasoning content.

### Provider exception, rejection, and timeout

- Exactly one provider call and no retry.
- Peter's message and request event remain committed.
- No Helios message is created.
- One sanitized error event is recorded at event sequence 2.
- The error event relates to Peter's triggering message and retains the Helios participant and exact configuration provenance.
- The turn becomes failed.
- Unauthorized or unavailable model errors follow this post-acceptance path.

### Finalization failure and stranded turns

- Inject a Phase C database failure after a fake provider result and assert that exactly one provider call occurred.
- Assert that the application makes no second provider call and performs no automatic retry or resend.
- Peter's message and request event remain committed, while the turn may remain open with no terminal event because finalization did not commit.
- The route returns a stable local finalization error that does not claim the provider failed.
- Exercise cancellation after the provider call begins and assert the same no-resend invariant.

### Unusable normal responses

Test separately:

- Completed but blank `output_text`.
- Incomplete response with no visible text.
- Incomplete response with partial visible text.
- Structured refusal without `output_text`.
- Other non-completed provider status.

Each must preserve the sanitized provider response in the error event, create no Helios message, and fail the turn with the expected stable reason.

### Validation and provenance

- `.env` loading uses `override=False`, so an existing process environment value wins.
- Missing or blank API key creates no records and makes no provider call.
- Missing or blank model creates no records and makes no provider call.
- Blank or whitespace-only message creates no records, consumes no sequence number, and makes no provider call.
- A blank or whitespace-only message is ignored even when the API key or model is missing.
- A request containing an unexpected authorship field is rejected before blank detection, even when its content is blank.
- A spoofed browser `participant_key` is rejected before database mutation.
- The server always authors accepted browser submissions as Peter.
- Helios cannot be submitted from the public browser route.
- Leading and trailing whitespace in a valid accepted message is preserved exactly.

### History boundary

- Input messages are ordered strictly by canonical room sequence.
- A message occurring after the triggering Peter message is excluded.
- Only Peter and Helios canonical messages with `message_type='chat'` are replayed.
- Raw response and reasoning items from API events are not replayed.
- Unsupported participant history fails safely rather than being dropped or misattributed.
- `system`, `correction`, `retraction`, and any other unsupported message type in the replay range cause Phase A to roll back with no records, sequence gap, or provider call.

### Configuration immutability

- An exact existing Helios configuration is reused.
- A changed model, instruction, setting, or tools payload creates a new configuration.
- Changing `HELIOS_OPENAI_MODEL` from `gpt-5.6-luna` to `gpt-5.6-sol` selects the Sol request and creates or reuses the distinct Sol configuration without altering Luna provenance.
- A previously used configuration is never updated in place.
- The requested model identifier remains in the configuration while the provider-reported resolved model remains in the provider event.
- Semantically identical settings and tools JSON reuse the configuration regardless of object key order or insignificant whitespace.
- `config_label` is not used as configuration identity.

### Secret containment

- Use a unique sentinel API-key value during tests.
- Assert that the sentinel cannot appear in request, response, or error event JSON; returned HTTP errors; captured application logs; or sanitized exception summaries.
- Exercise an exception whose original text contains the sentinel and prove that persisted and returned diagnostics omit it.
- Prove request events are built from an explicit allowlist rather than serialized client state, environment mappings, headers, or arbitrary exception objects.

Retain all existing tests. Run at minimum:

```text
python -m unittest discover -s tests -v
python -m compileall app tests
git diff --check
```

## Documentation

Update the README and `.env.example` as appropriate to explain:

- Installing pinned dependencies.
- Creating a local `.env`.
- That the application loads `.env` with `load_dotenv(override=False)`, so process environment values take precedence.
- Setting `OPENAI_API_KEY` and `HELIOS_OPENAI_MODEL`.
- Starting with `gpt-5.6-luna`, then switching to `gpt-5.6-sol` by changing only the environment value and restarting the server.
- Running the server.
- The exact first-turn settings and one-request/no-retry policy.
- What is canonicalized on success.
- Why Peter's message remains canonical after a provider failure.
- That automated tests never contact OpenAI.
- How to perform one intentional manual smoke test after Peter authorizes it.
- The known stranded-turn limitation below.

## Existing Test Messages

The current development database contains two Peter test messages in open turns. Because canonical replay includes all Peter and Helios `chat` history through the triggering message, the first live Helios request will include them.

Do not reset, delete, or replace the database during implementation. Before the intentional live smoke test, tell Peter that the messages will be included and let Peter choose whether to keep them or perform a separate, explicit reset. Never reset the database without direct authorization.

## Known Stranded-Turn Window

There is an unavoidable milestone-level stranded-turn window after the acceptance transaction commits and the provider request begins but before finalization commits. A process crash is only one possible cause. Cancellation, a database lock, disk failure, connection failure, transaction failure, or any other Phase C error can also leave:

- Peter's canonical message.
- The request API event.
- An open turn.
- An unknown provider outcome.

Once the provider call begins, no subsequent failure may trigger an automatic second provider call for that turn. Do not replay, resend, retry, or attempt automatic recovery because the provider may already have produced a billable result. Document manual reconciliation of stranded open turns as a known limitation. Automated recovery and idempotent reconciliation are future milestones.

## Non-Goals

Do not implement any of the following:

- Seeded-memory or room-memory retrieval.
- Memory creation, revision, or search.
- Replay of provider reasoning state.
- `previous_response_id` or Conversations API state.
- Tools or function calling.
- Reflections.
- Autonomous Helios turns.
- Multiple AI providers.
- Streaming.
- Automatic retries.
- Automatic stranded-turn recovery or reconciliation.
- Slash commands.
- Context summarization or compaction.
- Full historical Helios persona import.
- Schema redesign.
- Cross-client concurrency control beyond the existing canonical sequence guarantees.

## Safety and Repository Constraints

- Do not rewrite Git history or force-push.
- Do not alter `workspace.code-workspace`.
- Do not commit `.env`, API keys, SQLite databases, or runtime data.
- Do not make a live OpenAI request unless Peter explicitly authorizes it.
- Do not reset the development database without Peter's explicit authorization.
- Do not modify unrelated files or overwrite existing user work.
- Do not commit or push the implementation unless explicitly requested.
- If the existing schema cannot support a requirement without redesign, stop and report the specific conflict rather than silently changing the schema.

## Completion Report

When finished, report:

- Files changed.
- Important implementation decisions.
- Exact test, compilation, and `git diff --check` results.
- Confirmation that tests made no network request.
- Any remaining limitations or schema constraints.
- Whether the current development database still contains the two test messages.
- The exact command and browser steps for one intentional live smoke test.
- A reminder that the smoke test must not be run until Peter explicitly authorizes it.
