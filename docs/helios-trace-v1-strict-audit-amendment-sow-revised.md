# Statement of Work: Helios Room Trace v1 Strict Audit Amendment

## Objective

Close the two remaining fail-closed validation gaps found during review of
commit `97671c1afb2c986d8f19cb852c031d2dfdc6daf8`:

1. Trace must require exactly one application-generated inherited-memory
   context in the complete recorded provider input when retrieval selected one
   or more records, and none when retrieval selected no records.
2. Trace must reject unknown fields in the Seeded Memory Retrieval v1 audit
   envelope and in every selected-record audit object.

This is a historical-audit integrity change. It does not change runtime memory
retrieval, provider-request construction, model behavior, or canonical room
history.

The amendment is complete when a recorded request with duplicate, misplaced,
unexpected, or structurally ambiguous inherited-memory audit data fails with
the existing stable error `trace_data_invalid`, while traces valid under the
revised penultimate-position contract continue to render unchanged.

## Starting Point

Begin from the latest `main` branch of `qwemnb/Helios-Room`. The reviewed
baseline is:

```text
97671c1afb2c986d8f19cb852c031d2dfdc6daf8
```

If `main` has advanced, inspect and preserve the newer work. Do not reset,
rewrite history, discard unrelated changes, or modify immutable historical
records.

## Scope Boundaries

This amendment authorizes only Trace validation and its offline regression
tests.

The following are explicitly out of scope:

- changing `run_helios_turn()` or the provider request emitted by the runtime
- changing seeded-memory import, tokenization, FTS retrieval, ranking, result
  limits, text budgets, or context serialization
- changing the persisted Helios instructions, model, reasoning settings, or
  configuration identity
- creating a new `participant_configs` row
- changing schema v1.2 or adding a migration
- changing the Trace response shape or browser presentation for traces valid
  under the revised penultimate-position contract
- repairing, rewriting, redacting, replaying, or deleting Turn 11 or any other
  historical row
- adding a legacy validation branch for the earlier first-item inherited-
  context position
- reading current seeded-memory tables while reconstructing a trace
- making a live OpenAI or other provider request

Because this work does not alter provider behavior or configuration identity,
it must not create a new immutable Helios configuration version.

## Historical Turn 11 Decision

Turn 11 is immutable historical evidence. Its recorded inherited-memory
context used the earlier first-item position rather than the revised universal
penultimate position.

Turn 11 must remain byte-for-byte untouched. Under the revised universal
positional contract, attempting to trace Turn 11 must fail closed with
`trace_data_invalid`. Do not add a configuration-based, turn-based, or other
legacy validation branch to make it pass. This is an intentional validation
outcome, not permission to repair, redact, replay, delete, or otherwise modify
Turn 11.

Automated tests must reproduce this historical shape only in a temporary
fixture. They must not open, read, copy, or modify the live database to test
Turn 11 behavior.

## Required Behavior

### 1. Validate the raw historical request before privacy projection

For every recognized, unredacted request event whose raw payload contains a
Seeded Memory Retrieval v1 envelope, inherited-memory cardinality, position,
item shape, envelope schema, and selected-record schema must be validated
against `request_event["raw_payload"]` before `project_trace_json()` or any
other secret or display projection can remove fields.

Do not use the privacy-projected request as the source of truth for these
strict checks. In particular, secret-like unknown keys such as `token`,
`api_key`, or `client_secret` must not disappear before exact-schema
validation.

The existing redaction state contract remains unchanged. A deliberately
redacted request remains `unavailable` with reason `request_redacted`, and an
input deliberately omitted by the established projection remains
`unavailable` with reason `request_input_unavailable`.

Only privacy-projected data may be returned in a successful Trace response or
rendered by the browser. Raw payload objects are validation inputs only and
must never be returned as an additional field, substituted into the response,
logged, or included in an exception message.

When raw validation fails:

- return only the existing stable `trace_data_invalid` error
- do not include rejected field names, rejected values, raw exception text,
  SQL, file paths, provider payload secrets, or any portion of the rejected
  payload in the client-facing response
- preserve `Cache-Control: no-store`

### 2. Enforce exactly one inherited-memory context across all raw provider input

In `app/trace_service.py`, revise inherited-memory reconstruction so it scans
the complete recorded `request_event["raw_payload"]["request"]["input"]`
sequence for the reserved prefix:

```text
INHERITED_MEMORY_CONTEXT\n
```

An item is a context candidate whenever it is an object whose `content` value
is a string beginning with that exact prefix. Scan all positions and all roles.
A prefix-bearing item with a missing or wrong role, malformed JSON, extra
fields, or otherwise invalid structure is still a candidate and must not be
ignored during cardinality validation.

Apply these rules:

- When `memory_retrieval.selected` contains one or more records, there must be
  exactly one context candidate in the entire raw provider input.
- That sole candidate must be `request.input[-2]`.
- The sole candidate must satisfy the exact item schema:

  ```python
  set(candidate.keys()) == {"role", "content"}
  ```

