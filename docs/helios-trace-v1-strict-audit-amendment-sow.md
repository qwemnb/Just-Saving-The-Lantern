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
the existing stable error `trace_data_invalid`, while valid v1 traces continue
to render unchanged.

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
- changing the Trace response shape or browser presentation for valid traces
- repairing, rewriting, redacting, replaying, or deleting Turn 11 or any other
  historical row
- reading current seeded-memory tables while reconstructing a trace
- making a live OpenAI or other provider request

Because this work does not alter provider behavior or configuration identity,
it must not create a new immutable Helios configuration version.

## Required Behavior

### 1. Enforce exactly one inherited-memory context across all provider input

In `app/trace_service.py`, revise inherited-memory reconstruction so it scans
the complete recorded `request.input` sequence for the reserved prefix:

```text
INHERITED_MEMORY_CONTEXT\n
```

An item is a context candidate whenever its `content` is a string beginning
with that exact prefix. Scan all positions and all roles. A prefix-bearing item
with a wrong role, malformed JSON, or otherwise invalid structure is still a
candidate and must not be ignored during cardinality validation.

Apply these rules:

- When `memory_retrieval.selected` contains one or more records, there must be
  exactly one context candidate in the entire provider input.
- That sole candidate must be `request.input[-2]`.
- It must be exactly a `user` item with the existing supported item shape.
- Peter's triggering canonical message must remain `request.input[-1]` and
  continue to pass the existing correlation check.
- The context JSON must continue to pass canonical serialization, provenance,
  retriever-version, selected-record correlation, hash, and text-budget
  validation.
- When `memory_retrieval.selected` is empty, there must be zero context
  candidates anywhere in the provider input.
- Two or more candidates are always invalid, even if the penultimate candidate
  is valid and correlates perfectly.
- A candidate at any non-penultimate position is invalid, even if no candidate
  appears at the expected position.

Any violation must fail closed through the existing `trace_data_invalid`
contract. Trace must never select one of several candidates, silently ignore an
earlier candidate, or display a partially validated inherited-memory audit.

The `INHERITED_MEMORY_CONTEXT\n` prefix is reserved at this audit boundary.
Conservative rejection is intentional when a recorded provider input is
ambiguous about whether a prefix-bearing item is canonical content or
application-generated context. Do not infer intent from item position or from
whether the suffix happens to parse as JSON.

### 2. Require the exact Seeded Memory Retrieval v1 envelope schema

In `_validate_memory_retrieval()`, require the retrieval dictionary to contain
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
Do not preserve unknown fields for display and do not accept them as harmless
forward-compatible metadata under the v1 retriever version.

### 3. Require the exact v1 selected-record schema

For every object in `memory_retrieval.selected`, require exactly these keys:

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
`trace_data_invalid`. A future audit schema that adds fields must use an
explicitly supported retriever/schema version before Trace may accept those
fields.

### 4. Preserve the existing error and read-only contracts

All newly detected inconsistencies must use the current Trace error path:

```text
HTTP 500
error: trace_data_invalid
```

Preserve `Cache-Control: no-store` on the HTTP error response. Do not expose
raw exception text, SQL, file paths, provider payload secrets, or the rejected
data in the client-facing error.

The implementation must remain a read-only historical reconstruction:

- use only the recorded request event and already loaded canonical trace data
- do not query `seed_memories`, FTS tables, topic tables, participant-subject
  tables, or any future memory table
- do not load `.env`, instantiate a provider client, or make a network request
- do not write, repair, normalize, or backfill the database

## Implementation Guidance

Keep the change localized to the inherited-memory validation path in
`app/trace_service.py` and the associated test modules. Small named constants
or pure helpers for the two exact v1 key sets and for locating context
candidates are acceptable when they make the invariant easier to review.

Do not weaken existing validation to accommodate the new cardinality checks.
Perform structural and cardinality checks before projecting inherited-memory
data into the successful Trace response.

The production serializer and Trace validator may share stable constants where
that does not make Trace accept future fields implicitly. Exact v1 acceptance
must remain deliberate: adding a serializer field alone must not silently
expand the historical validator's accepted schema.

## Required Offline Tests

Add focused regression tests proving all of the following.

### Context cardinality and position

- A valid request containing exactly one penultimate context still succeeds.
- A second valid context inserted earlier in `request.input` fails.
- A second malformed prefix-bearing item inserted earlier fails.
- A prefix-bearing item with a non-`user` role fails.
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

### Exact v1 schemas

- A valid retrieval envelope with exactly the required keys succeeds.
- Adding one unknown top-level retrieval key fails.
- Removing one required top-level retrieval key fails.
- A valid selected record with exactly the required keys succeeds.
- Adding one unknown key to any selected record fails.
- Removing one required key from a selected record fails.
- Unknown keys fail even when all known values and correlations remain valid.
- JSON object member reordering alone does not fail validation.

### Endpoint and safety regression

- Through the real ASGI route, each new invalid case returns HTTP 500 with
  stable code `trace_data_invalid` and `Cache-Control: no-store`.
- The successful Trace response shape for valid recorded memory remains
  unchanged.
- Trace still performs no writes and queries no memory tables.
- Provider clients remain mocked or absent; the test suite makes no network
  request.

Prefer mutation-based fixtures derived from one known-valid recorded request so
each failure isolates a single violated invariant.

## Verification

Run the repository's complete existing verification suite, not only the new
tests. At minimum:

```text
python -m unittest discover -s tests
node --test tests/test_trace_ui.js
python -m compileall app tests
```

Also run any repository-established formatting, linting, JavaScript syntax,
and JSON validation checks present on the current `main` branch.

No test, fixture, script, or manual verification step may submit a live
provider request.

## Acceptance Criteria

This amendment is accepted only when all of the following are true:

1. Trace scans the full recorded provider input for the reserved inherited-
   memory prefix.
2. A nonempty selected list requires exactly one candidate, at `input[-2]`.
3. An empty selected list requires zero candidates everywhere.
4. Duplicate and misplaced candidates fail with `trace_data_invalid`.
5. The v1 retrieval envelope accepts exactly its defined key set.
6. Every v1 selected record accepts exactly its defined key set.
7. Unknown fields fail closed rather than appearing in a successful trace.
8. Existing valid Trace output and UI behavior are unchanged.
9. Schema v1.2, runtime request construction, seeded-memory retrieval, and
   immutable participant configurations are unchanged.
10. Turn 11 and all other historical rows remain byte-for-byte untouched.
11. The complete offline test suite passes.
12. No live provider call occurs.

## Deliverables

- the localized Trace validation change
- focused regression tests for context cardinality and exact v1 key sets
- any minimal test-fixture updates required by those tests
- a concise implementation report listing changed files, verification commands
  and results, and confirmation that no live provider call occurred

