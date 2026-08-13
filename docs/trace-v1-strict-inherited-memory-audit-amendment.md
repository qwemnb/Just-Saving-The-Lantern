# Trace v1 Strict Inherited-Memory Audit Amendment

## Status and authorization gate

This document is a proposed Statement of Work for review. Drafting it does not
authorize implementation.

Stop after returning this document for review. Do not modify application code,
tests, schema, configuration, historical data, or the live database. Do not
make a live provider request, commit, push, or rewrite history. Implementation
requires separate explicit approval.

## Baseline and relationship to prior work

Treat the latest `main` branch as the implementation baseline. The baseline
reviewed while drafting this SOW is:

```text
d8afd8a304331c4d9c4ee85f63de614db4725483
```

If `main` advances before implementation, inspect and preserve the newer work.
Do not reset, revert, or discard unrelated changes.

[`seeded-memory-retrieval-v1-amendment.md`](seeded-memory-retrieval-v1-amendment.md)
is immutable historical specification for the behavior already implemented by
`97671c1`. Do not edit, supersede, or restate that amendment as part of this
work. This new SOW covers only the remaining Trace v1 audit-validation
hardening.

## Objective

Close two remaining fail-closed validation gaps in Trace v1:

1. Require exactly one application-generated inherited-memory context in the
   complete recorded provider input when retrieval selected records, and none
   when retrieval selected no records.
2. Reject unknown or missing fields in the Seeded Memory Retrieval v1 audit
   envelope and every selected-record audit object before privacy projection
   can remove secret-like fields.

This is a historical-audit integrity change. It must not change runtime memory
retrieval, provider-request construction, model behavior, canonical room
history, or the successful Trace response shape.

## Scope boundaries

Implementation authorization, if later granted, is limited to Trace validation
and offline regression tests.

The following are out of scope:

- changing `run_helios_turn()` or any provider request emitted by the runtime
- changing seeded-memory import, tokenization, FTS retrieval, ranking, result
  limits, text budgets, or context serialization
- changing persisted instructions, model selection, reasoning settings,
  configuration identity, or any `participant_configs` row
- changing schema v1.2 or adding a migration
- changing Trace routes, response shape, or browser presentation for traces
  valid under the strict penultimate-position contract
- repairing, rewriting, redacting, replaying, deleting, or otherwise modifying
  Turn 11 or any other historical row
- adding a legacy validation branch for a first-item inherited context
- reading current seeded-memory tables while reconstructing a trace
- loading provider credentials, creating a provider client, or making a live
  provider request

No new immutable configuration is required because this work does not alter
provider behavior or configuration identity.

## Required behavior

### 1. Validate raw audit evidence before privacy projection

For every recognized, unredacted request event whose raw payload contains a
Seeded Memory Retrieval v1 envelope, validate inherited-memory cardinality,
position, item shape, retrieval-envelope schema, and selected-record schemas
against `request_event["raw_payload"]` before `project_trace_json()` or any
other privacy/display projection can remove fields.

The privacy-projected request must not be the source of truth for these strict
checks. Secret-like unknown keys such as `token`, `api_key`, and
`client_secret` must not disappear before exact-schema validation.

Raw payload data is validation input only. Only privacy-projected data may be
returned in a successful Trace response or rendered by the browser. Do not
return a raw-payload alias, substitute raw objects into the response, log raw
payload data, or include rejected data in an exception message.

The existing unavailable-state contract remains unchanged:

- a deliberately redacted request is `unavailable` with reason
  `request_redacted`
- an input omitted by the established projection is `unavailable` with reason
  `request_input_unavailable`

Any raw validation failure must use only the existing stable error:

```text
HTTP 500
error: trace_data_invalid
message: The recorded trace data is invalid.
```

The response must retain `Cache-Control: no-store`. Rejected field names,
rejected values, raw payload fragments, exception text, SQL, paths, and secrets
must never appear in response bodies or headers.

### 2. Scan the complete provider input for context candidates