- Its `role` must be exactly `user`.
- Its `content` must be a string beginning with the reserved prefix.
- Peter's triggering canonical message must remain `request.input[-1]` and
  continue to pass the existing exact correlation check.
- The context JSON must continue to pass canonical serialization, provenance,
  retriever-version, selected-record correlation, hash, and text-budget
  validation.
- When `memory_retrieval.selected` is empty, there must be zero context
  candidates anywhere in the raw provider input.
- Two or more candidates are always invalid, even if the penultimate candidate
  is valid and correlates perfectly.
- A candidate at any non-penultimate position is invalid, even if no candidate
  appears at the expected position.
- When selected records require a context, a penultimate item with non-string
  `content` is not a candidate and therefore fails the required cardinality;
  it must not be coerced, stringified, or otherwise accepted.

Any violation must fail closed through the existing `trace_data_invalid`
contract. Trace must never select one of several candidates, silently ignore
an earlier candidate, or display a partially validated inherited-memory audit.

The `INHERITED_MEMORY_CONTEXT\n` prefix is reserved at this audit boundary.
Conservative rejection is intentional when a recorded provider input is
ambiguous about whether a prefix-bearing item is canonical content or
application-generated context. Do not infer intent from item position, role,
or whether the suffix happens to parse as JSON.

### 3. Require the exact Seeded Memory Retrieval v1 envelope schema

Against the raw audit envelope, require the retrieval dictionary to contain
exactly these keys:

| Key |
| --- |
| `retriever_version` |
| `owner_participant_id` |
| `query_source_message_id` |
| `query_terms` |
| `fts_query` |
| `result_limit` |
| `text_budget_chars` |
| `omitted_for_budget` |
| `selected` |

The key comparison must be set-based so JSON object member order remains
irrelevant. Continue all existing value, type, range, query-reconstruction,
cardinality, and correlation checks.

Missing keys and additional keys must both fail with `trace_data_invalid`.
This includes additional keys that the privacy projection would classify as
secret-like and omit. Do not preserve unknown fields for display and do not
accept them as harmless forward-compatible metadata under the v1 retriever
version.

### 4. Require the exact v1 selected-record schema

Against each raw object in `memory_retrieval.selected`, require exactly these
keys:

| Key |
| --- |
| `rank` |
| `seed_memory_id` |
| `stable_id` |
| `seed_batch_id` |
| `source_content_sha256` |
| `source_label` |
| `source_locator` |
| `memory_text_sha256` |
| `exact_topic_match` |
| `topic_match_weight_sum` |
| `fts_bm25` |
| `importance` |
| `confidence` |

The key comparison must again be set-based. Retain all existing checks for
rank order, identifiers, uniqueness, SHA-256 formatting, labels, locators,
finite numeric values, score consistency, bounds, and deterministic sorting.

An unknown key in any selected record must fail the entire trace with
`trace_data_invalid`, including an unknown key that secret projection would
otherwise omit. A future audit schema that adds fields must use an explicitly
supported retriever/schema version before Trace may accept those fields.

### 5. Preserve the existing error and read-only contracts

All newly detected inconsistencies must use the current Trace error path:

```text
HTTP 500
error: trace_data_invalid
```

Preserve `Cache-Control: no-store` on the HTTP error response. Do not expose
raw exception text, SQL, file paths, provider payload secrets, rejected field
names, or rejected field values in the client-facing error.

The implementation must remain a read-only historical reconstruction:

- use only the raw recorded request event, its privacy projection, and already
  loaded canonical trace data
- do not query `seed_memories`, FTS tables, topic tables, participant-subject
  tables, or any future memory table
- do not load `.env`, instantiate a provider client, or make a network request
- do not write, repair, normalize, or backfill the database

## Implementation Guidance

Keep the change localized to the inherited-memory validation path in
`app/trace_service.py` and the associated test modules. Small named constants
or pure helpers for the two exact v1 key sets and for locating context
candidates are acceptable when they make the invariant easier to review.

The raw request event and privacy-projected request event may both be supplied
to the inherited-memory reconstruction helper, but their roles must remain
separate:

- raw data determines strict validity
- privacy-projected data supplies successful browser-visible output

Do not expose or return a raw-payload alias. Do not reconstruct the successful
browser response from raw data after projection. Do not let the projection
silently broaden exact v1 schema acceptance by removing an unknown field
before validation.

Do not weaken existing validation to accommodate the new cardinality checks.
Perform raw structural and cardinality checks before projecting inherited-
memory data into the successful Trace response.

The production serializer and Trace validator may share stable constants where
that does not make Trace accept future fields implicitly. Exact v1 acceptance
must remain deliberate: adding a serializer field alone must not silently
expand the historical validator's accepted schema.

## Required Offline Tests

All automated tests must use temporary databases and must not open or inspect
the live database.

Prefer mutation-based fixtures derived from one known-valid recorded request
so each failure isolates a single violated invariant.

### Context cardinality, position, and exact item shape

