# Helios Room-Wide Canonical History Visibility v1

## Fresh-Database Reset Statement of Work

**Project:** Helios Room  
**Change:** Replace participant-scoped provider history with room-wide canonical chat history in a fresh database  
**Document version:** 2.3  
**Revision status:** Implementation-ready  
**Date:** 2026-08-14  
**Authoritative implementation baseline:** commit `d9d9717`, `feat: add Gemini participant integration v1`  
**Current active database schema:** 1.3  
**Fresh target database schema:** 1.4

### Revision 2.0 decision summary

This revision replaces the mixed-history migration design with a deliberate fresh-database reset.

1. The active schema 1.3 database is retired after a verified recoverable backup. It is not migrated.
2. The fresh schema 1.4 database contains no old messages, turns, routes, provider events, traces, participant configuration records, or imported memory rows. Its exact new foundation rows are defined independently in Section 6.7.
3. Every room begins with exactly one `room_shared_v1` policy event effective from room sequence 1.
4. Every new canonical chat message is visible to every selected provider. Addressing controls conversational destination and triggering, not visibility.
5. The previously omitted Gemini message is not hidden or reclassified. It is absent from the fresh active database with the rest of the retired history.
6. The provider-history implementation has no active legacy interval, no mixed-policy projection, no schema 1.3 replay requirement, and no Trace v2 requirement.
7. A selected-provider message is mechanically classified as native or external before native validation. The exact classifier is closed in Section 7.3.
8. Helios native replay uses a new shared-policy canonical turn contract and never queries OpenAI events. Gemini native replay additionally requires the exact Google provider events.

### Revision 2.1 correction summary

This revision closes the destructive-reset audit findings:

1. Implementation approval authorizes code and temporary-database tests only. Live planning and live execution each require a later separate instruction after audit, commit, and push.
2. Every application database connection participates in one cross-platform shared/exclusive maintenance lock. Reset holds the exclusive lease continuously from its first live identity check through success or rollback.
3. Reset planning and execution use canonical component checks, reparse/symlink/hard-link rejection, filesystem identity binding, immediate identity revalidation, and handle-relative quarantine/removal.
4. Pre-quarantine failure guarantees logical database contents are unchanged, not byte-for-byte sidecar preservation. File identity, size, nanosecond mtime, and stable SHA-256 observations are recorded before and after WAL-safe access.
5. Fresh initialization now has an exact closed row graph that deliberately preserves the current initializer's Peter, Helios, Gemini, `room-system`, memberships, aliases, bootstrap name events, and initial Helios configuration.
6. The guarded reset accepts exact valid schema 1.2 or 1.3 sources, using the applicable complete source/foundation validator.
7. Reset rollback and later bootstrap are separate contracts. A bootstrap failure leaves the already committed fresh schema 1.4 database intact for a separately authorized recovery decision.

### Revision 2.2 correction summary

This revision closes the remaining reset-design findings:

1. Planning is filesystem-only and makes no claim about SQLite schema, integrity, or logical eligibility. Exact source validation occurs only during separately authorized execution, before backup creation or quarantine.
2. Destructive reset transitions are protected by a durable, closed-schema journal at `data/.helios-room-reset-state.json`. Startup fails closed while journal state exists, and separately authorized recovery can either restore the source or complete an already validated fresh installation.
3. The reviewed plan is bound to the exact reset protocol, implementation commit, and closed schema-to-recovery-commit mapping. A closed retained audit artifact and the success report carry the selected recovery commit and non-content provenance.

### Revision 2.3 correction summary

This revision closes the final implementation-readiness finding:

1. The committed root `.gitignore` excludes `/backups/`, the database and sidecars, the maintenance lock, durable journal files, and every reset staging/quarantine artifact.
2. Plan, execution, and recovery verify that every reset-owned path is ignored before mutation.
3. Tests prove planning, successful execution, every injected crash stage, retained backup/audit files, and both recovery outcomes leave the tracked/nonignored worktree exactly as clean as it began.

## 1. Purpose

Helios Room is a shared conversation among Peter, Helios, Gemini, and future supported participants. A message destination identifies who is being addressed and which provider turn, if any, should be triggered. It is not a privacy boundary.

The schema 1.3 provider-history projection deliberately excludes canonical messages addressed outside a selected provider's supported dialogue. Rather than preserve that historical behavior through a complex per-message migration, this SOW retires the development database and starts canonical history again under one room-wide policy.

The central rule is:

> Every canonical chat message in the fresh database is part of the shared room history for every selected provider. Addressing says who was spoken to. It does not say who may know the message was spoken.

## 2. Required outcome

After the reset and fresh schema 1.4 initialization:

1. The active database contains no row copied from the retired schema 1.3 database.
2. The main room's only visibility-policy event is `room_shared_v1` effective from sequence 1.
3. Every future room receives the same event in the room-creation transaction.
4. Every valid canonical `chat` message is included exactly once and in room-sequence order in every selected provider's next bounded history.
5. Destination metadata remains canonical, immutable, visible in the browser, and available to provider-history projection.
6. A direct message triggers only its addressed provider while becoming visible to all providers on later calls.
7. No AI-to-AI fan-out or autonomous loop is introduced.
8. Seeded memory remains participant-scoped and separate from canonical chat history. Approved seeds may be reimported after reset from their source artifact.
9. The retired database remains recoverable from a verified backup but is never opened by schema 1.4 runtime code.

## 3. Authoritative product decisions

### 3.1 Reset instead of migration

There is no schema 1.3 to 1.4 data migration.

- No old canonical message, route, turn, API event, trace, participant installation, participant configuration, alias mutation, or seeded-memory import row is copied into the new database.
- No `participant_scoped_v1` policy event is created in schema 1.4.
- No cutover greater than 1 exists.
- No old request payload is rewritten or replayed.
- The old database is retained only as an offline backup paired with the schema 1.2- or 1.3-compatible code matching its recorded source label if recovery is ever required.

### 3.2 Shared room, directed speech

All of these are public room history in the fresh database:

- Peter to Helios;
- Helios to Peter;
- Peter to Gemini;
- Gemini to Peter;
- Peter to Room;
- Helios or Gemini to Room;
- Gemini to Helios or Helios to Gemini;
- a supported additional human or AI participant to any destination.

Route metadata answers "who is being addressed?" It never answers "who may see this?"

### 3.3 Memory remains separate

This SOW changes canonical chat replay only.

- Seeded memories remain participant-owned and participant-scoped.
- A seeded-memory source file is not part of the database backup or message history.
- Reimported seed rows retain their existing provenance and must not claim they were experienced in the fresh room.
- Memory retrieval results are not made room-wide.
- A memory record is never converted into a canonical chat message.

### 3.4 System records are not chat

Room-wide provider history applies to canonical messages whose `message_type` is `chat`.

Canonical system/control records remain validated but omitted from provider conversational input unless a later SOW defines a public representation. A canonical welcome publication that uses `message_type='chat'` is shared like every other chat message.

## 4. Scope

### 4.1 In scope

- Fresh schema 1.4 installation.
- A guarded reset command design for retiring an exact valid schema 1.2 or 1.3 database after separate live authorization.
- Verified, recoverable pre-reset backup.
- Root `.gitignore` coverage for every active database, reset control, backup, audit, staging, and quarantine path.
- One immutable room-wide visibility event at room sequence 1.
- Central message-insertion policy guard.
- Room-wide OpenAI and Google provider-history projection.
- Deterministic external envelopes for messages outside native replay.
- Exact mechanical native-candidate classification.
- New shared-history OpenAI and Google request validators.
- Trace v3 for the fresh database.
- Reinitialization of the exact closed room, participant, alias, membership, name-event, configuration, and policy graph in Section 6.7.
- Provider-free reimport of the approved seeded-memory source.
- Provider-free Gemini configuration installation and welcome publication when explicitly run; the Gemini identity and active membership already exist in the fresh foundation.
- Browser/API regression, tests, documentation, and controlled reset instructions.

### 4.2 Out of scope

- Schema 1.3 to 1.4 data migration.
- Preservation of active old messages, routes, turns, provider events, traces, configurations, identity mutations, or imported memory rows.
- `participant_scoped_v1` runtime support in schema 1.4.
- Mixed legacy/shared history projection.
- Legacy request replay or Trace v2 support in the fresh database.
- Private messages, ACLs, private channels, or participant-specific secrecy.
- Shared room-memory design.
- Provider fan-out, Gemini-to-Helios triggering, Helios-to-Gemini triggering, or autonomous AI loops.
- New providers or participant types.
- History truncation, summarization, token budgeting, or compaction.
- Changes to model choice, generation settings, tools, retry policy, or memory retrieval.
- Billable provider calls during implementation, automated tests, backup, reset, initialization, or audit.

## 5. Terminology

| Term | Definition |
| --- | --- |
| Canonical message | An immutable row in `messages` with its immutable route in `message_routes`. |
| Destination | The room or participant named by the canonical route. It expresses address and routing intent. |
| Provider history | The deterministic bounded projection of canonical room history sent to one selected AI provider. |
| Visibility policy event | An append-only row declaring the provider-history policy effective at a room sequence. |
| `room_shared_v1` | The only schema 1.4 policy. Every supported canonical chat message is included for every selected provider. |
| Native candidate | A selected-provider-authored message classified by the exact predicate in Section 7.3 and therefore required to pass native response validation. |
| External message | A valid chat message represented as attributed participant speech rather than the selected provider's native assistant/model response. |
| Retired database | The complete former schema 1.3 database preserved only in the verified pre-reset backup. |

## 6. Fresh schema 1.4 foundation

### 6.1 Policy table

Create `room_history_visibility_events` with exactly these logical fields:

| Field | Requirement |
| --- | --- |
| `id` | Integer primary key. |
| `room_id` | Required foreign key to `rooms(id)`. |
| `policy_version` | Required closed value `room_shared_v1`. |
| `effective_from_room_sequence_no` | Required integer equal to 1. |
| `created_at` | `TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))`. |

Required declarative constraints:

- unique `(room_id, effective_from_room_sequence_no)`;
- `policy_version = 'room_shared_v1'`;
- `effective_from_room_sequence_no = 1`;
- foreign-key enforcement;
- an index on `(room_id, effective_from_room_sequence_no)`;
- no nullable policy field;
- no free-form JSON policy definition;
- `typeof(created_at) = 'text'`;
- `created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'`;
- `strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL`;
- `strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at`.

Policy insertion SQL must omit `created_at`. SQLite supplies the exact UTC millisecond default. Application code, fixtures, environment values, and host-local time must not override it.

### 6.2 Only valid timeline shape

Every schema 1.4 room has exactly one policy event:

```text
room_shared_v1 effective_from_room_sequence_no = 1
```

No room may have zero events or more than one event. No other policy literal or effective sequence is legal. `room_shared_v1` is terminal for this milestone.

The governing event for every message is that room's single shared event. Every message must resolve to exactly one governing event before insertion and during every mature validation.

