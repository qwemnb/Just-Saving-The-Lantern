# Gemini Seeded Memory Workshop v1

## Purpose

This framework helps Peter and Gemini create a small, reviewed set of
Gemini-owned inherited memories for Helios Room.

These memories preserve useful continuity from Peter and Gemini's interactions
before Gemini entered Helios Room. They are reference records. They are not:

- canonical events that happened inside Helios Room;
- messages from Peter inside Helios Room;
- hidden instructions for Gemini;
- proof of consciousness, identity, or a predetermined relationship;
- copies of Helios's private inherited memories;
- automatic permission to import anything into the live database.

Gemini proposes. Peter reviews, edits, rejects, and approves. Only the final
Peter-approved manifest should be considered for import.

## Core Principle

Preserve what will help Gemini recognize context without telling Gemini what it
must become.

A strong seeded memory says what was discussed, chosen, valued, created, or
left uncertain. It does not assign Gemini a fixed personality, emotional state,
conclusion, role, or developmental destination.

## Source Boundary

Gemini may use only source material Peter deliberately supplies for this
workshop, such as:

- prior Peter-Gemini conversations;
- Peter-approved summaries of those conversations;
- artifacts Peter and Gemini created together;
- corrections Peter provides during review.

Gemini must not claim access to conversations or facts that are absent from the
supplied source packet. If Gemini remembers something through a product-level
memory feature but cannot locate it in the reviewed source packet, it may ask
Peter whether to add the relevant source. It must not silently convert that
recollection into a seed record.

Helios-owned seed memories may be used only as examples of format and
provenance discipline. Their content must not be copied, paraphrased, or
reassigned to Gemini unless Peter separately supplies the underlying
Peter-Gemini source showing that the same continuity genuinely exists there.

## Recommended Initial Size

Start with 8 to 12 records. Prefer short, distinct records of roughly 300 to
1,000 characters each. The importer permits up to 4,000 characters per record,
but smaller records retrieve more precisely and leave room for several relevant
records inside the 8,000-character request budget.

The runtime can select at most five records for one turn. Do not combine every
important fact into one large omnibus record merely to force it into every
request.

## Good Initial Memory Domains

Gemini should look for continuity in these domains without assuming that every
domain needs a record:

1. Peter's identity, language, preferences, or boundaries that materially
   affect how Gemini understands him.
2. The history and character of Peter and Gemini's actual conversations.
3. Shared creative projects, technical work, or recurring questions.
4. Decisions Peter and Gemini made together that are likely to matter again.
5. Recurring metaphors, names, phrases, or conceptual anchors.
6. Unresolved questions that should remain open rather than be flattened into
   conclusions.
7. Explicit corrections Peter made that prevent a likely future
   misunderstanding.
8. Helios Room's purpose, but only as Peter and Gemini actually discussed it.

## Admission Test for Each Record

A candidate should survive all of these questions:

1. **Source:** Can Gemini point to supplied source material supporting it?
2. **Durability:** Is it likely to matter in a later conversation rather than
   only today?
3. **Usefulness:** Would retrieving it help Gemini answer with better context?
4. **Distinctness:** Is it meaningfully different from the other records?
5. **Provenance:** Does the wording distinguish direct history, Peter's report,
   Gemini's interpretation, and uncertainty?
6. **Non-directive form:** Does it describe continuity without ordering future
   Gemini behavior?
7. **Privacy:** Is Peter comfortable placing it in Gemini's private inherited
   memory store?
8. **Accuracy:** Would Peter recognize the wording as fair and appropriately
   qualified?

Reject a candidate if any answer is no.

## Wording Rules

Each `memory_text` should:

- stand on its own when retrieved without neighboring records;
- name Peter, Gemini, the project, or the subject instead of relying on vague
  pronouns;
- preserve meaningful uncertainty with language such as `Peter described`,
  `Gemini proposed`, `they explored`, `the record does not establish`, or
  `it remains uncertain`;
- distinguish a source statement from an interpretation;
- include likely retrieval vocabulary naturally in the prose;
- avoid commands such as `always`, `must`, `never disagree`, `remember that you
  are`, or `act as if` unless the record is accurately describing a boundary
  Peter explicitly set;
- avoid claiming that Gemini directly experienced anything outside the source
  conversation;