Scan every position and role in
`request_event["raw_payload"]["request"]["input"]` for the exact reserved
prefix:

```text
INHERITED_MEMORY_CONTEXT\n
```

An item is a candidate whenever it is an object whose `content` value is a
string beginning with that exact prefix. A prefix-bearing item remains a
candidate when its role is missing or wrong, its JSON suffix is malformed, or
it has extra fields. Candidate discovery must not silently ignore structurally
invalid or ambiguously placed prefix-bearing objects.

When `memory_retrieval.selected` is nonempty:

- exactly one candidate must exist in the complete input
- that candidate must be exactly `request.input[-2]`
- its exact schema must satisfy:

  ```python
  set(candidate.keys()) == {"role", "content"}
  ```

- `role` must be exactly `user`
- `content` must be a string beginning with the reserved prefix
- Peter's triggering canonical message must be exactly `request.input[-1]`
- the context suffix must continue to pass canonical JSON serialization,
  provenance, retriever-version, selected-record correlation, hash, and text-
  budget validation

When `memory_retrieval.selected` is empty, zero candidates may appear anywhere
in the input.

Two or more candidates are always invalid, even when the expected penultimate
candidate is valid. A sole non-penultimate candidate is invalid. When selected
records require a context, a penultimate object with non-string `content`
produces no candidate and therefore fails required cardinality; it must not be
coerced or stringified.

The reserved prefix defines a conservative audit boundary. Trace must not
infer intent from item position, role, or whether a suffix happens to parse.

### 3. Require the exact retrieval-envelope schema

Before projection, require the raw `memory_retrieval` object to contain exactly
these keys:

```text
retriever_version
owner_participant_id
query_source_message_id
query_terms
fts_query
result_limit
text_budget_chars
omitted_for_budget
selected
```

Use set-based comparison so JSON member order is irrelevant. Missing and
additional keys must fail with `trace_data_invalid`, including keys privacy
projection would otherwise remove. Retain all existing type, range, query-
reconstruction, cardinality, and correlation checks.

### 4. Require the exact selected-record schema

Before projection, require every raw object in `memory_retrieval.selected` to
contain exactly these keys:

```text
rank
seed_memory_id
stable_id
seed_batch_id
source_content_sha256
source_label
source_locator
memory_text_sha256
exact_topic_match
topic_match_weight_sum
fts_bm25
importance
confidence
```

Use set-based comparison. Missing and additional keys must fail the entire
trace, including unknown secret-like keys. Retain all existing validation for
rank order, identifiers, uniqueness, SHA-256 formatting, nonblank provenance,
finite numeric values, score consistency, bounds, and deterministic sorting.

A future audit shape must use an explicitly supported retriever/schema version
rather than silently broadening the accepted v1 key sets.

### 5. Preserve read-only reconstruction and historical fidelity

The implementation must remain a single read-only SQLite snapshot and use only
the raw recorded request event, its privacy projection, and already loaded
canonical trace data.

It must not:

- query seeded-memory, FTS, topic, participant-subject, or future memory tables
- load `.env` or provider configuration
- instantiate a provider client or access the network
- write, repair, normalize, backfill, or commit database state

Turn 11 is immutable historical evidence. Its earlier first-item inherited-
context position does not satisfy the strict universal penultimate-position
contract. Turn 11 must remain byte-for-byte untouched and must fail closed with
`trace_data_invalid` when traced under this contract. Do not add a turn-based,
configuration-based, or other legacy exception.

Automated tests must reproduce this shape only in a temporary fixture. They
must not open, inspect, copy, or modify the live database.

Traces valid under the strict penultimate-position contract must continue
unchanged.

## Implementation guidance

Keep implementation localized to `app/trace_service.py` and associated test
modules. Small explicit constants or pure helpers for the two exact key sets
and candidate discovery are acceptable.

Raw and projected data must retain separate roles:

- raw data determines strict validity
- privacy-projected data supplies successful browser-visible output

Adding a field to a production serializer must not automatically broaden what
the Trace v1 historical validator accepts.