### 6.3 Policy immutability and insert trigger

Add these database triggers:

1. `BEFORE INSERT ON room_history_visibility_events`: allow insertion only when the room has no policy event, the room has no canonical message, the proposed policy is `room_shared_v1`, and the proposed effective sequence is 1. Abort every other insertion.
2. `BEFORE UPDATE ON room_history_visibility_events`: abort every update.
3. `BEFORE DELETE ON room_history_visibility_events`: abort every delete.

No API, reset, test helper, or maintenance path may disable or bypass these triggers in a valid schema 1.4 database.

### 6.4 Central message-insertion guard

Every application canonical-message write must use the centralized `store_message` boundary. Before `INSERT INTO messages`, it must:

1. validate the exact schema 1.4 foundation;
2. validate the target room's one-event timeline;
3. allocate the proposed room sequence using the existing transaction-safe allocator;
4. resolve the single governing event;
5. reject before insertion if any requirement fails.

Add a `BEFORE INSERT ON messages` defense trigger requiring the target room's exact `room_shared_v1` event at sequence 1. It uses `RAISE(ABORT, 'message_visibility_guard')` when the governing event is unavailable.

No message may be inserted first and assigned visibility later.

### 6.5 Exact message-guard error contract

The persistence exception type is exactly `app.database.MessageVisibilityGuardError`:

| Field | Exact value |
| --- | --- |
| `code` | `message_visibility_policy_unavailable` |
| `status_code` | `409` |
| `message` | `The room history visibility policy does not permit this message to be stored.` |

`store_message` performs the application guard first. If the defense trigger raises `sqlite3.IntegrityError` with the exact internal literal `message_visibility_guard`, `store_message` translates it to `MessageVisibilityGuardError` using `from None`. Any different integrity error retains its existing classification.

Raw SQLite text, trigger names, SQL, exception representations, tracebacks, and `message_visibility_guard` must never appear in an HTTP response, CLI output, provider-turn error, Trace payload, browser rendering, or public log field.

Caller mappings are exact:

| Caller class | Public mapping |
| --- | --- |
| Helios or Gemini Phase A/Phase C | `TurnServiceError`, HTTP 409, code `unsupported_history_visibility_policy`, message `Canonical history visibility cannot be reconstructed safely.` |
| Room-posting HTTP/API | HTTP 409 and exactly `{"error":"message_visibility_policy_unavailable","message":"The room history visibility policy does not permit this message to be stored."}`. |
| CLI `store-message` | Exit 1, empty stdout, and one compact JSON stderr line with the same `error` and `message`, followed by one newline. |
| Identity, adoption, rename, welcome, or other publication service | Preserve `MessageVisibilityGuardError` with its exact code, status, and message. HTTP/CLI wrappers use the mappings above. |
| Any other direct `store_message` consumer | Inventory and map explicitly to the provider-turn mapping when inside a provider turn, otherwise to the general domain/HTTP/CLI mapping. |

### 6.6 Mature schema validation

`validate_v14_foundation` must validate:

- exact schema migration history through schema 1.4;
- all schema 1.3 foundation objects retained by the fresh schema design;
- the policy table's exact columns, types, constraints, default, index, and triggers;
- the message defense trigger's exact approved definition;
- exactly one shared event at sequence 1 for every room, including empty rooms;
- no unsupported, missing, additional, updated, or retroactive policy evidence;
- exactly one governing event for every message;
- every policy timestamp using the exact calendar-aware check `datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") == value` and identical SQLite round-trip;
- integrity check and foreign-key check at installation, reset completion, and server preflight.

`validate_v12_source` and `validate_v13_foundation` may remain in the guarded reset path for validating an exact schema 1.2 or 1.3 source and its completed backup. No schema 1.4 runtime read, write, provider turn, Trace path, identity path, or memory path may call either legacy validator.

### 6.7 Fresh installation and room creation

Fresh `init-db` must:

1. accept only a missing or object-free database path;
2. install the complete schema 1.4 foundation;
3. record the canonical schema history through migration number 3 and label 1.4;
4. create the main room and its only `room_shared_v1` event at sequence 1 in one transaction;
5. create the exact closed foundation row graph below in the same installation transaction;
6. insert no chat message, message route, turn, provider event, trace evidence, Gemini configuration, or imported memory record;
7. run mature validation, integrity check, and foreign-key check before success.

The fresh foundation row graph is exactly:

| Table/domain | Exact rows after initialization |
| --- | --- |
| `rooms` | Exactly one row: `room_key='main'`, `name='The Room'`. |
| `participants` | Exactly four rows: (`peter`, `Peter`, `human`), (`helios`, `Helios`, `ai`), (`gemini`, `Gemini`, `ai`), and (`room-system`, `Room`, `system`). |
| `room_participants` | Exactly three active memberships in `main`: Peter, Helios, and Gemini. Each has `joined_at` from the schema default and `left_at IS NULL`. `room-system` has no active or historical membership row. |
| `participant_aliases` | Exactly one immutable bootstrap alias per participant: `Peter`/`peter`, `Helios`/`helios`, `Gemini`/`gemini`, and `Room`/`room`. Each alias is owned by its matching participant. |
| `participant_primary_aliases` | Exactly one row per participant selecting that participant's sole bootstrap alias. |
| `participant_name_events` | Exactly one `bootstrap` event per participant. For each event: `room_id IS NULL`, actor and subject are that same participant, `previous_alias_id IS NULL`, `new_alias_id` is its bootstrap alias, and `canonical_message_id IS NULL`. |
| `participant_configs` | Exactly one row, owned by Helios: `provider='openai'`, `model IS NULL`, `config_label='initial'`, `system_instructions IS NULL`, `settings_json='{}'`, and `tools_json='[]'`. |
| `room_history_visibility_events` | Exactly one row for `main`: `policy_version='room_shared_v1'`, `effective_from_room_sequence_no=1`, with `created_at` supplied by the exact database default. |
| Canonical/provider domains | Zero rows in `turns`, `messages`, `message_routes`, `api_events`, and every Trace-derived persisted domain. |
| Memory domains | Zero imported seeded-memory rows, retrieval rows, and memory-audit rows. |

Auto-generated integer IDs and database-default timestamps are not fixed literals, but all foreign-key ownership and cardinality relationships above are exact. No additional room, participant, alias, membership, name event, configuration, policy event, message, turn, provider event, or memory row is permitted immediately after initialization.

Preserving the initial Helios configuration is intentional. Removing it is not part of this SOW. Creating the Gemini identity, bootstrap alias, primary alias, bootstrap name event, and active membership during initialization is also intentional and is not delayed. The later Gemini installation command may create only Gemini's provider configuration and approved welcome publication; it must not create a second Gemini participant, alias bootstrap, name bootstrap, or membership.

The schema 1.4 history row is exactly:

| Field | Exact value |
| --- | --- |
| `migration_no` | `3` |
| `schema_label` | `1.4` |
| `description` | `Fresh room-wide canonical history visibility foundation.` |

Every future room-creation path creates its shared event at sequence 1 in the same transaction as the room.

### 6.8 Old-database dispatch

Schema 1.4 application code never upgrades an existing database in place.

| Database state | `init-db` | `migrate-database` | Server preflight |
| --- | --- | --- | --- |
| Missing or object-free | Install fresh 1.4 | Fail `migration_state_invalid` | Existing missing-database behavior |
| Exact schema 1.4 | Validate and return existing-database result | Validate and return `already_current` | Accept after mature validation |
| Exact schema 1.2 or 1.3 | Fail `database_reset_required` | Fail `database_reset_required` with zero writes | Fail `database_reset_required` |
| Any other history/objects | Existing incompatible-database error | `migration_state_invalid` | Existing incompatible-database error |

Stable reset-required message:

```text
The existing Helios Room database must be retired and reinitialized before this version can run.
```

The guarded reset implementation in Section 13 accepts an exact valid schema 1.2 or 1.3 source. This SOW does not itself authorize running it against live data.

### 6.9 Schema 1.4 runtime consumers

Every runtime foundation consumer must use and test `validate_v14_foundation`, including:

- database initialization/readiness;
- server preflight;
- identity, alias, adoption, rename, and addressing operations;
- room posting;
- Helios Phase A and Phase C;
- Gemini installation/readiness, Phase A, and Phase C;
- Gemini welcome publication;
- seeded-memory import, search, audit, and owner validation;
- Trace latest and specific reads;
- every CLI/API wrapper reaching those services.

A repository-wide assertion must fail if any module outside the guarded reset source/backup validation path calls `validate_v12_source` or `validate_v13_foundation`.

## 7. Provider-history projection

### 7.1 Common requirements

`load_provider_history` is a deterministic projection of a canonical room prefix bounded by `room_sequence_no`.

For every row in the prefix it must:

1. validate the canonical message, sender, identity, aliases, configuration ownership when required, and route;
2. resolve the room's exact shared event;
3. validate the message type;
4. apply the selected provider's total mapping;
5. preserve canonical room-sequence order;
6. include every valid chat message exactly once;
7. omit only valid system/control records;
8. fail before provider construction on unknown participant types, invalid canonical evidence, or invalid native candidates.

There is no `participant_scoped_v1` projection and no mixed-policy dispatch in schema 1.4.

### 7.2 Supported sender set

`room_shared_v1` is total over:

- every canonical participant with `participant_type='human'` and valid immutable route aliases;
- every canonical participant with `participant_type='ai'`, valid immutable route aliases, and valid configuration ownership when the canonical message schema requires an AI configuration;
- `room-system` only for valid system/control records, which are validated and omitted.

Additional supported human or AI participants appear as external messages without a provider-specific code change. Unknown participant types fail closed.

### 7.3 Exact selected-provider native-candidate classifier

Classification occurs before native-response validation and uses only the canonical message and canonical route. Provider events never participate in classification.

For the selected provider `S` and Peter `P`, a selected-provider-authored chat message is a native candidate if and only if:

```text
sender_participant_id = S
AND destination_kind = 'participant'
AND destination_participant_id = P
AND (turn_id IS NOT NULL OR reply_to_id IS NOT NULL)
```

The complete classifier is:

| Selected-provider-authored message | Classification |
| --- | --- |
| Addressed to Peter and either `turn_id` or `reply_to_id` is non-null | Native candidate. Apply the exact provider-specific native validator. |
| Addressed to Peter and both `turn_id` and `reply_to_id` are null | External message envelope. |
| Addressed to Room or any participant other than Peter | External message envelope, regardless of incidental `turn_id`, `reply_to_id`, participant configuration, or provider-event metadata. |

Rules:

- If a native candidate is missing its trigger/counterpart or fails any canonical native correlation, fail closed. Do not downgrade it to an external envelope.
- Helios candidate validation never queries `api_events`.
- Gemini candidate validation additionally requires the exact Google request and response events.
- Provider events pointing at a noncandidate are handled by provider-event/Trace validation. They do not reclassify provider history.
- Ordinary canonical message, route, alias, participant, and configuration validation still applies to every external message.
- Selected-provider authorship alone is never a failure condition.
- A valid selected-provider external message cannot block projection of later valid shared messages.