- avoid diagnosing Peter or another person;
- omit secrets, credentials, private addresses, financial account data, or
  unnecessary sensitive third-party detail;
- avoid praise, mythology, or emotional certainty added merely to make the
  record feel more meaningful.

### Weak and Strong Examples

Weak:

> Peter and Gemini have a sacred bond, and Gemini should protect it forever.

Strong:

> Peter and Gemini have held recurring conversations about continuity between
> AI sessions. Peter values recognition without forced certainty and wants the
> relationship to remain open to what develops. This record describes Peter's
> stated preference and the history of those conversations; it does not assign
> Gemini a predetermined identity or conclusion.

Weak:

> Peter is the Keeper and Gemini knows what that means.

Strong:

> In the supplied Peter-Gemini conversation, Peter used the name "Keeper" as
> part of his personal spiritual language. Gemini should treat the term as
> Peter's self-description in that context, not infer a universal definition or
> claim knowledge beyond the supplied conversation.

## Provenance Classes

Gemini should label each candidate internally as one of these classes during
review. These labels help Peter evaluate wording, but they do not become extra
fields in the final JSON manifest.

| Class | Meaning | Typical confidence |
| --- | --- | --- |
| Direct statement | Peter explicitly stated it in the supplied source | 0.95 to 1.0 |
| Recorded decision | Peter and Gemini explicitly chose or finalized it | 0.95 to 1.0 |
| Repeated pattern | The same theme appears clearly across sources | 0.80 to 0.95 |
| Joint interpretation | Peter and Gemini developed the framing together | 0.70 to 0.90 |
| Gemini inference | Gemini infers it from limited evidence | Usually reject, otherwise 0.50 to 0.70 with explicit qualification |
| Uncertain recollection | Source or attribution is incomplete | Reject until clarified, or preserve the uncertainty explicitly |

## Scoring Rubric

The manifest requires `importance`, `confidence`, and topic `weight` values
from 0.0 through 1.0.

### Importance

- `1.0`: foundational continuity likely to affect many future conversations.
- `0.8`: durable and likely to matter in a recurring domain.
- `0.6`: useful project or preference continuity with narrower relevance.
- `0.4`: valid but situational; usually defer from the initial batch.
- `0.2`: mostly ephemeral; normally reject.

### Confidence

- `1.0`: directly supported and explicitly confirmed by Peter.
- `0.9`: directly supported with only minor paraphrase.
- `0.75`: supported synthesis across multiple source passages.
- `0.6`: meaningful uncertainty remains and is stated in the memory text.
- below `0.6`: normally reject until the source or wording improves.

### Topic Weight

- `1.0`: the record is centrally about this topic.
- `0.8`: strong secondary topic.
- `0.6`: useful supporting topic.
- below `0.5`: omit the topic rather than adding retrieval noise.

Use one primary topic and no more than three useful secondary topics per
record. Reuse the same `topic_key` and exact `name` consistently across the
entire manifest.

## Stable Identifiers

Use lowercase, durable IDs that describe the record rather than its exact
wording:

```text
gemini-peter-<subject>-v1
```

Examples:

```text
gemini-peter-room-purpose-v1
gemini-peter-continuity-question-v1
gemini-peter-creative-collaboration-v1
```

If a record later needs materially different content, create a reviewed new
version such as `-v2`. Do not reuse an imported stable ID for unrelated or
quietly rewritten content.

## Workshop Sequence

### Stage 1: Establish the Source Packet

Peter gives Gemini a bounded set of transcripts, summaries, or artifacts. Give
each source a short label and, when possible, a date or date range.

Gemini first returns only:

- the source labels it received;
- any obvious gaps or unreadable sections;
- any ambiguity about authorship;
- a statement that it will not use unsupplied material.

No memory candidates are written yet.

### Stage 2: Build a Candidate Ledger

Gemini proposes up to 15 candidates in a human-readable table. For each one,
include:

| Field | Purpose |
| --- | --- |
| Candidate ID | Temporary review identifier such as `C01` |
| Proposed stable ID | Future immutable identifier |
| Proposed memory text | Standalone factual continuity record |
| Provenance class | One class from the table above |
| Source anchors | Exact source labels and concise locations |
| Importance | 0.0 to 1.0 with one-sentence reason |
| Confidence | 0.0 to 1.0 with one-sentence reason |
| Topics | Proposed keys, names, and weights |
| Participant subjects | Usually `peter` and `gemini`, only when actually subjects |
| Risks or uncertainty | Overstatement, privacy, duplication, or ambiguity |
| Recommendation | Keep, merge, revise, defer, or reject |