## Required offline tests

All tests must use temporary databases. Provider behavior must be mocked or
absent, and no test may access the live database or network.

### Context cardinality, position, and exact item shape

- a valid request with exactly one penultimate context succeeds unchanged
- a second valid context inserted earlier fails
- a second malformed prefix-bearing candidate inserted earlier fails
- a sole candidate with an extra key fails
- a sole candidate with a missing `role` fails
- a sole candidate with the wrong `role` fails
- a required penultimate context with non-string `content` fails
- one valid context moved away from the penultimate position fails
- selected records with no candidate fail
- an empty selection with a penultimate candidate fails
- an empty selection with a candidate at any earlier position fails
- Peter's exact triggering message remains required as the final input item

At least one duplicate-context test must leave the expected penultimate
context fully valid while inserting another candidate earlier. The result must
be `trace_data_invalid`.

### Exact raw v1 schemas

- valid retrieval and selected-record objects with exactly the required keys
  succeed
- an unknown top-level retrieval key fails
- removing a required top-level retrieval key fails
- an unknown selected-record key fails
- removing a required selected-record key fails
- JSON object member reordering alone remains valid
- `memory_retrieval.token` with a distinctive sentinel value fails
- `memory_retrieval.selected[0].client_secret` with a distinctive sentinel
  value fails

For both secret-like unknown-field cases, test through the real ASGI route and
assert:

- HTTP status is 500
- error code is exactly `trace_data_invalid`
- `Cache-Control` is `no-store`
- neither the rejected field name nor its sentinel value appears in response
  bodies or headers

### Historical and safety regression

- a temporary Turn 11-shaped first-item context fails under the universal
  penultimate contract
- every fixture row is unchanged after the attempted trace
- no legacy position or configuration exception is accepted
- each newly invalid shape returns the stable route-level error without raw
  payload fragments, exception text, SQL, paths, or secrets
- successful trace response shape and privacy projection remain unchanged
- Trace performs no writes and queries no memory tables
- no provider client or network request is used

## Verification

Run the complete repository verification suite, including at minimum:

```text
python -m unittest discover -s tests
node --test tests/test_trace_ui.js
python -m compileall app tests
git diff --check
node --check static/app.js
```

Run any additional formatting, linting, JavaScript syntax, or JSON validation
checks established by the current `main` branch. Do not install dependencies,
access the network, inspect the live database, or make a live provider request
without separate authorization.

## Acceptance criteria

The amendment is complete only when:

1. Strict inherited-memory validation uses `request_event["raw_payload"]`
   before privacy projection.
2. Only privacy-projected data is browser-visible.
3. Rejected names and values never appear in client errors.
4. The complete raw input is scanned for the reserved prefix.
5. A nonempty selection requires exactly one candidate at `input[-2]`.
6. The candidate has exactly `role` and `content`, with role `user` and string
   content beginning with the reserved prefix.
7. An empty selection permits no candidate anywhere.
8. Duplicate, malformed, missing, and misplaced candidates fail closed.
9. The raw retrieval envelope accepts exactly the defined v1 key set.
10. Every raw selected record accepts exactly the defined v1 key set.
11. Secret-like unknown fields fail before projection rather than disappearing
    into an apparently valid audit.
12. Valid strict-contract traces retain their existing response shape and UI
    behavior.
13. Turn 11 remains byte-for-byte untouched and fails under the universal
    contract without a legacy branch.
14. Schema v1.2, runtime request construction, seeded-memory behavior, and
    immutable configurations remain unchanged.
15. The complete offline verification suite passes.
16. No live database access or live provider request occurs.

## Deliverables after separate implementation approval

- localized Trace validation changes
- focused raw cardinality, candidate-shape, and exact-schema regression tests
- secret-like unknown-field no-leakage ASGI tests
- a temporary Turn 11-position fixture proving fail-closed, write-free behavior
- a concise implementation report listing changed files and verification
  results and confirming no live database or provider access