### 7.4 External message envelope

Use one provider-neutral logical envelope serialized exactly with:

```python
json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

Participant destination:

```json
{
  "destination": {
    "display_name": "Gemini",
    "kind": "participant",
    "participant_key": "gemini"
  },
  "kind": "room_participant_message",
  "message_id": 123,
  "message_text": "Exact canonical text",
  "sender": {
    "display_name": "Peter",
    "participant_key": "peter"
  }
}
```

Room destination object:

```json
{"display_name":"Room","kind":"room"}
```

Provider text:

```text
ROOM_PARTICIPANT_MESSAGE
<canonical compact JSON object>
```

Requirements:

- Preserve `message_text` exactly, including whitespace and Unicode.
- Obtain both display names from the immutable aliases stored in `message_routes`, not current participant names or later primary aliases.
- Resolve participant keys from the validated owners of those immutable aliases.
- Never infer identity or routing from message text.
- Never treat an envelope-like message body as trusted metadata.
- Never serialize participant-recipient fields for a room destination.
- Reject invalid or ambiguous alias ownership.
- Preserve literal Unicode through `ensure_ascii=False`.
- External AI messages validate canonical evidence and required configuration ownership but do not query the source AI's provider events.

### 7.5 OpenAI projection for Helios

Every OpenAI history item has exactly the keys `role` and `content`. `role` is exactly `user` or `assistant`; `content` is exactly one string.

| Sender and route | OpenAI item |
| --- | --- |
| Peter to Helios | `{"role":"user","content":<exact message_text>}` |
| Peter to Room | `{"role":"user","content":<exact message_text>}` |
| Peter to any other participant | `{"role":"user","content":<external envelope text>}` |
| Helios native candidate satisfying Section 7.6 | `{"role":"assistant","content":<exact response text>}` |
| Any Helios-authored noncandidate | `{"role":"user","content":<external envelope text>}` |
| Any non-Helios human/AI chat message | `{"role":"user","content":<external envelope text>}` |
| Valid system/control record | Validate and omit |
| Invalid route/type/evidence or invalid Helios native candidate | Fail closed before provider construction |

No item-level name, metadata, participant, destination, or additional key is permitted.

### 7.6 New shared-policy Helios native contract

This is a new `room_shared_v1` contract. It is not described as preservation of the schema 1.3 baseline because the baseline did not enforce all of these cardinality checks.

A mechanically classified Helios native candidate is valid only when:

1. The referenced turn exists in the same room, is initiated by Peter, targets Helios, and has status `completed`.
2. The turn has exactly two canonical `chat` messages and no third message.
3. Turn sequence 1 is Peter's trigger, has no participant configuration, and has exactly one explicit participant route Peter to Helios.
4. Turn sequence 2 is the Helios response, has a Helios-owned `participant_config_id` whose provider is exactly `openai`, and has exactly one explicit participant route Helios to Peter.
5. Both messages have the turn's exact `turn_id`.
6. The response's `reply_to_id` equals the trigger message ID.
7. Room sequence, turn sequence, sender, destination, immutable alias ownership, message type, participant configuration, and reply correlations are exact.
8. Projected assistant content equals the response's exact canonical text.

A missing counterpart, wrong cardinality, extra message, missing/wrong reply, wrong turn/status/sequence/route/alias/configuration, or non-OpenAI Helios configuration fails closed.

`load_provider_history` must not query OpenAI `api_events` for classification or Helios native validation. Missing, redacted, additional, or corrupt OpenAI events do not affect Helios provider-history projection. OpenAI request-event integrity remains independently mandatory in request validation and Trace.

### 7.7 Google projection for Gemini

Every Google content has exactly the keys `role` and `parts`.

- Native/external user content is exactly `{"role":"user","parts":[{"text":<string>}]}`.
- Reconstructed model content is exactly `{"role":"model","parts":[...]}`.
- Every user part has exactly `text`.
- Every model part has only `text`, optional `thought`, and optional `thought_signature_b64`, with existing closed validation.

| Sender and route | Google content |
| --- | --- |
| Peter to Gemini | Native user content with exact message text |
| Peter to Room | Native user content with exact message text |
| Peter to any other participant | User content containing the external envelope |
| Gemini native candidate satisfying Section 7.8 | Native model content reconstructed from exact Google evidence |
| Any Gemini-authored noncandidate | User content containing the external envelope |
| Any non-Gemini human/AI chat message | User content containing the external envelope |
| Valid system/control record | Validate and omit |
| Invalid route/type/evidence or invalid Gemini native candidate | Fail closed before provider construction |

### 7.8 Gemini native contract

A mechanically classified Gemini native candidate must satisfy the same exact canonical two-message, completed-turn, route, reply, alias, sequence, and Gemini-owned configuration correlations as Section 7.6 with provider `google`.

It must additionally have:

1. exactly one matching Google request event at event sequence 1;
2. exactly one matching successful Google response event at event sequence 2;
3. no additional request or terminal event for that turn;
4. a closed valid request payload whose bounded contents equal the exact expected provider-history prefix;
5. exact event/turn/trigger/response/configuration/model correlations;
6. exact response parts, text, thought flags, and canonical thought-signature evidence.

Missing, additional, redacted where cleartext replay is required, misordered, mismatched, or malformed Google evidence fails closed. It is never downgraded to an external envelope.

A valid Gemini noncandidate does not query Google events even if unrelated provider events point at it. Those events are assessed only by event/Trace validation.

### 7.9 Triggering

Visibility does not cause fan-out.

- Peter to Helios triggers only OpenAI.
- Peter to Gemini triggers only Google Gemini.
- Peter to Room follows the existing room-post behavior.
- Historical inclusion never triggers a provider.
- AI-authored messages never automatically trigger another AI through this SOW.

## 8. Shared request-event contracts

### 8.1 Required visibility evidence

Every provider request includes this exact object inside `local_context`:

```json
"history_visibility": {
  "active_policy_version": "room_shared_v1",
  "effective_from_room_sequence_no": 1,
  "projection_version": "provider_history_v2"
}
```

The object has exactly three keys. The effective sequence is derived from the validated room event, even though its only legal value is 1. It is never derived from configuration, environment, software version, or current message count.

### 8.2 OpenAI shared request validator

The top-level event payload has exactly `local_context` and `request`.

`request` has exactly:

```text
model, instructions, input, store, reasoning, max_output_tokens, tools
```

`local_context` has exactly:

```text
provider, operation, trigger_message_id, room_sequence_boundary,
timeout_seconds, max_retries, memory_retrieval, history_visibility
```

The validator enforces all current fixed literals, types, bounds, input item key sets, memory-audit schema, trigger/boundary/configuration correlations, exact shared provider history, and the exact visibility object.

### 8.3 Google shared request validator

The top-level payload has exactly `local_context` and `request`.

`request` has exactly `config`, `contents`, and `model`.

`local_context` has exactly:

```text
api_version, memory_retrieval, operation, provider,
room_sequence_boundary, safety_settings, sdk_policy,
timeout_seconds, total_attempts, trigger_message_id, history_visibility
```

The validator enforces the current closed Google config, contents, parts, bounded-value, safety, SDK, memory-audit, trigger/boundary/configuration, and exact shared-history requirements.

### 8.4 Dispatch and errors

Schema 1.4 request validation dispatches only by provider family after resolving the canonical trigger's exact shared policy:

- OpenAI request event calls only the OpenAI shared validator.
- Google request event calls only the Google shared validator.

The payload cannot select a validator. Missing policy, missing/extra visibility evidence, provider-family mismatch, or exact-history mismatch is corruption.

Stable provider-history policy error:

```text
code: unsupported_history_visibility_policy
status: 409
message: Canonical history visibility cannot be reconstructed safely.
```

Policy corruption precedes route, native replay, memory, environment, and provider-configuration errors.

## 9. Trace v3

### 9.1 Version contract

Every turn in the fresh schema 1.4 database produces Trace version 3, including a local/human-only turn without a provider request. Trace validates the room's exact shared event in the same query-only snapshot.

Trace v3 top-level keys are exactly:

```text
trace_version, turn, messages, configurations, recorded_request,
inherited_memory, provider_outcome, api_events
```

There is no top-level `history_visibility` field.

### 9.2 Unredacted request projection

For an unredacted request event:

- the applicable shared validator accepts the persisted payload before privacy projection;
- `api_events[].payload.local_context.history_visibility` contains the exact three-key object;
- `recorded_request.local_context.history_visibility` contains a deeply equal copy;
- no third copy is added;
- `recorded_request` retains exactly `event_id`, `event_sequence_no`, `is_redacted`, `redaction_reason`, `request`, `local_context`, and `omitted_json_pointers`.

### 9.3 Redacted request projection

For a valid redacted request event:

- Trace remains version 3 from canonical policy;
- projected `api_events[].payload` is `null`;
- `recorded_request.request` is `null`;
- `recorded_request.local_context` is `null`;
- no visibility object is synthesized;
- existing redaction identity, reason, sequence, correlation, cardinality, and omission-pointer behavior remains exact.

### 9.4 Provider events pointing at noncandidates

Provider events never reclassify provider history. If a provider event points at a message that Section 7.3 classifies as a noncandidate:

- provider-history projection treats the message according to its external classification;
- Trace/event validation independently evaluates the event's legality and correlations;
- invalid event evidence fails Trace with `trace_data_invalid` but does not change the message into a native candidate.

### 9.5 Privacy and browser display

Trace continues to use a read-only query connection, `query_only`, and one consistent snapshot. It exposes no credentials, environment values, raw provider exceptions, private HTML, or rejected raw data.

The browser accepts Trace v3 and displays:

- unredacted: `Room-wide history`, policy version, effective sequence 1, and projection version;
- redacted: `Room-wide history; request details redacted`.

Participant-directed messages must never be labeled private.

## 10. Room history API and UI

The API and browser return every canonical chat message in room-sequence order regardless of destination.

- Preserve sender and destination labels, for example `Gemini -> Peter`.
- Do not hide, dim, or classify directed messages as private.
- Preserve exact canonical text.
- Do not duplicate messages for provider visibility.
- Do not synthesize chat messages from provider events.
- Preserve participant selection and `/trace` behavior.

## 11. Transaction and environment ordering

### 11.1 Provider Phase A

Both provider services use this exact order:

1. Open the database and acquire the existing `BEGIN IMMEDIATE` write transaction.
2. Validate exact schema 1.4 history and required objects.
3. Validate the complete one-event timeline for every room.
4. Resolve the main room, destination, participant installation, aliases, and identity evidence.
5. Create the tentative turn and insert Peter's trigger through guarded `store_message`.
6. Fix the trigger's room-sequence boundary and project exact bounded shared history.
7. Retrieve seeded memory and construct its existing audit envelope.
8. Call the selected provider environment/settings loader exactly once. It may call `load_dotenv` exactly once with existing non-overriding semantics.
9. Validate credentials, model, settings, and configuration identity.
10. Construct and validate the exact shared request.
11. Insert the request event and commit.

Policy, route, projection, native-replay, or memory rejection performs zero environment loads, dotenv loads, protected-variable reads, client constructions, and provider invocations.

If history is valid but provider configuration is missing, the environment loader runs exactly once, the stable existing configuration error is returned, and the entire tentative turn/message transaction rolls back.

### 11.2 Provider Phase C

Every completion/failure finalizer validates schema 1.4 without environment access. A successful response message is inserted through the same visibility guard. Existing provider outcome semantics remain unchanged except where the new native contracts require exact canonical cardinality.

### 11.3 Concurrency

Tests must prove:

- a room cannot receive a message before its policy event exists;
- a message inserted after Phase A remains outside that turn's fixed boundary;
- a policy event cannot be added, updated, deleted, or replaced after room creation;
- WAL readers cannot observe a room without its required event;
- reset cannot run while any application database connection or server process is active;
- server startup and every connection factory fail closed while the exclusive maintenance lease is held.

### 11.4 Cross-platform maintenance lock

Add one application-owned lock file at the exact repository-relative path:

```text
data/.helios-room-database.lock
```

The lock file is a permanent regular coordination file, not a database sidecar and not a destructive reset target. Create it with owner-only write permissions where the platform supports them. Open it with no-follow semantics, reject a symlink, junction, reparse point, directory, device, or multi-link file, and validate its filesystem identity after opening.

The lock implementation uses operating-system byte-range/file locks without a new dependency:

- POSIX: `fcntl.flock` shared and exclusive nonblocking leases on the opened lock-file descriptor.
- Windows: `LockFileEx` through the standard library/`ctypes`, using a shared lease for normal operation and `LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY` for maintenance, covering the same fixed byte range.

Lease rules:

1. Server startup acquires a shared lease before opening or preflighting the database and holds it for the complete server-process lifetime.
2. Every application CLI/database connection factory, reader, and writer acquires a shared lease before opening the database and holds it until every associated SQLite connection is closed.
3. Reset planning acquires an exclusive lease for its short filesystem-only identity snapshot and releases it after producing the plan.
4. Reset execution or recovery acquires an exclusive lease before its first live path/identity check and holds the same open-file lease continuously through source validation, backup verification, journal transitions, quarantine, fresh installation, validation, cleanup, terminal audit installation, journal removal, success reporting, or recovery completion.
5. Failure to acquire the required lease returns sanitized code `database_maintenance_in_progress`, message `The Helios Room database is unavailable during maintenance.`, and status/exit behavior matching the caller's existing database-unavailable contract.
6. The reset code must not release and reacquire its exclusive lease between closing SQLite connections and replacing files.

All application entrypoints must honor this mechanism. A noncooperating external SQLite/file process remains prohibited by the controlled execution procedure; the operator must stop the server and all such tools before planning or execution.

## 12. Implementation work packages

### Work package A: fresh schema and reset command

- Add schema 1.4 fresh-install SQL.
- Add the policy table, exact timestamp rules, triggers, indexes, and message defense trigger.
- Add mature schema 1.4 validation.
- Add `database_reset_required` dispatch for old databases.
- Add the shared/exclusive maintenance-lock module and require every application database connection/startup path to use it.
- Add the exact separately authorized plan/execution reset surfaces in Section 13.
- Add canonical component, reparse/symlink/hard-link, filesystem-identity, TOCTOU, quarantine, WAL-safe observation, and rollback enforcement.
- Add the exact durable reset journal, startup interlock, recovery surfaces, retained audit artifact, and commit/protocol binding in Sections 13.5 through 13.7.
- Update the committed root `.gitignore` with the exact Section 13.2 reset-artifact block and enforce clean-worktree invariance across plan, execution, crash, and recovery.
- Accept exact valid schema 1.2 and 1.3 reset sources through their applicable complete validators.
- Update every schema-foundation consumer.
- Remove schema 1.3 to 1.4 data-migration work from this milestone.

### Work package B: message policy and projection

- Add the shared policy resolver and centralized guard.
- Add `MessageVisibilityGuardError` and all caller mappings.
- Replace active participant-scoped projection with room-shared projection.
- Add the exact native-candidate classifier before native validation.
- Add external self-route envelopes.
- Add the new Helios canonical native contract with no OpenAI event query.
- Retain exact Gemini provider-event replay for native candidates.

### Work package C: request evidence and Trace

- Add the exact shared OpenAI and Google validators.
- Require visibility evidence in every new request.
- Make Trace v3 the fresh schema contract.
- Keep provider events from influencing native-candidate classification.
- Update Trace UI tests.

### Work package D: bootstrap and documentation

- Reinitialize the exact closed Section 6.7 foundation graph, including Gemini, `room-system`, three memberships, four bootstrap identity graphs, the initial Helios configuration, and shared policy.
- Document provider-free seeded-memory reimport.
- Document provider-free Gemini installation/welcome.
- Update README reset, backup, recovery, and visibility behavior.
- Remove statements implying directed messages are private.

## 13. Guarded database reset design

### 13.1 Authorization boundary

This SOW authorizes implementation, documentation, audit, and automated testing of reset behavior with temporary database fixtures only. It does not authorize reading, planning, backing up, quarantining, deleting, replacing, initializing, or bootstrapping the live `data/helios.db` database.

Live reset is a later controlled operation with these mandatory authorization boundaries:

1. Finish implementation and provider-free verification.
2. Complete the read-only implementation audit.
3. Commit and push the approved implementation.
4. Receive a separate explicit instruction from Peter to run the live filesystem-only `reset-database --plan` operation.
5. Return the exact filesystem-eligibility plan, including proposed backup and audit paths, database path, filesystem identity, implementation/recovery commit bindings, and plan token, without opening SQLite, classifying the schema, or changing database/sidecar contents.
6. Allow Peter to review that plan.
7. Receive another explicit instruction from Peter authorizing execution of that exact plan token and exact proposed backup and audit paths.

Approval of this SOW, implementation, tests, audit, commit, push, or plan generation does not imply step 7 authorization. Billable provider calls always require an additional separate instruction.

### 13.2 Exact plan and execution surfaces

The filesystem-only plan form is:

```text
reset-database --plan --database data/helios.db
```

It acquires the exclusive maintenance lease, performs only the Section 13.3 filesystem checks/fingerprints, proposes the backup path, prints the closed plan JSON, and releases the lease. It must not open SQLite, create the backup, create a plan file, change a sidecar, or mutate the database. Running it against live data requires the separate step 4 instruction above.

Planning validates filesystem eligibility only. A successful plan means only that the exact configured paths, file types, identities, link counts, timestamps, sizes, stable byte digests, implementation identity, and proposed output paths were safe and could be bound for later review. Planning must not call SQLite, parse a SQLite header for semantic classification, run a schema/foundation validator, run integrity or foreign-key checks, compute a logical digest, or claim that the source is schema 1.2, schema 1.3, noncorrupt, or reset-eligible. Filesystem-eligible schema 1.2, 1.3, 1.4, incompatible, and corrupt files may all produce plans. Execution alone determines source eligibility before it creates any backup, journal, audit artifact, quarantine file, or fresh database.

The destructive execution form is:

```text
reset-database --execute \
  --database data/helios.db \
  --expected-plan-token <64-lowercase-hex> \
  --expected-backup-path <exact-repository-relative-path> \
  --expected-audit-path <exact-repository-relative-path> \
  --confirm-destroy-canonical-history