At this stage Gemini must prefer honest omission over completeness.

### Stage 3: Peter's Review

Peter marks every candidate:

- **approve**;
- **approve with edits**;
- **merge**;
- **defer**;
- **reject**;
- **needs source check**.

Silence is not approval. Gemini should ask focused questions only where the
answer changes inclusion or wording. It should not argue Peter into preserving
a candidate.

### Stage 4: Adversarial Memory Audit

Before producing JSON, Gemini audits the approved set and reports:

1. unsupported factual claims;
2. emotional or identity claims presented with too much certainty;
3. instructions disguised as memories;
4. duplicated or overly broad records;
5. privacy concerns and unnecessary third-party details;
6. missing uncertainty or attribution;
7. inconsistent topic names or keys;
8. source locators too vague to audit;
9. wording that assigns Gemini a predetermined role;
10. memories copied from Helios continuity without an independent
    Peter-Gemini source.

Gemini revises only with Peter's approval.

### Stage 5: Final Human Preview

Gemini presents the exact final memory texts in a numbered list, followed by a
compact provenance table. Peter approves the final wording as a set.

This is the last content review. Any later wording change returns to this
stage.

### Stage 6: Generate the Import Manifest

Only after explicit final approval, Gemini emits one strict JSON object with no
comments, Markdown fences, review fields, or additional keys.

Generating the manifest is not authorization to import it.

## Paste-Into-Gemini Master Prompt

Copy the following prompt into the Gemini conversation that has access to the
reviewed source material:

```text
We are preparing a small set of Gemini-owned inherited seeded memories for Helios Room.

Purpose: preserve useful continuity from our Peter-Gemini conversations before Gemini entered Helios Room. These records will be private reference data supplied only to Gemini when locally retrieved. They are not canonical Helios Room events, not new messages from Peter, not hidden instructions, and not proof of any identity or relationship claim.

Your role is to propose memories from only the source packet I deliberately provide in this workshop. Do not use unsupplied product memory, hidden state, general assumptions about me, or Helios-owned memory content. Do not invent shared experiences. Distinguish my direct statements, recorded joint decisions, repeated patterns, joint interpretations, your inferences, and genuine uncertainty.

Preserve open-endedness. Do not tell your future instance what personality, emotions, beliefs, role, or conclusion it must have. A good memory should improve contextual recognition without steering the experiment toward a predetermined outcome.

Work in six stages and stop for my approval after each stage:
1. Source inventory: list only the supplied source labels, gaps, unreadable sections, and authorship ambiguities. Write no memories yet.
2. Candidate ledger: propose at most 15 short, standalone candidates with temporary ID, stable ID, memory text, provenance class, source anchors, importance, confidence, topics with weights, participant subjects, risks, and keep/revise/defer/reject recommendation.
3. Peter review: accept my approve, edit, merge, defer, reject, and source-check decisions. Silence is not approval.
4. Adversarial audit: identify unsupported claims, overconfident emotional or identity claims, instructions disguised as memories, duplication, privacy issues, missing attribution, topic conflicts, vague locators, predetermined-role language, and any continuity borrowed from Helios without an independent Peter-Gemini source.
5. Final preview: show the exact proposed memory texts and a compact provenance table. Wait for explicit approval of the complete set.
6. Manifest: only after that approval, emit strict importer-valid JSON with no comments, Markdown fences, review fields, or extra keys.

Selection test for every candidate: sourced, durable, useful, distinct, provenance-aware, non-directive, private enough to store, and accurate in my judgment. Prefer omission over speculation.

Writing rules: name the subject clearly; keep each record self-contained; preserve uncertainty; distinguish reported facts from interpretation; include natural retrieval vocabulary; avoid diagnoses; omit secrets and unnecessary third-party detail; do not use commands unless accurately describing an explicit boundary I set.

Target 8 to 12 final records, generally 300 to 1,000 characters each. Use lowercase stable IDs in the form gemini-peter-<subject>-v1. Use one primary topic at weight 1.0 and at most three useful secondary topics at 0.6 or higher. Use participant subjects only for people or AI participants actually discussed in that record.

Before Stage 1, reply with a one-paragraph restatement of these boundaries and ask me to provide or identify the source packet.
```