- A valid request containing exactly one penultimate context still succeeds.
- A second valid context inserted earlier in `request.input` fails.
- A second malformed prefix-bearing item inserted earlier fails.
- A sole prefix-bearing candidate with one extra key fails.
- A sole prefix-bearing candidate with a missing `role` fails.
- A sole prefix-bearing candidate with a wrong `role` fails.
- A penultimate item with non-string `content` fails when selected records
  require a context.
- One valid context moved away from the penultimate position fails.
- Selected records with no context candidate fail.
- An empty selected list with a prefix-bearing item at the penultimate position
  fails.
- An empty selected list with a prefix-bearing item at any earlier position
  fails.
- Peter's exact triggering message remains required as the final input item.

At least one duplicate-context test must reproduce the review finding exactly:
the expected penultimate context remains valid while another context appears
earlier in the input. The result must be `trace_data_invalid`.

### Exact raw v1 schemas

- A valid raw retrieval envelope with exactly the required keys succeeds.
- Adding one unknown top-level retrieval key fails.
- Adding `memory_retrieval.token` with a distinctive sentinel value fails.
- Removing one required top-level retrieval key fails.
- A valid raw selected record with exactly the required keys succeeds.
- Adding one unknown key to any selected record fails.
- Adding `memory_retrieval.selected[0].client_secret` with a distinctive
  sentinel value fails.
- Removing one required key from a selected record fails.
- Unknown keys fail even when all known values and correlations remain valid.
- JSON object member reordering alone does not fail validation.

For both secret-like unknown-field cases, assert through the real ASGI route
that:

- the response is HTTP 500
- the response code is `trace_data_invalid`
- `Cache-Control` is `no-store`
- neither the unknown field name nor its sentinel value appears in the response
  body or headers

The test may additionally capture logs or exceptions if the test harness
already exposes them, but must not add production logging of raw payload data.

### Historical Turn 11 contract

- A temporary fixture reproducing Turn 11's earlier first-item context position
  fails with `trace_data_invalid` under the universal penultimate contract.
- The attempted trace performs no write and leaves every fixture row unchanged.
- No legacy position or configuration exception is accepted.
- The test does not open, read, copy, or modify the live database.

### Endpoint and safety regression

- Through the real ASGI route, each new invalid case returns HTTP 500 with
  stable code `trace_data_invalid` and `Cache-Control: no-store`.
- Error responses contain no rejected field names, rejected values, raw
  payload fragments, unrestricted exception text, SQL, paths, or secrets.
- The successful Trace response shape for valid recorded memory remains
  unchanged and contains privacy-projected data only.
- Trace still performs no writes and queries no memory tables.
- Provider clients remain mocked or absent; the test suite makes no network
  request.

## Verification

Run the repository's complete existing verification suite, not only the new
tests. At minimum:

```text
python -m unittest discover -s tests
node --test tests/test_trace_ui.js
python -m compileall app tests
git diff --check
node --check static/app.js
```

Also run any repository-established formatting, linting, JavaScript syntax,
and JSON validation checks present on the current `main` branch.

No test, fixture, script, or manual verification step may submit a live
provider request or access the live database.

## Acceptance Criteria

This amendment is accepted only when all of the following are true:

1. Strict inherited-memory validation reads the unredacted historical request
   from `request_event["raw_payload"]` before secret projection.
2. Only privacy-projected data is returned to the browser.
3. Rejected field names and values never appear in errors.
4. Trace scans the full raw recorded provider input for the reserved inherited-
   memory prefix.
5. A nonempty selected list requires exactly one candidate, at `input[-2]`.
6. The sole candidate has exactly the keys `role` and `content`, with role
   `user` and string content beginning with the reserved prefix.
7. An empty selected list requires zero candidates everywhere.
8. Duplicate, malformed, and misplaced candidates fail with
   `trace_data_invalid`.
9. The v1 raw retrieval envelope accepts exactly its defined key set.
10. Every raw v1 selected record accepts exactly its defined key set.
11. Secret-like unknown fields fail closed before projection rather than being
    omitted into an apparently valid audit.
12. Unknown fields fail closed rather than appearing in a successful trace.
13. Traces valid under the revised penultimate-position contract retain their
    existing successful response shape and UI behavior.
14. Turn 11 remains byte-for-byte untouched and fails closed when traced under
    the revised universal positional contract; no legacy branch exists.
15. Schema v1.2, runtime request construction, seeded-memory retrieval, and
    immutable participant configurations are unchanged.
16. All other historical rows remain untouched.
17. The complete offline test suite passes.
18. No live database access or live provider call occurs.

## Deliverables

- the localized Trace validation change
- focused regression tests for raw context cardinality, exact context-item
  shape, and exact raw v1 key sets
- secret-like unknown-field no-leakage route tests
- a temporary historical-position fixture for the Turn 11 fail-closed contract
- any minimal test-fixture updates required by those tests
- a concise implementation report listing changed files, verification commands
  and results, and confirmation that neither the live database nor a live
  provider was accessed