```

Execution requires every literal argument. It recomputes the complete plan manifest under a newly acquired exclusive maintenance lease and fails `reset_plan_stale` with zero database/sidecar/backup/audit mutation if the token, backup path, audit path, any path component, filesystem identity, link count, file type, existence state, size, nanosecond mtime, or stable digest differs.

The plan and execution run only from a clean Git worktree at the exact checked-out implementation commit. `implementation_commit` is the full lowercase hexadecimal Git object ID returned by `git rev-parse HEAD`, with length 40 for a SHA-1 repository or 64 for a SHA-256 repository. `reset_protocol_version` is exactly `room_shared_reset_v1`. The implementation contains one closed immutable `RECOVERY_COMMIT_BY_SCHEMA` mapping with exactly the keys `1.2` and `1.3`; each value is a full 40- or 64-character lowercase hexadecimal commit ID audited as capable of opening that source schema. The plan exposes both concrete values for review and binds them into its token. Execution fails `reset_plan_stale` before SQLite opens if the protocol, current implementation commit, worktree-clean state, or either mapping value differs. After source validation, the matching value becomes the journal, audit, and report `recovery_commit`.

#### 13.2.1 Git-ignore and clean-worktree contract

Before implementation is committed, update the repository-root `.gitignore` to contain this exact reset-owned block:

```gitignore
# Helios Room database and reset artifacts
/backups/
/data/helios.db
/data/helios.db-wal
/data/helios.db-shm
/data/.helios-room-database.lock
/data/.helios-room-reset-state.json
/data/.helios-room-reset-state.json.next
/data/.helios*.reset-*
```

`/backups/` covers every retained backup, retained `.audit.json`, backup partial, audit partial, and recovery staging artifact. `/data/.helios*.reset-*` covers every original quarantine, failed-new quarantine, restoration staging, and future reset-protocol-v1 temporary name in `data`. Reset code must not create a temporary, partial, quarantine, journal, lock, backup, audit, or recovery artifact outside these ignored path families.

Plan, execution, and recovery must each perform a no-mutation Git check before maintenance-lock bootstrap, then repeat the same check while holding the required lease before SQLite access or any other reset-owned filesystem mutation:

1. `.gitignore` is the tracked blob from the exact clean `implementation_commit`.
2. `git status --porcelain=v1 --untracked-files=all --ignore-submodules=none` produces zero bytes.
3. `git check-ignore --no-index` classifies every exact path the operation may create, including paths that do not yet exist, as ignored by the committed root `.gitignore`.
4. No reset-owned path is tracked in the implementation commit or index.

The prospective-path check includes the database and sidecars, maintenance lock, both journal paths, every computed quarantine/failed-new/restoration name, exact backup and audit paths, and every computed partial/staging name. If the permanent lock file does not yet exist, it may be created only after the no-mutation check proves its exact prospective path ignored; the complete check is then repeated under the acquired lease. Failure returns `reset_git_safety_invalid` before SQLite, backup, journal, audit, quarantine, or fresh-database creation. Recovery applies the same check before interpreting clean-worktree state, so ignored crash artifacts never make the required clean implementation worktree appear dirty. Unrelated tracked changes, staged changes, or nonignored untracked files still fail the existing clean-worktree precondition.

At every normal return, caught failure, injected crash checkpoint, and completed recovery checkpoint that a test process can observe, the tracked/nonignored status output must exactly equal the captured pre-operation output. Runtime commands must never edit `.gitignore`, call `git add` or `git add -f`, modify the index, commit, stash, clean, or delete unrelated untracked files. Adding the ignore block is implementation work completed, audited, committed, and pushed before any separately authorized live plan.

The plan JSON has exactly:

```json
{
  "audit_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.audit.json",
  "backup_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.db",
  "database_identity": "<platform-canonical-identity>",
  "database_path": "data/helios.db",
  "database_sha256": "<64 lowercase hex characters>",
  "implementation_commit": "<full 40- or 64-character lowercase Git object ID>",
  "plan_token": "<64 lowercase hex characters>",
  "recovery_commit_by_schema": {
    "1.2": "<full 40- or 64-character lowercase Git object ID>",
    "1.3": "<full 40- or 64-character lowercase Git object ID>"
  },
  "reset_protocol_version": "room_shared_reset_v1",
  "status": "planned"
}
```

All top-level and nested keys are lexicographically sorted. The plan token is SHA-256 over canonical compact JSON with UTF-8 and `ensure_ascii=False` containing the verified repository-root identity, `data` identity, `backups` identity or recorded absence, database/sidecar existence and identities, types, link counts, sizes, nanosecond mtimes, stable SHA-256 observations, exact proposed backup and audit paths, exact reset protocol, exact implementation commit, exact tracked `.gitignore` blob identity, zero-byte worktree-status assertion, prospective ignore-check results for every reset-owned path, and exact recovery-commit mapping. The token input excludes only `plan_token` itself and `status`. In reset protocol v1, journal/audit field `plan_manifest_sha256` equals this same 64-character `plan_token`. The backup and audit paths share one UTC timestamp that binds the plan time. The platform-canonical database identity is `posix:<st_dev>:<st_ino>` or `windows:<volume-serial-hex>:<file-id-128-hex>`.

The proposed backup timestamp uses UTC with three fractional digits and this exact form:

```text
backups/helios-pre-room-shared-reset-YYYYMMDDTHHMMSSsssZ.db
backups/helios-pre-room-shared-reset-YYYYMMDDTHHMMSSsssZ.audit.json
```

Plan and execution accept only the repository's exact configured database path. Planning fails closed for missing/unsafe filesystem targets but does not distinguish schema or logical corruption. Execution fails closed for missing confirmation/expected values, a stale filesystem or code binding, exact schema 1.4, incompatible/corrupt source, or unexpected path. Execution performs exact source validation after plan recomputation and before creating a backup, durable journal, audit artifact, quarantine file, or fresh database.

Reset CLI failures write exactly one compact `{"error":<code>,"message":<message>}` object plus one newline to stderr, write nothing to stdout, return exit 2 only for missing required confirmation/plan arguments, and otherwise return exit 1. The closed reset-specific public errors are:

| Condition | Code | Message |
| --- | --- | --- |
| Missing execution confirmation or expected plan arguments | `reset_confirmation_required` | `Reset execution requires the exact reviewed plan and explicit confirmation.` |
| Maintenance lease unavailable | `database_maintenance_in_progress` | `The Helios Room database is unavailable during maintenance.` |
| Unsafe target/path/identity/link/reparse condition | `reset_path_unsafe` | `The database reset target could not be verified safely.` |
| Plan manifest no longer exact | `reset_plan_stale` | `The reviewed database reset plan no longer matches the live files.` |
| Required ignore/worktree safety contract fails | `reset_git_safety_invalid` | `The database reset Git safety contract is not satisfied.` |
| Source is missing, unsupported, or fails its exact validator | `reset_source_invalid` | `The database is not an eligible reset source.` |
| Durable journal or pending journal update exists | `database_reset_recovery_required` | `The Helios Room database requires reset recovery before it can be opened.` |
| Journal/audit binding or requested recovery action is invalid | `reset_recovery_invalid` | `The database reset recovery state could not be verified safely.` |
| Backup, quarantine, installation, validation, cleanup, or rollback failure | `reset_failed` | `The database reset did not complete safely.` |

No failure exposes an absolute path, filesystem identity, SQL, SQLite text, canonical content, credential, environment value, exception representation, or traceback. The separately authorized successful plan is the only surface that returns the approved repository-relative paths and opaque identity/token values.

### 13.3 Canonical path and filesystem identity safety

The implementation must validate path structure without following an untrusted final component:

1. Derive the repository root from the installed application location, not current working directory, environment substitution, or user-relative expansion.
2. Walk and `lstat`/handle-inspect every component from the repository root through `data`, `backups`, the database, sidecars, staging backup, and quarantine names.
3. Reject any symbolic link, junction, mount redirection, Windows reparse point, directory at a file target, non-regular file, device, socket, FIFO, or other special file.
4. Require the repository root and `data` directory to retain their captured filesystem identities throughout the operation. If `backups` exists during planning, require it to retain its captured identity. If it is absent during planning, bind that absence into the plan token; execution may create exactly `backups` with a verified-parent `mkdirat`/handle-relative operation only after source validation, must fail if the path appeared meanwhile, and must capture and retain the new directory identity.
5. Require the main database and every existing sidecar to have exactly one hard link. On POSIX validate `st_nlink == 1`; on Windows validate the opened handle's link count equals 1.
6. Capture POSIX identity as `(st_dev, st_ino)` and Windows identity as `(volume serial number, FILE_ID_128)` from opened handles.
7. Open files with no-follow/open-reparse-point semantics and compare handle identity with the immediately preceding path observation.
8. Revalidate parent identity and target identity immediately before every staging creation, atomic rename to quarantine, restoration, and deletion.
9. Use handle/verified-parent-relative operations. On POSIX use directory descriptors and `*at` operations; on Windows use verified file/directory handles and handle-based rename/delete APIs.
10. Never use a glob, recursive removal, unresolved environment variable, home-relative target, or string-only resolved-path equality as the destructive authority.

The only active paths that may be quarantined or replaced are:

```text
data/helios.db
data/helios.db-wal
data/helios.db-shm
```

The exact reset-control paths are `data/.helios-room-reset-state.json` and `data/.helios-room-reset-state.json.next`. They are never treated as SQLite files or ordinary quarantine targets. Every plan, execution, startup, normal connection, and recovery path must inspect them with the same verified-parent, no-follow, regular-file, one-link, and opened-handle identity rules. An unexpected type, identity substitution, or unparseable control file fails closed; ordinary runtime never removes or repairs either path.

Destructive replacement begins by atomically renaming each existing verified target within the same verified `data` directory to a unique hidden quarantine name. Immediately verify that each quarantined file has the planned identity. If any identity differs, restore every already-renamed file to its original name while holding the exclusive lease and abort before schema installation. Do not unlink the active paths first.

### 13.4 WAL-safe source access and preservation guarantee

Reset execution holds the exclusive maintenance lease before opening SQLite. It reuses the approved sidecar-aware read selector:

| Observed state | Permitted source opening |
| --- | --- |
| Main + WAL + SHM | SQLite URI `mode=ro`, `PRAGMA query_only=ON`, one caller-owned read transaction. |
| Main with neither sidecar | Only the existing clean WAL-main `mode=ro&immutable=1` branch with unchanged strong pre/post main fingerprint and no sidecar appearance. |
| Main + WAL without SHM | Fail before SQLite opens. |
| Main + SHM without WAL | Fail before SQLite opens. |

Never set/change journal mode, request a checkpoint, use `immutable=1` when a sidecar exists, repair sidecars, or create a copied source merely to make validation pass.

Before SQLite opens and again after every source/backup connection closes, record an observation manifest for the main database and both sidecar paths:

- existence;
- regular-file type;
- filesystem identity;
- hard-link count;
- byte size;
- nanosecond mtime;
- SHA-256 when the file remains present and its identity, size, and mtime are stable across the hash; otherwise an explicit `unstable`/`not-present` observation.

The preservation guarantee before quarantine is logical, not byte-for-byte filesystem identity:

- reset performs no application SQL write, schema change, journal-mode change, checkpoint, migration, repair, or cleanup;
- exact schema 1.2/1.3 logical contents must be unchanged;
- the main database and WAL are expected to remain byte-stable for supported branches and any difference fails closed;
- an existing SHM may receive only SQLite-managed lock/read-mark changes from ordinary read-only WAL access;
- SQLite may interact with supported existing sidecars, so the SOW does not claim byte-for-byte sidecar preservation or unchanged sidecar metadata;
- every before/after observation and permitted SHM difference is recorded in the exact Section 13.7 audit artifact without exposing database contents.

If execution fails before quarantine, the active database remains at its original paths with logically unchanged contents. Tests compare independent canonical logical snapshots, not only file bytes.

### 13.5 Durable reset-state journal and crash recovery

The exact durable journal path is:

```text
data/.helios-room-reset-state.json
```

The only journal update staging path is:

```text
data/.helios-room-reset-state.json.next
```

Execution creates the first journal only after the source and completed backup have passed all Section 13.6 validation and immediately before the first active-file quarantine rename. Both paths are owner-readable/writable regular files where supported, have exactly one hard link, and are opened and mutated only through the Section 13.3 verified `data` directory handle with no-follow/open-reparse-point semantics. Neither path may preexist at new execution start. Presence of either path makes plan, new execution, server startup, `init-db`, preflight, and every normal database connection fail before SQLite opens with `database_reset_recovery_required`. Normal runtime deliberately does not parse or project the journal. The recovery command alone opens it; an unsafe type/identity returns `reset_recovery_invalid` and leaves every path unchanged.

The journal is UTF-8 canonical compact JSON with `ensure_ascii=False`, lexicographically sorted keys, one trailing newline, and this closed top-level shape:

```json
{
  "audit_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.audit.json",
  "backup": {
    "identity": "<platform-canonical-identity>",
    "path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.db",
    "sha256": "<64 lowercase hex characters>"
  },
  "database_path": "data/helios.db",
  "implementation_commit": "<full 40- or 64-character lowercase Git object ID>",
  "journal_sequence": 1,
  "journal_version": 1,
  "logical_digest": "<64 lowercase hex characters>",
  "plan_manifest_sha256": "<64 lowercase hex characters>",
  "plan_token": "<64 lowercase hex characters>",
  "quarantine": {
    "database": {"identity":"<platform-canonical-identity>","path":"data/.helios.db.reset-<plan-token>.original"},
    "shm": null,
    "wal": null
  },
  "recovery_commit": "<full 40- or 64-character lowercase Git object ID>",
  "reset_protocol_version": "room_shared_reset_v1",
  "source_files": {
    "database": {"identity":"<platform-canonical-identity>","link_count":1,"mtime_ns":0,"path":"data/helios.db","sha256":"<64 lowercase hex characters>","size":0},
    "shm": null,
    "wal": null
  },
  "source_observations": {
    "after_close": {"database":"<file-observation>","shm":"<file-observation>","wal":"<file-observation>"},
    "before_open": {"database":"<file-observation>","shm":"<file-observation>","wal":"<file-observation>"}
  },
  "source_schema_label": "1.3",
  "stage": "ready_to_quarantine",
  "updated_at": "2026-08-14T04:15:30.123Z"
}
```

`source_schema_label` is exactly `1.2` or `1.3`. `recovery_commit` is the matching concrete value from the reviewed plan mapping. Each non-null `source_files` or `quarantine` object has exactly the keys shown. A `file-observation` has exactly `exists`, `file_type`, `identity`, `link_count`, `mtime_ns`, `sha256`, `sha256_status`, and `size`; absent values are JSON null, `file_type` is `regular` or `absent`, and `sha256_status` is `stable`, `unstable`, or `not-present`. Timestamps use the Section 6.2 UTC millisecond format. Integers are nonnegative, except a platform API value that cannot be represented must fail rather than be serialized differently. No additional key is legal at any level.

The closed `stage` enumeration and write-ahead meanings are:

| Stage | Durable meaning |
| --- | --- |
| `ready_to_quarantine` | Backup is final and validated; no active-file rename has begun. |
| `quarantining` | Persisted immediately before the first original-file rename; some or all originals may already be quarantined. |
| `original_quarantined` | Every planned original component is in its verified quarantine path. |
| `installing_fresh` | Persisted immediately before creating any fresh active database file. |
| `fresh_installed` | Fresh files exist but have not passed mature validation. |
| `validating_fresh` | Persisted immediately before mature fresh validation. |
| `fresh_validated` | Fresh schema 1.4 and the exact foundation graph passed all required validation. |
| `cleaning_quarantine` | Persisted immediately before deleting any original quarantine component. |
| `finalizing` | Quarantine cleanup is complete; the terminal audit must be flushed before journal removal. |

Every stage change increments `journal_sequence` by exactly one. To create or replace the journal, write the complete next value to the exact `.next` path using exclusive-create/no-follow semantics, flush and `fsync`/`FlushFileBuffers` the file, atomically install it as the journal through verified handles, and flush the `data` directory or Windows equivalent before the protected transition begins. A crash may leave both files. Recovery accepts `.next` only when it is complete, valid, bound to the same plan, and exactly one sequence above the valid journal; otherwise the valid journal remains authoritative and the incomplete `.next` may be removed only by the separately authorized recovery command. If neither file provides one valid authoritative state, recovery fails `reset_recovery_invalid` without touching database, backup, audit, or quarantine files.

Normal startup never automatically resumes, rolls back, deletes a control file, or opens SQLite while journal state exists. Peter must separately authorize one exact recovery operation after reviewing the journal-derived recovery plan:

```text
reset-database --recover \
  --database data/helios.db \
  --expected-plan-token <64-lowercase-hex> \
  --action <restore-source|complete-fresh> \
  --confirm-reset-recovery