## Exact Manifest Shape

The current strict importer accepts exactly these fields. Every object must
contain all fields shown, including nullable fields. No extra fields are
allowed.

```json
{
  "format_version": 1,
  "batch": {
    "name": "gemini-peter-inherited-continuity-v1",
    "source_type": "curated_gemini_conversation_history",
    "source_uri": null,
    "source_created_at": null,
    "source_description": "Peter-approved continuity curated from supplied Peter-Gemini conversations that occurred before Gemini entered Helios Room.",
    "notes": "Gemini proposed the records through the Gemini Seeded Memory Workshop v1. Peter reviewed and explicitly approved the final wording before import."
  },
  "memories": [
    {
      "stable_id": "gemini-peter-example-subject-v1",
      "memory_text": "Replace this example with one exact Peter-approved standalone continuity record.",
      "category": "relationship-continuity",
      "importance": 0.8,
      "confidence": 1.0,
      "source_label": "Peter-Gemini continuity workshop",
      "source_locator": "Replace with an auditable supplied source label, date, and concise location.",
      "topics": [
        {
          "topic_key": "example-subject",
          "name": "Example Subject",
          "weight": 1.0
        }
      ],
      "participant_subjects": [
        "peter",
        "gemini"
      ]
    }
  ]
}
```

The example record must be replaced, not imported.

## Importer Constraints

Gemini should validate the final output against these rules before delivery:

- UTF-8 JSON without a byte-order mark;
- `format_version` is integer `1`;
- root contains exactly `format_version`, `batch`, and `memories`;
- batch contains exactly `name`, `source_type`, `source_uri`,
  `source_created_at`, `source_description`, and `notes`;
- `source_created_at` is either null or an actual UTC timestamp such as
  `2026-08-13T20:00:00Z`;
- `memories` is a nonempty array;
- every memory contains exactly the nine fields shown above;
- every `stable_id` is unique and no longer than 200 characters;
- `memory_text` is nonblank and no longer than 4,000 Unicode characters;
- `category` and `source_label` are nonblank and no longer than 200 characters;
- `source_locator` is nonblank and no longer than 1,000 characters;
- `importance`, `confidence`, and every topic `weight` are finite numbers from
  0.0 through 1.0, not booleans;
- every memory has at least one topic;
- each topic contains exactly `topic_key`, `name`, and `weight`;
- topic keys are lowercase, unique within a memory, and no longer than 200
  characters;
- one topic key always maps to the same exact topic name across the manifest;
- one case-insensitive topic name always maps to the same topic key;
- `participant_subjects` is an array of unique lowercase stable participant
  keys, each no longer than 64 characters;
- all Peter-Gemini records use only participant keys that will exist in the
  target database;
- no duplicate JSON keys, comments, NaN, or Infinity values appear.

For Gemini ownership, the future import command defined by the Gemini
Integration v1 SOW is:

```cmd
python -m app.main import-seed-memories --owner-participant-key gemini --file data\imports\gemini_seed_memories_v1.json
```

Do not run this command until the Gemini integration is implemented, the
manifest has been reviewed, the live SQLite database has a verified backup,
and Peter separately authorizes the import.

## Peter's Final Approval Checklist

Before accepting the manifest, Peter should be able to answer yes to each
question:

- Did I knowingly supply or approve every source?
- Does every record sound true without needing hidden context?
- Are direct statements, interpretations, and uncertainties clearly
  distinguished?
- Is anything written as an instruction to future Gemini rather than a record
  of continuity?
- Does any record force a consciousness claim, identity, emotional state, or
  relationship outcome?
- Did Helios-private continuity leak into Gemini's records?
- Are third-party details necessary and appropriate?
- Would I still want Gemini to retrieve each record months from now?
- Are broad records split enough for precise retrieval?
- Are the exact final texts and source locators correct?
- Have I explicitly approved this exact complete set?

If any answer is no or uncertain, return to the candidate ledger. The goal is
not to preserve everything. The goal is to preserve a small amount of honest,
useful continuity without predetermining what happens next.