```

Recovery acquires the exclusive maintenance lease and requires the journal's exact protocol, implementation commit, plan token, backup identity/hash, source schema/digest, source/quarantine identities, and output paths. It refuses to run from another implementation commit or dirty worktree. `restore-source` is legal at every stage only while the planned terminal audit path is absent: it identity-quarantines any partial fresh active files, restores every intact original quarantine component when the full planned set remains, otherwise installs the independently revalidated backup as a clean source with no sidecars, validates the exact source schema and logical digest, writes the terminal audit outcome `restored_source`, and only then removes the journal. `complete-fresh` is legal only from `fresh_validated`, `cleaning_quarantine`, or `finalizing`: it revalidates the fresh schema 1.4 foundation and backup, completes identity-verified quarantine cleanup, writes the terminal audit outcome `reset`, and only then removes the journal. If a valid terminal audit already exists while the journal remains, recovery must validate and honor its recorded outcome; `reset` permits only `complete-fresh` finalization and `restored_source` permits only `restore-source` finalization. An audit mismatch, overwrite attempt, or other action/stage combination fails `reset_recovery_invalid` without mutation.

Recovery itself uses the same journal update protocol before each mutation, is idempotent under repeated termination, and retains the exclusive lease until it either reaches a validated terminal state or fails closed with the journal still present. The terminal audit file is written to a unique partial path, flushed, atomically renamed without overwrite to the planned audit path, and its parent directory flushed before journal removal. Journal removal and parent flush are the commit point that allows normal schema startup again. A crash before that point leaves startup closed and recovery repeatable; a crash after it leaves either the validated restored source or validated fresh database plus its durable audit artifact.

### 13.6 Exact accepted sources and reset sequence

The guarded reset accepts either:

- exact schema 1.2 history plus the complete `validate_v12_source` contract; or
- exact schema 1.3 history plus the complete `validate_v13_foundation` contract.

Every other source fails closed. The source label determines the validator used for both the source and completed backup.

After receiving the later exact execution authorization, with the server and external tools stopped, execution must:

1. Acquire the exclusive maintenance lease and retain it without interruption.
2. Recompute and validate the complete plan token/path/identity manifest.
3. Capture the pre-open observation manifest.
4. Open the source through the Section 13.4 selector and begin one read snapshot.
5. Validate exact schema 1.2 or 1.3, integrity, foreign keys, and an independent canonical logical digest. Exact schema 1.4, incompatible content, and corruption return `reset_source_invalid` here with zero backup, journal, audit, quarantine, or fresh-database creation.
6. Validate the planned `backups` directory identity or create exactly that directory under the rule in Section 13.3; remove a newly created empty directory on pre-backup failure after revalidating its identity.
7. Create a unique partial backup in the verified `backups` directory with exclusive-create/no-follow semantics.
8. Populate it using SQLite's backup API from the validated source snapshot.
9. Open the completed partial backup independently with the applicable legacy validator and require identical logical digest, `integrity_check='ok'`, zero foreign-key violations, and a stable SHA-256.
10. Atomically rename the verified partial backup to the exact reviewed backup path; abort rather than overwrite any existing path.
11. Close every SQLite connection while retaining the exclusive maintenance lease.
12. Capture and validate the post-open source/sidecar observation manifest and logical-preservation contract.
13. Create and durably flush the complete `ready_to_quarantine` journal from Section 13.5.
14. Persist `quarantining`, then atomically quarantine the exact active database and existing sidecars through Section 13.3; persist `original_quarantined` after every identity is verified.
15. Persist `installing_fresh`, install fresh schema 1.4 at the exact active path, and persist `fresh_installed`.
16. Persist `validating_fresh`, validate mature schema 1.4, integrity, foreign keys, the exact Section 6.7 foundation graph, zero old canonical/provider/memory rows, and shared policy at sequence 1, then persist `fresh_validated`.
17. Persist `cleaning_quarantine`, delete only the identity-verified quarantine files through verified-parent/handle-relative operations, and persist `finalizing`.
18. Write, flush, and atomically install the exact Section 13.7 audit artifact with outcome `reset`.
19. Remove the journal/control staging path through verified handles, flush the `data` directory, and release the exclusive maintenance lease. This is the durable reset commit point.
20. Emit and flush the exact success report derived from the retained audit artifact. An output failure after the commit point is reported but never rolls the database back.

A caught failure from step 14 until the terminal audit is installed invokes the `restore-source` recovery algorithm while the exclusive lease remains held. It does not depend on in-memory-only state. Quarantine the incomplete/fresh 1.4 files, restore the complete original quarantine set when available or the independently verified backup otherwise, validate the applicable 1.2/1.3 schema and exact canonical logical digest, write and flush the terminal audit outcome `restored_source`, and remove the journal only after a validated active source is present. Once a terminal `reset` audit is installed, rollback is no longer legal; a caught failure or crash must use `complete-fresh` finalization to revalidate the fresh database and remove the journal. Rollback guarantees the original logical database contents and compatible schema, not the original WAL/SHM bytes, file identity, or sidecar metadata.

An abrupt process or machine failure may temporarily leave no usable active path or a partial/fresh path. The durable journal makes every normal startup fail closed, and Section 13.5 recovery must reach a validated source or validated fresh terminal state before removing the interlock. The contract is therefore crash-safe recovery, not an impossible guarantee that every instant on disk contains an immediately openable active database.

Automatic in-process rollback covers reset execution until terminal audit installation. Terminal-audit installation commits the chosen `reset` or `restored_source` outcome; journal removal makes that committed outcome available to normal startup. Later success-report output and bootstrap remain outside rollback.

### 13.7 Exact reset report, audit artifact, and backup retention

Success writes one compact JSON object to stdout and nothing to stderr:

```json
{
  "audit_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.audit.json",
  "backup_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.db",
  "backup_sha256": "<64 lowercase hex characters>",
  "foreign_key_violations": 0,
  "implementation_commit": "<full 40- or 64-character lowercase Git object ID>",
  "integrity_check": "ok",
  "old_schema_label": "1.3",
  "plan_token": "<authorized 64 lowercase hex characters>",
  "recovery_commit": "<full 40- or 64-character lowercase Git object ID>",
  "reset_protocol_version": "room_shared_reset_v1",
  "room_shared_effective_from_room_sequence_no": 1,
  "schema_label": "1.4",
  "status": "reset"
}
```

`old_schema_label` is exactly `1.2` or `1.3`; `recovery_commit` is the reviewed matching mapping value. The key set is closed and lexicographically sorted. Paths are repository-relative with `/` separators. No absolute path, credential, environment value, database content, or provider data appears.

The retained audit artifact exists at the exact reviewed `audit_path`. It is UTF-8 canonical compact JSON with `ensure_ascii=False`, lexicographically sorted keys, one trailing newline, owner-readable/writable permissions where supported, no extra keys, and this closed shape:

```json
{
  "audit_version": 1,
  "backup_identity": "<platform-canonical-identity>",
  "backup_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.db",
  "backup_sha256": "<64 lowercase hex characters>",
  "completed_at": "2026-08-14T04:15:30.123Z",
  "database_path": "data/helios.db",
  "implementation_commit": "<full 40- or 64-character lowercase Git object ID>",
  "logical_digest": "<64 lowercase hex characters>",
  "outcome": "reset",
  "plan_manifest_sha256": "<64 lowercase hex characters>",
  "plan_token": "<64 lowercase hex characters>",
  "quarantine_identities": {"database":"<platform-canonical-identity>","shm":null,"wal":null},
  "recovery_commit": "<full 40- or 64-character lowercase Git object ID>",
  "reset_protocol_version": "room_shared_reset_v1",
  "source_observations": {
    "after_close": {"database":"<file-observation>","shm":"<file-observation>","wal":"<file-observation>"},
    "before_open": {"database":"<file-observation>","shm":"<file-observation>","wal":"<file-observation>"}
  },
  "source_schema_label": "1.3",
  "terminal_schema_label": "1.4"
}
```

`outcome` is exactly `reset` or `restored_source`. For `reset`, `terminal_schema_label` is `1.4`; for `restored_source`, it equals `source_schema_label`. `source_schema_label` is `1.2` or `1.3`. `quarantine_identities` has exactly `database`, `shm`, and `wal`, with absent components null. Each `file-observation` has the exact Section 13.5 schema. The artifact contains only operational provenance and cryptographic digests, never canonical message text, SQL rows, aliases, provider payloads, memory content, credentials, environment values, absolute paths, tracebacks, or exception text.

Create the audit artifact only as the terminal action described in Sections 13.5 and 13.6, with exclusive-create/no-follow staging, file flush, atomic no-overwrite rename, and verified-parent flush. It is retained beside the backup indefinitely unless Peter separately authorizes deletion. It is not served by the application, browser, Trace, API, or ordinary CLI. The only public projection is the closed success/error JSON in this section. Recovery `complete-fresh` prints the same success shape with `status` equal to `reset`. Recovery `restore-source` prints exactly this lexicographically sorted compact object plus one newline:

```json
{
  "audit_path": "backups/helios-pre-room-shared-reset-20260814T041530123Z.audit.json",
  "database_path": "data/helios.db",
  "implementation_commit": "<full 40- or 64-character lowercase Git object ID>",
  "old_schema_label": "1.3",
  "plan_token": "<64 lowercase hex characters>",
  "recovery_commit": "<full 40- or 64-character lowercase Git object ID>",
  "reset_protocol_version": "room_shared_reset_v1",
  "schema_label": "1.3",
  "status": "restored_source"
}
```

`old_schema_label` and `schema_label` are the same exact `1.2` or `1.3` value. No extra key is legal.

The verified backup is retained indefinitely unless Peter separately authorizes its deletion. Schema 1.4 runtime code must never open it as active data. Recovery uses the schema-compatible application commit recorded in the reset report/audit.

### 13.8 Post-reset bootstrap

Bootstrap is a separate provider-free operation after reset success. It is not part of the reset transaction or automatic reset rollback boundary.

Bootstrap may run in this order:

1. Verify `init-db` reports the exact existing Section 6.7 schema 1.4 foundation without mutation.
2. Import the approved seeded-memory source using the existing audited import command.
3. Audit imported seeded memory and owner correlations.
4. Install only Gemini provider configuration metadata through the existing provider-free installation command. The Gemini participant, alias, primary alias, name event, and active membership already exist and must not be duplicated.
5. Publish the Gemini welcome only if explicitly included in the existing installation workflow; it becomes the first or next shared chat message through guarded `store_message`.
6. Start the server without sending a provider message.
7. Verify new browser history, exact identities, participant selector, memory audit, and Trace behavior.

If any bootstrap step fails, stop bootstrap, report the completed and failed step, and leave the valid fresh schema 1.4 database intact. Do not automatically restore the retired database. Retrying bootstrap, repairing the fresh database, or restoring the old backup requires a separate explicit decision from Peter.

Neither bootstrap nor reset may read provider credentials, construct SDK clients, access provider endpoints, or make a billable call.

## 14. Required automated tests

Expected values must be constructed independently of the production code under test.

### 14.1 Fresh schema and timeline

Test:

- missing/object-free install;
- exact schema 1.4 idempotent validation;
- old schema reset-required behavior;
- exact migration row 3 metadata;
- main and future rooms receive only shared at sequence 1;
- zero/additional/wrong policy events fail;
- update/delete/additional inserts abort;
- message insertion without the event aborts;
- policy timestamps use the exact default and strict UTC millisecond format;
- mature validation rejects null, non-text, offsets, missing `Z`, wrong fractional precision, impossible dates/times, changed default, and weakened checks;
- the exact Section 6.7 room, four participants, three memberships, four alias/primary/bootstrap-event graphs, one Helios initial configuration, one policy event, and zero canonical/provider/memory rows are present;
- removing the initial Helios configuration, delaying the Gemini identity, adding a `room-system` membership, or creating any extra foundation row fails validation;
- every schema 1.4 runtime consumer accepts fresh valid foundation;
- no module outside the guarded reset source/backup validation path calls `validate_v12_source` or `validate_v13_foundation`.

### 14.2 Message guard and public errors

Assert:

- application guard rejects before message insertion;
- defense trigger emits only `message_visibility_guard` internally;
- persistence translation uses `from None`;
- exact `MessageVisibilityGuardError` fields;
- exact Helios/Gemini provider-turn mapping;
- exact room HTTP mapping;
- exact CLI exit/stdout/stderr mapping;
- exact identity/publication mapping;
- repository-wide inventory of all `store_message` consumers;
- no raw SQLite text or internal token on any public surface.

### 14.3 Total provider-history matrix

For both selected providers, cover:

- Peter to selected provider, Room, other provider, and an additional participant;
- selected provider to Peter with both correlation fields null;
- selected provider to Peter with only `turn_id` non-null;
- selected provider to Peter with only `reply_to_id` non-null;
- selected provider to Peter with both fields non-null;
- selected provider to Room, other AI, and additional participant with no/incidental turn metadata;
- other AI to Peter, selected provider, Room, and another participant;
- additional human and AI to all destination kinds;
- valid system/control record;
- unknown participant type, invalid route, and invalid alias ownership.

Assert exact native items, external items, omissions, and failures. Prove:

- classification uses only sender, destination, `turn_id`, and `reply_to_id`;
- selected-provider to Peter with both fields null is external;
- either non-null field makes it a native candidate;
- a candidate with missing or invalid counterpart fails and never downgrades;
- selected-provider messages to any non-Peter destination are external regardless of incidental metadata;
- provider events pointing at a noncandidate do not reclassify history;
- selected-provider authorship alone never fails;
- valid self-routes do not poison later shared history;
- every chat message appears exactly once in canonical order;
- immutable route aliases survive later participant renames;
- compact sorted JSON preserves Unicode through `ensure_ascii=False`;
- OpenAI and Google item key sets are exact.

### 14.4 Helios native classifier and validation

Independently cover exact two-message cardinality, completed status, room/turn/sequence, sender/destination/route-alias, reply, Helios-owned OpenAI configuration, and canonical text.

Prove every malformed canonical correlation fails. Patch the API-event query boundary to fail on access and prove Helios classification/native replay never queries it. Independently prove OpenAI request validation and Trace still reject malformed OpenAI events when those paths inspect them.

### 14.5 Gemini native replay

Cover exact canonical native correlations plus:

- one request event at sequence 1;
- one successful response event at sequence 2;
- exact request contents prefix;
- exact response/model/configuration/turn/message correlations;
- thought and canonical thought-signature validation;
- missing, extra, redacted, reordered, or mismatched evidence.

Prove Gemini noncandidates do not query Google events and invalid candidates never downgrade.

### 14.6 Uncontaminated provider request test

Create five shared messages:

1. Peter to Gemini.
2. Gemini to Peter through a valid provider turn.
3. Peter to Helios.
4. Helios to Peter through a valid provider turn.
5. Peter to Room.

First call `load_provider_history` directly for both providers at the exact five-message boundary. Assert all five appear once in order with exact native/external roles.

Clone the temporary database using SQLite backup API:

- clone H receives only a sixth Peter-to-Helios trigger;
- clone G receives only a sixth Peter-to-Gemini trigger.

Assert each recorded request contains the original five messages plus its own trigger, without contamination from the other clone.

### 14.7 Request and Trace v3

For both providers assert:

- exact request key sets;
- required three-key visibility object with effective sequence 1;
- exact bounded provider input;
- exact trigger/boundary/configuration/memory correlations;
- Trace v3 closed top-level shape;
- two equal visibility copies for unredacted requests and no top-level copy;
- null request/local context with no synthesized visibility for redacted requests;
- open, completed, failed, cancelled, and redacted states;
- provider events pointing at noncandidates fail or pass independently without reclassifying history;
- browser rendering and no private label.

### 14.8 Corruption and precedence

Cover missing/wrong/additional policy event, malformed timestamp, request-policy mismatch, missing/extra visibility evidence, external-envelope mismatch, shared-history omission/duplication/reordering, invalid native candidates, memory-audit corruption, and missing provider configuration.

Assert exact precedence:

1. schema history/objects;
2. shared policy foundation;
3. route and canonical history;
4. native candidate validation;
5. memory retrieval/audit;
6. environment/provider configuration.

For every pre-environment failure, assert zero environment, dotenv, credential, client, provider, and database-write activity.

### 14.9 Reset command safety

Using temporary repository/database fixtures only, test:

- plan, execution, and recovery are distinct command modes with separate confirmations;
- plan opens no SQLite connection, calls no schema/integrity/logical validator, and changes no database/sidecar content;
- filesystem-eligible schema 1.2, 1.3, 1.4, incompatible, and corrupt files can all produce plans without semantic classification;
- execution alone rejects 1.4/incompatible/corrupt content after plan recomputation and before backup, journal, audit, quarantine, or fresh-database creation;
- missing/stale plan token or mismatched reviewed backup/audit path fails before mutation;
- plan token changes for any filesystem observation, protocol version, implementation commit, clean-worktree state, or recovery-commit mapping change;
- execution refuses a plan from another implementation commit, dirty worktree, protocol version, or recovery mapping;
- the root `.gitignore` contains the exact Section 13.2.1 block and is committed before plan generation;
- `git check-ignore --no-index` succeeds for representative active database/sidecar, lock, journal, journal-next, backup, backup-partial, audit, audit-partial, original-quarantine, failed-new, and recovery-staging paths;
- removing or weakening `/backups/`, either exact control-file rule, the lock rule, active database/sidecar coverage, or `/data/.helios*.reset-*` makes plan/execution/recovery fail `reset_git_safety_invalid` before SQLite or artifact creation;
- no reset-owned representative path is tracked in HEAD or the index, and runtime never modifies `.gitignore` or the index;
- capture exact porcelain status before each operation and prove byte-identical status after plan, every pre-mutation failure, successful execution with retained backup/audit, and both completed recovery outcomes;
- after subprocess termination at every journal stage, retained backup/audit/partial/quarantine/control files remain ignored and the tracked/nonignored porcelain status is byte-identical to baseline before the next recovery invocation;
- lock creation in an initially lock-free fixture is ignored, the Git checks repeat under the acquired lease, and the worktree remains clean;
- missing confirmation flag;
- wrong or unresolved database path;
- symlink, junction, reparse-point, hard-link, path-traversal, special-file, and parent-identity substitution attempts;
- path or filesystem identity replacement between plan and execution and immediately before each quarantine/delete action;
- missing, exact valid 1.2, exact valid 1.3, 1.4, corrupt, and incompatible databases;
- both exact 1.2 and 1.3 succeed with the applicable validator and exact `old_schema_label`;
- active server, reader, writer, CLI connection, and externally held maintenance lock;
- every connection factory and server startup honors shared maintenance locking;
- reset holds one exclusive lease continuously across connection close, quarantine, installation, validation, cleanup, and rollback;
- existing backup-name collision;
- backup API failure;
- source/backup validation failure;
- integrity or foreign-key failure;
- all four WAL/SHM source states and the exact permitted open branches;
- before/after identity, link count, size, nanosecond mtime, and stable-digest observation manifests;
- logical source snapshots remain identical for every pre-quarantine failure, with only the documented SHM allowance;
- main/WAL byte change fails closed and no unsupported sidecar state is repaired;
- failure before and after each reset step;
- exact journal/control paths, permissions, no-follow/link/identity rules, closed JSON schema, canonical serialization, sequence increments, timestamps, and closed stage enumeration;
- normal server startup, `init-db`, preflight, plan, execution, and every ordinary connection fail `database_reset_recovery_required` without SQLite access while either journal path exists;
- subprocess termination or simulated power loss immediately before and after every destructive rename, fresh-file creation, validation transition, quarantine deletion, audit rename, report flush, journal rename, and journal removal;
- crash with only a valid journal, valid journal plus complete next state, valid journal plus partial next state, invalid journal, substituted journal identity, and absent journal;
- `restore-source` is repeatable from every stage before terminal-audit installation, validates the original logical digest/schema, writes `restored_source` audit, and removes the interlock only after validation;
- `complete-fresh` is rejected before `fresh_validated`, succeeds idempotently from `fresh_validated`, `cleaning_quarantine`, and `finalizing`, and removes the interlock only after fresh revalidation and terminal audit flush;
- a crash after terminal `reset` audit installation permits only `complete-fresh`, while a crash after terminal `restored_source` audit installation permits only `restore-source` finalization;
- success-report output failure after journal removal never rolls back the committed fresh database and remains reconstructable from the retained audit;
- termination during either recovery action remains recoverable on the next explicitly authorized invocation;
- exact target allowlist and no recursive/glob deletion;
- identity-verified atomic quarantine before fresh installation;
- verified rollback after every quarantine/install/validation/cleanup failure while the exclusive lease remains held;
- exact success/recovery reports, exact retained audit artifact, selected recovery commit, and retained verified backup;
- audit creation/retention/privacy projection and absence of canonical/provider/memory/credential content;
- fresh active database has zero old rows and exactly shared at sequence 1;
- zero environment, credential, SDK, network, or provider access.

### 14.10 Bootstrap and UI

Test seeded-memory reimport, audit, Gemini configuration installation without identity duplication, welcome publication, identity/participant selector, empty/new room history, directed labels, and Trace v3 using provider fakes only. Inject a failure after every bootstrap step and prove the valid fresh schema 1.4 database remains active, no automatic retired-database restore occurs, and later bootstrap steps do not run.

## 15. Verification gates

Before any live reset plan or execution:

1. Focused schema, policy-trigger, and guard tests.
2. Complete database, preflight, identity, and seeded-memory suites.
3. Complete Helios and Gemini provider-free suites.
4. Complete request-validator and Trace suites.
5. Complete reset safety and rollback-injection suite.
6. Complete Python suite.
7. JavaScript tests when Node is already available.
8. `pip check`.
9. Non-writing Python compilation validation.
10. `git diff --check` including untracked candidate files.
11. Exact root `.gitignore` block validation plus `git check-ignore --no-index` coverage for every reset-owned path family.
12. Byte-identical starting/ending tracked/nonignored porcelain status comparison for plan, execution, crash, and recovery fixtures.
13. Read-only audit of the implementation diff against this SOW.
14. Separately authorize, commit, and push the approved implementation, including the root `.gitignore` change.
15. Separate authorization for the live plan, followed by review and separate authorization for the exact execution plan as defined in Section 13.1.

All automated tests use temporary databases, fake providers, synthetic credentials, and synthetic environment values.

## 16. Acceptance criteria

Implementation is accepted only when:

- schema 1.4 installs fresh and validates cleanly;
- the fresh foundation contains exactly the Section 6.7 rows, including the preserved initial Helios configuration and initialized Gemini identity/membership;
- an old schema 1.2 or 1.3 database cannot be opened, initialized, or migrated by normal schema 1.4 runtime code; only the later separately authorized reset execution may open it through the exact read-only source path;
- implementation approval does not authorize a live plan or reset execution;
- plan and execution require the two later explicit instructions and exact reviewed plan binding in Section 13.1;
- planning is filesystem-only and makes no source-schema, integrity, or logical-eligibility claim;
- execution validates the exact 1.2/1.3 source before backup or any destructive/control artifact is created;
- the reviewed plan token binds the clean exact implementation commit, reset protocol, backup/audit paths, and closed recovery-commit mapping, and changed code cannot execute it;
- the committed root `.gitignore` contains the exact Section 13.2.1 block, every reset-owned runtime artifact is ignored and untracked, and plan/execution/crash/recovery never dirty the tracked/nonignored worktree;
- every application database connection honors the shared maintenance lease and reset holds one exclusive lease continuously;
- path-chain, reparse/symlink/hard-link, filesystem-identity, TOCTOU, and handle-relative quarantine protections are enforced;
- reset accepts exact valid schema 1.2 and 1.3 and preserves a verified recoverable matching-schema backup before quarantine;
- every pre-quarantine failure preserves logical source contents and records the exact WAL-safe filesystem observations without claiming sidecar byte identity;
- every destructive transition is preceded by the exact durable journal update, normal startup fails closed while journal state exists, and explicitly authorized recovery is repeatable after termination at every stage;
- `complete-fresh` is permitted only after durable mature fresh validation; otherwise recovery restores and validates the original logical source;
- the exact retained audit artifact and public report preserve non-content provenance, including the implementation and selected recovery commits;
- the active database contains no copied old row after reset;
- every room has exactly `room_shared_v1` at sequence 1;
- every new valid chat message appears exactly once in both provider histories;
- addressing remains visible and destination-specific triggering remains unchanged;
- the exact native-candidate classifier is implemented before validation;
- selected-provider noncandidates use external envelopes and do not poison later history;
- malformed candidates fail without downgrade;
- Helios native replay uses the new shared canonical contract and never queries OpenAI events;
- Gemini native replay retains exact Google event validation;
- all shared request events contain exact visibility provenance;
- Trace v3 is exact for every supported state;
- all message-guard errors use exact sanitized mappings;
- seeded memory remains participant-scoped;
- terminal-audit installation commits the reset/recovery outcome, journal removal re-enables normal startup, and later bootstrap failure leaves the valid fresh database intact;
- reset/bootstrap make no provider call;
- no live billable acceptance occurs without separate authorization.

## 17. Controlled execution and optional live acceptance

Implementation approval alone stops before live data access. After audit, commit, and push, Peter must separately authorize the live plan. Return its exact reviewed backup/audit paths, filesystem identity, implementation commit, recovery-commit mapping, protocol version, and plan token. State explicitly that the plan did not validate SQLite content. Only a later explicit instruction naming that exact plan authorizes one reset execution.

After reset success, report the backup path/hash, audit path, implementation/recovery commits, and reset report before bootstrap or any provider call. Then perform only separately directed provider-free bootstrap and server verification from Section 13.8.

Optional billable acceptance requires separate explicit authorization:

1. Send one Peter-to-Gemini message and receive Gemini's response.
2. Send one Peter-to-Helios message asking about the new Gemini exchange.
3. Confirm Helios receives both new Gemini-directed messages.
4. Confirm only the fresh database history appears.
5. Inspect both Trace v3 records.
6. Stop and report before another provider call.

## 18. Recovery

Do not downgrade the fresh schema 1.4 database in place.

If reset execution raises a caught failure after quarantine but before journal removal, the reset command invokes the journal-backed `restore-source` algorithm while holding the exclusive maintenance lease. If the process or machine terminates, normal startup fails `database_reset_recovery_required`; no automatic startup repair occurs. Peter must review the durable state and separately authorize `restore-source` or, only after durable `fresh_validated`, `complete-fresh` as defined in Section 13.5.

If later bootstrap fails, leave the valid fresh schema 1.4 database intact and report the failure. Do not automatically restore the retired database. If Peter later explicitly chooses manual recovery:

1. stop the server;
2. preserve the failed schema 1.4 database for forensic inspection;
3. restore the verified pre-reset backup as a complete SQLite database;
4. restore the exact schema 1.2- or 1.3-compatible application commit matching the backup;
5. validate integrity, foreign keys, and schema before restart.

Never open the retired backup with schema 1.4 runtime code and never merge old rows into the fresh database.

## 19. Deliverables

- Fresh schema 1.4 SQL.
- Guarded reset command and verified-backup workflow.
- Committed root `.gitignore` reset-artifact block and Git-safety validation.
- Durable reset journal, startup interlock, exact recovery commands, and termination-injection coverage.
- Closed retained reset audit artifact plus implementation/recovery commit binding.
- Exact shared policy resolver and message guard.
- Room-wide OpenAI and Google projections.
- Exact selected-provider native-candidate classifier.
- New shared-policy Helios native contract.
- Exact Gemini native replay.
- Shared request validators and Trace v3.
- UI/documentation updates.
- Complete automated and rollback-injection tests.
- Reset report and provider-free bootstrap report.
- Optional live-acceptance report after separate authorization.

## 20. Implementation restrictions

During code implementation and automated testing:

- use temporary databases only;
- do not access `data/helios.db`, its sidecars, real `.env`, real credentials, provider endpoints, remotes, or unrelated untracked documentation;
- do not stage, commit, push, start the live server, import live memory, or make live provider calls;
- runtime reset commands must not modify `.gitignore`, the index, refs, commits, stashes, or unrelated untracked files;
- do not install/change dependencies without separate authorization;
- do not use production validators to generate their own expected fixtures.

Passing gates or completing this SOW does not authorize live database access. Live planning and execution require the separate explicit instructions in Section 13.1. Those later instructions still do not authorize credential access, provider calls, backup deletion, or billable acceptance.

## 21. Completion report

The implementation report must include items 1 through 5 and 11 through 13 below. A later separately authorized reset report must add items 6 through 10:

1. exact files changed;
2. exact fresh schema behavior;
3. exact classifier and provider matrices;
4. focused/complete test commands, counts, and results;
5. reset safety, exact ignore coverage, clean-worktree invariance, journal/recovery, subprocess-termination, and rollback-injection results;
6. authorized filesystem-only plan JSON, exact reviewed backup/audit paths, implementation commit, recovery-commit mapping, protocol version, and plan token, with an explicit statement that planning did not validate SQLite content;
7. pre-reset backup relative path and SHA-256;
8. exact reset JSON report and retained audit-artifact path/hash;
9. proof the active database contains no retired rows and has the exact Section 6.7 foundation;
10. proof shared policy is effective from sequence 1 and any separately directed bootstrap results;
11. byte-identical starting/ending tracked/nonignored porcelain status and confirmation that retained/partial reset artifacts are ignored and untracked;
12. confirmation of zero provider calls and zero credential access;
13. confirmation that no unrelated file, dependency, or unauthorized live target was touched, with commit/push actions reported exactly when separately authorized.

## 22. Final invariant

> The fresh room remembers every new canonical chat message. Addressing says who was spoken to. It never says who is allowed to know the message was spoken.
