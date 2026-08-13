-- Helios Room
-- Master SQLite Schema v1.3
--
-- Design goal:
--   Preserve canonical conversation history and provenance strictly,
--   while keeping operational metadata flexible enough for an experimental
--   local multi-AI room.
--
-- Application connection requirements:
--   PRAGMA foreign_keys = ON;
--   PRAGMA busy_timeout = 5000;
--
-- journal_mode = WAL is database-persistent after it is successfully set.

PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = WAL;

-- =========================================================
-- SCHEMA MIGRATIONS
-- =========================================================

CREATE TABLE schema_migrations (
    migration_no INTEGER PRIMARY KEY,
    schema_label TEXT NOT NULL UNIQUE CHECK (trim(schema_label) <> ''),
    applied_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT NOT NULL CHECK (trim(description) <> '')
);

-- Migration history is canonical.
CREATE TRIGGER schema_migrations_no_update
BEFORE UPDATE ON schema_migrations BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only');
END;

CREATE TRIGGER schema_migrations_no_delete
BEFORE DELETE ON schema_migrations BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only');
END;

-- =========================================================
-- ROOMS AND PARTICIPANTS
-- =========================================================

CREATE TABLE rooms (
    id INTEGER PRIMARY KEY,
    room_key TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (trim(room_key) <> ''),
    name TEXT NOT NULL CHECK (trim(name) <> ''),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE participants (
    id INTEGER PRIMARY KEY,
    participant_key TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (trim(participant_key) <> ''),
    name TEXT NOT NULL CHECK (trim(name) <> ''),
    participant_type TEXT NOT NULL
        CHECK (participant_type IN ('human', 'ai', 'system')),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Membership is descriptive room history, not a hard authorization layer.
-- A participant may leave and later rejoin, creating multiple periods.
CREATE TABLE room_participants (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    left_at TEXT,

    CHECK (left_at IS NULL OR left_at > joined_at),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- Only one currently-open membership period per participant per room.
CREATE UNIQUE INDEX uq_room_participants_active
    ON room_participants(room_id, participant_id)
    WHERE left_at IS NULL;

-- Historical membership periods may not overlap.
CREATE TRIGGER room_participants_no_overlap_insert
BEFORE INSERT ON room_participants
WHEN EXISTS (
    SELECT 1
    FROM room_participants AS existing
    WHERE existing.room_id = NEW.room_id
      AND existing.participant_id = NEW.participant_id
      AND existing.joined_at
            < COALESCE(NEW.left_at, '9999-12-31T23:59:59.999Z')
      AND NEW.joined_at
            < COALESCE(existing.left_at, '9999-12-31T23:59:59.999Z')
)
BEGIN
    SELECT RAISE(ABORT, 'room membership periods may not overlap');
END;

CREATE TRIGGER room_participants_no_overlap_update
BEFORE UPDATE OF room_id, participant_id, joined_at, left_at
ON room_participants
WHEN EXISTS (
    SELECT 1
    FROM room_participants AS existing
    WHERE existing.id <> OLD.id
      AND existing.room_id = NEW.room_id
      AND existing.participant_id = NEW.participant_id
      AND existing.joined_at
            < COALESCE(NEW.left_at, '9999-12-31T23:59:59.999Z')
      AND NEW.joined_at
            < COALESCE(existing.left_at, '9999-12-31T23:59:59.999Z')
)
BEGIN
    SELECT RAISE(ABORT, 'room membership periods may not overlap');
END;

-- =========================================================
-- PARTICIPANT / AGENT CONFIGURATION HISTORY
-- =========================================================

CREATE TABLE participant_configs (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL,

    provider TEXT,
    model TEXT,
    config_label TEXT,

    system_instructions TEXT,

    settings_json TEXT
        CHECK (settings_json IS NULL OR json_valid(settings_json)),

    tools_json TEXT
        CHECK (tools_json IS NULL OR json_valid(tools_json)),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    UNIQUE (id, participant_id),

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- Once a configuration has produced canonical output or API history,
-- preserve it exactly so old behavior can be reconstructed.
CREATE TRIGGER participant_configs_immutable_once_used
BEFORE UPDATE ON participant_configs
WHEN EXISTS (
        SELECT 1 FROM messages
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM reflections
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM room_memories
        WHERE creator_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM tool_invocations
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM api_events
        WHERE participant_config_id = OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'a used participant configuration is immutable');
END;

CREATE TRIGGER participant_configs_no_delete_once_used
BEFORE DELETE ON participant_configs
WHEN EXISTS (
        SELECT 1 FROM messages
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM reflections
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM room_memories
        WHERE creator_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM tool_invocations
        WHERE participant_config_id = OLD.id
    )
    OR EXISTS (
        SELECT 1 FROM api_events
        WHERE participant_config_id = OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'a used participant configuration is immutable');
END;

-- =========================================================
-- TURNS
-- One turn groups an interaction chain, including model/tool loops.
-- =========================================================

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    initiated_by_participant_id INTEGER,

    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'completed', 'failed', 'cancelled')),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    completed_at TEXT,

    CHECK (
        (status = 'open' AND completed_at IS NULL)
        OR
        (
            status IN ('completed', 'failed', 'cancelled')
            AND completed_at IS NOT NULL
        )
    ),

    CHECK (completed_at IS NULL OR completed_at >= created_at),

    UNIQUE (id, room_id),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (initiated_by_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- Turn identity is stable, but an open turn can advance once to a terminal state.
CREATE TRIGGER turns_stable_identity
BEFORE UPDATE ON turns
WHEN NEW.id IS NOT OLD.id
  OR NEW.room_id IS NOT OLD.room_id
  OR NEW.initiated_by_participant_id IS NOT OLD.initiated_by_participant_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'turn identity is immutable');
END;

CREATE TRIGGER turns_terminal_state_immutable
BEFORE UPDATE ON turns
WHEN OLD.status <> 'open'
 AND (
     NEW.status IS NOT OLD.status
     OR NEW.completed_at IS NOT OLD.completed_at
 )
BEGIN
    SELECT RAISE(ABORT, 'a terminal turn cannot be changed');
END;

-- =========================================================
-- MESSAGES
-- Canonical room history.
-- room_sequence_no provides deterministic conversational order.
-- =========================================================

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,

    turn_id INTEGER,
    room_id INTEGER NOT NULL,

    room_sequence_no INTEGER NOT NULL
        CHECK (room_sequence_no > 0),

    turn_sequence_no INTEGER,

    participant_id INTEGER NOT NULL,
    participant_config_id INTEGER,

    reply_to_id INTEGER,

    message_type TEXT NOT NULL DEFAULT 'chat'
        CHECK (
            message_type IN (
                'chat',
                'system',
                'correction',
                'retraction'
            )
        ),

    message_text TEXT NOT NULL
        CHECK (trim(message_text) <> ''),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (
        (
            turn_id IS NULL
            AND turn_sequence_no IS NULL
            AND message_type = 'system'
        )
        OR
        (
            turn_id IS NOT NULL
            AND turn_sequence_no IS NOT NULL
            AND turn_sequence_no > 0
        )
    ),

    CHECK (
        message_type NOT IN ('correction', 'retraction')
        OR reply_to_id IS NOT NULL
    ),

    UNIQUE (id, room_id),
    UNIQUE (room_id, room_sequence_no),
    UNIQUE (turn_id, turn_sequence_no),

    FOREIGN KEY (turn_id, room_id)
        REFERENCES turns(id, room_id) ON DELETE RESTRICT,

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_config_id, participant_id)
        REFERENCES participant_configs(id, participant_id)
        ON DELETE RESTRICT,

    FOREIGN KEY (reply_to_id, room_id)
        REFERENCES messages(id, room_id) ON DELETE RESTRICT
);

-- Allocate room_sequence_no inside the same write transaction as INSERT,
-- preferably under BEGIN IMMEDIATE. The UNIQUE constraint is the final guard.

-- Canonical messages are never rewritten. Corrections/retractions are new messages.
CREATE TRIGGER messages_no_update
BEFORE UPDATE ON messages BEGIN
    SELECT RAISE(ABORT, 'messages is append-only');
END;

CREATE TRIGGER messages_no_delete
BEFORE DELETE ON messages BEGIN
    SELECT RAISE(ABORT, 'messages is append-only');
END;

-- AI-authored messages should always identify the exact configuration used.
CREATE TRIGGER messages_ai_config_required
BEFORE INSERT ON messages
WHEN NEW.participant_config_id IS NULL
 AND EXISTS (
    SELECT 1
    FROM participants
    WHERE id = NEW.participant_id
      AND participant_type = 'ai'
 )
BEGIN
    SELECT RAISE(ABORT, 'AI messages require a participant configuration');
END;

CREATE TRIGGER messages_reply_must_precede_message
BEFORE INSERT ON messages
WHEN NEW.reply_to_id IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM messages AS parent
    WHERE parent.id = NEW.reply_to_id
      AND parent.room_id = NEW.room_id
      AND parent.room_sequence_no >= NEW.room_sequence_no
 )
BEGIN
    SELECT RAISE(ABORT, 'a reply must follow its target in room sequence');
END;

-- =========================================================
-- TOPICS
-- topic_key is globally stable; display names only need to be unique
-- among siblings, allowing structures such as:
--   helios.identity
--   peter.identity
-- =========================================================

CREATE TABLE topics (
    id INTEGER PRIMARY KEY,

    topic_key TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (trim(topic_key) <> ''),

    name TEXT NOT NULL COLLATE NOCASE
        CHECK (trim(name) <> ''),

    parent_topic_id INTEGER,

    CHECK (parent_topic_id IS NULL OR parent_topic_id <> id),

    FOREIGN KEY (parent_topic_id)
        REFERENCES topics(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_topics_root_name
    ON topics(name COLLATE NOCASE)
    WHERE parent_topic_id IS NULL;

CREATE UNIQUE INDEX uq_topics_sibling_name
    ON topics(parent_topic_id, name COLLATE NOCASE)
    WHERE parent_topic_id IS NOT NULL;

CREATE TRIGGER topics_no_cycle
BEFORE UPDATE OF parent_topic_id ON topics
WHEN NEW.parent_topic_id IS NOT NULL
BEGIN
    WITH RECURSIVE ancestors(id) AS (
        SELECT NEW.parent_topic_id

        UNION

        SELECT topics.parent_topic_id
        FROM topics
        JOIN ancestors
          ON topics.id = ancestors.id
        WHERE topics.parent_topic_id IS NOT NULL
    )
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM ancestors
            WHERE id = OLD.id
        )
        THEN RAISE(ABORT, 'topic hierarchy may not contain a cycle')
    END;
END;

CREATE TABLE message_topics (
    message_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,

    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight BETWEEN 0.0 AND 1.0),

    PRIMARY KEY (message_id, topic_id),

    FOREIGN KEY (message_id)
        REFERENCES messages(id) ON DELETE CASCADE,

    FOREIGN KEY (topic_id)
        REFERENCES topics(id) ON DELETE RESTRICT
);

-- =========================================================
-- SEED IMPORT BATCHES
-- Imported continuity is separate from room-native memory.
-- =========================================================

CREATE TABLE seed_batches (
    id INTEGER PRIMARY KEY,

    name TEXT NOT NULL
        CHECK (trim(name) <> ''),

    source_type TEXT,
    source_uri TEXT,

    source_content_sha256 TEXT
        CHECK (
            source_content_sha256 IS NULL
            OR (
                length(source_content_sha256) = 64
                AND source_content_sha256 NOT GLOB '*[^0-9A-Fa-f]*'
            )
        ),

    source_created_at TEXT,
    source_description TEXT,
    notes TEXT,

    imported_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Seed provenance freezes once the batch has been used.
CREATE TRIGGER seed_batches_immutable_once_used
BEFORE UPDATE ON seed_batches
WHEN EXISTS (
    SELECT 1 FROM seed_memories
    WHERE seed_batch_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'a used seed batch is immutable');
END;

CREATE TRIGGER seed_batches_no_delete_once_used
BEFORE DELETE ON seed_batches
WHEN EXISTS (
    SELECT 1 FROM seed_memories
    WHERE seed_batch_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'a used seed batch is immutable');
END;

-- =========================================================
-- SEEDED MEMORIES
-- owner = whose inherited memory it is.
-- participant-subject links are optional and only describe room participants.
-- Other subjects belong in topics for v1.2.
-- =========================================================

CREATE TABLE seed_memories (
    id INTEGER PRIMARY KEY,

    seed_batch_id INTEGER,
    owner_participant_id INTEGER,

    memory_text TEXT NOT NULL
        CHECK (trim(memory_text) <> ''),

    category TEXT,

    importance REAL NOT NULL DEFAULT 0.5
        CHECK (importance BETWEEN 0.0 AND 1.0),

    confidence REAL NOT NULL DEFAULT 1.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),

    source_label TEXT,
    source_record_id TEXT,
    source_locator TEXT,

    active INTEGER NOT NULL DEFAULT 1
        CHECK (active IN (0, 1)),

    superseded_by INTEGER,

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (superseded_by IS NULL OR active = 0),
    CHECK (superseded_by IS NULL OR superseded_by <> id),

    FOREIGN KEY (seed_batch_id)
        REFERENCES seed_batches(id) ON DELETE RESTRICT,

    FOREIGN KEY (owner_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (superseded_by)
        REFERENCES seed_memories(id) ON DELETE RESTRICT
);

CREATE TABLE seed_memory_topics (
    seed_memory_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,

    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight BETWEEN 0.0 AND 1.0),

    PRIMARY KEY (seed_memory_id, topic_id),

    FOREIGN KEY (seed_memory_id)
        REFERENCES seed_memories(id) ON DELETE CASCADE,

    FOREIGN KEY (topic_id)
        REFERENCES topics(id) ON DELETE RESTRICT
);

CREATE TABLE seed_memory_participant_subjects (
    seed_memory_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL,

    PRIMARY KEY (seed_memory_id, participant_id),

    FOREIGN KEY (seed_memory_id)
        REFERENCES seed_memories(id) ON DELETE CASCADE,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- Seed wording and provenance are immutable. Interpretive state
-- (importance, confidence, category, active, superseded_by) may evolve.
CREATE TRIGGER seed_memories_stable_provenance
BEFORE UPDATE ON seed_memories
WHEN NEW.id IS NOT OLD.id
  OR NEW.seed_batch_id IS NOT OLD.seed_batch_id
  OR NEW.owner_participant_id IS NOT OLD.owner_participant_id
  OR NEW.memory_text IS NOT OLD.memory_text
  OR NEW.source_label IS NOT OLD.source_label
  OR NEW.source_record_id IS NOT OLD.source_record_id
  OR NEW.source_locator IS NOT OLD.source_locator
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(
        ABORT,
        'seed memory wording and provenance are immutable; supersede it instead'
    );
END;

CREATE TRIGGER seed_memories_no_delete
BEFORE DELETE ON seed_memories BEGIN
    SELECT RAISE(
        ABORT,
        'seed memory history is append-only; deactivate or supersede it'
    );
END;

CREATE TRIGGER seed_memories_supersession_owner
BEFORE UPDATE OF superseded_by ON seed_memories
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM seed_memories AS replacement
    WHERE replacement.id = NEW.superseded_by
      AND replacement.owner_participant_id
            IS NEW.owner_participant_id
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'seed memory replacement must have the same owner'
    );
END;

CREATE TRIGGER seed_memories_no_supersession_cycle
BEFORE UPDATE OF superseded_by ON seed_memories
WHEN NEW.superseded_by IS NOT NULL
BEGIN
    WITH RECURSIVE chain(id) AS (
        SELECT NEW.superseded_by

        UNION

        SELECT seed_memories.superseded_by
        FROM seed_memories
        JOIN chain
          ON seed_memories.id = chain.id
        WHERE seed_memories.superseded_by IS NOT NULL
    )
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM chain
            WHERE id = OLD.id
        )
        THEN RAISE(
            ABORT,
            'seed memory supersession may not contain a cycle'
        )
    END;
END;

-- =========================================================
-- ROOM MEMORIES
-- Room-native, interpretive memory.
-- owner = whose memory it is.
-- creator = who/what created the memory record.
-- This distinction matters if a separate memory-maintenance agent is added.
-- =========================================================

CREATE TABLE room_memories (
    id INTEGER PRIMARY KEY,

    room_id INTEGER NOT NULL,

    owner_participant_id INTEGER,

    creator_participant_id INTEGER NOT NULL,
    creator_config_id INTEGER,

    memory_text TEXT NOT NULL
        CHECK (trim(memory_text) <> ''),

    category TEXT,

    importance REAL NOT NULL DEFAULT 0.5
        CHECK (importance BETWEEN 0.0 AND 1.0),

    confidence REAL NOT NULL DEFAULT 1.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),

    active INTEGER NOT NULL DEFAULT 1
        CHECK (active IN (0, 1)),

    superseded_by INTEGER,

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (creator_config_id IS NULL OR creator_participant_id IS NOT NULL),
    CHECK (superseded_by IS NULL OR active = 0),
    CHECK (superseded_by IS NULL OR superseded_by <> id),

    UNIQUE (id, room_id),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (owner_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (creator_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (creator_config_id, creator_participant_id)
        REFERENCES participant_configs(id, participant_id)
        ON DELETE RESTRICT,

    FOREIGN KEY (superseded_by, room_id)
        REFERENCES room_memories(id, room_id) ON DELETE RESTRICT
);

CREATE TABLE room_memory_sources (
    room_memory_id INTEGER NOT NULL,
    room_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,

    PRIMARY KEY (room_memory_id, message_id),

    FOREIGN KEY (room_memory_id, room_id)
        REFERENCES room_memories(id, room_id) ON DELETE CASCADE,

    FOREIGN KEY (message_id, room_id)
        REFERENCES messages(id, room_id) ON DELETE RESTRICT
);

CREATE TABLE room_memory_topics (
    room_memory_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,

    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight BETWEEN 0.0 AND 1.0),

    PRIMARY KEY (room_memory_id, topic_id),

    FOREIGN KEY (room_memory_id)
        REFERENCES room_memories(id) ON DELETE CASCADE,

    FOREIGN KEY (topic_id)
        REFERENCES topics(id) ON DELETE RESTRICT
);

CREATE TABLE room_memory_participant_subjects (
    room_memory_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL,

    PRIMARY KEY (room_memory_id, participant_id),

    FOREIGN KEY (room_memory_id)
        REFERENCES room_memories(id) ON DELETE CASCADE,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- Room memory wording and provenance are preserved.
-- Interpretive state may be revised or superseded.
CREATE TRIGGER room_memories_stable_provenance
BEFORE UPDATE ON room_memories
WHEN NEW.id IS NOT OLD.id
  OR NEW.room_id IS NOT OLD.room_id
  OR NEW.owner_participant_id IS NOT OLD.owner_participant_id
  OR NEW.creator_participant_id IS NOT OLD.creator_participant_id
  OR NEW.creator_config_id IS NOT OLD.creator_config_id
  OR NEW.memory_text IS NOT OLD.memory_text
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(
        ABORT,
        'room memory wording and provenance are immutable; supersede it instead'
    );
END;

CREATE TRIGGER room_memories_no_delete
BEFORE DELETE ON room_memories BEGIN
    SELECT RAISE(
        ABORT,
        'room memory history is append-only; deactivate or supersede it'
    );
END;

CREATE TRIGGER room_memories_creator_ai_config_required
BEFORE INSERT ON room_memories
WHEN NEW.creator_config_id IS NULL
 AND EXISTS (
    SELECT 1
    FROM participants
    WHERE id = NEW.creator_participant_id
      AND participant_type = 'ai'
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'AI-created room memories require a participant configuration'
    );
END;

CREATE TRIGGER room_memories_supersession_owner
BEFORE UPDATE OF superseded_by ON room_memories
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM room_memories AS replacement
    WHERE replacement.id = NEW.superseded_by
      AND replacement.room_id = NEW.room_id
      AND replacement.owner_participant_id
            IS NEW.owner_participant_id
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'room memory replacement must be in the same room and have the same owner'
    );
END;

CREATE TRIGGER room_memories_no_supersession_cycle
BEFORE UPDATE OF superseded_by ON room_memories
WHEN NEW.superseded_by IS NOT NULL
BEGIN
    WITH RECURSIVE chain(id) AS (
        SELECT NEW.superseded_by

        UNION

        SELECT room_memories.superseded_by
        FROM room_memories
        JOIN chain
          ON room_memories.id = chain.id
        WHERE room_memories.superseded_by IS NOT NULL
    )
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM chain
            WHERE id = OLD.id
        )
        THEN RAISE(
            ABORT,
            'room memory supersession may not contain a cycle'
        )
    END;
END;

-- =========================================================
-- REFLECTIONS
-- Longer-form agent evaluations, distinct from ordinary memories.
-- Raw API history remains the ultimate audit trail.
-- =========================================================

CREATE TABLE reflections (
    id INTEGER PRIMARY KEY,

    room_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL,
    participant_config_id INTEGER,

    source_turn_id INTEGER,

    reflection_text TEXT NOT NULL
        CHECK (trim(reflection_text) <> ''),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_config_id, participant_id)
        REFERENCES participant_configs(id, participant_id)
        ON DELETE RESTRICT,

    FOREIGN KEY (source_turn_id, room_id)
        REFERENCES turns(id, room_id) ON DELETE RESTRICT
);

CREATE TRIGGER reflections_ai_config_required
BEFORE INSERT ON reflections
WHEN NEW.participant_config_id IS NULL
 AND EXISTS (
    SELECT 1
    FROM participants
    WHERE id = NEW.participant_id
      AND participant_type = 'ai'
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'AI reflections require a participant configuration'
    );
END;

-- =========================================================
-- OPEN THREADS
-- Questions or ideas explicitly worth returning to later.
-- These are operational and intentionally editable.
-- =========================================================

CREATE TABLE open_threads (
    id INTEGER PRIMARY KEY,

    room_id INTEGER NOT NULL,
    owner_participant_id INTEGER,
    source_message_id INTEGER,

    topic_text TEXT NOT NULL
        CHECK (trim(topic_text) <> ''),

    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'abandoned')),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    resolved_at TEXT,

    CHECK (
        (status = 'open' AND resolved_at IS NULL)
        OR
        (
            status IN ('resolved', 'abandoned')
            AND resolved_at IS NOT NULL
        )
    ),

    CHECK (resolved_at IS NULL OR resolved_at >= created_at),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (owner_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (source_message_id, room_id)
        REFERENCES messages(id, room_id) ON DELETE RESTRICT
);

-- =========================================================
-- NORMALIZED TOOL INVOCATIONS
-- Provider-neutral tool history for /lasttool and /trace.
-- Exact provider payloads are still stored in api_events.
-- =========================================================

CREATE TABLE tool_invocations (
    id INTEGER PRIMARY KEY,

    turn_id INTEGER NOT NULL,
    room_id INTEGER NOT NULL,

    participant_id INTEGER NOT NULL,
    participant_config_id INTEGER,

    invocation_sequence_no INTEGER NOT NULL
        CHECK (invocation_sequence_no > 0),

    provider_call_id TEXT,

    tool_name TEXT NOT NULL
        CHECK (trim(tool_name) <> ''),

    arguments_json TEXT NOT NULL
        CHECK (json_valid(arguments_json)),

    result_json TEXT
        CHECK (result_json IS NULL OR json_valid(result_json)),

    status TEXT NOT NULL DEFAULT 'requested'
        CHECK (
            status IN (
                'requested',
                'completed',
                'failed',
                'cancelled'
            )
        ),

    requested_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    completed_at TEXT,

    error_text TEXT,

    CHECK (
        (status = 'requested' AND completed_at IS NULL)
        OR
        (
            status IN ('completed', 'failed', 'cancelled')
            AND completed_at IS NOT NULL
        )
    ),

    CHECK (completed_at IS NULL OR completed_at >= requested_at),

    UNIQUE (turn_id, invocation_sequence_no),

    FOREIGN KEY (turn_id, room_id)
        REFERENCES turns(id, room_id) ON DELETE RESTRICT,

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_config_id, participant_id)
        REFERENCES participant_configs(id, participant_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER tool_invocations_ai_config_required
BEFORE INSERT ON tool_invocations
WHEN NEW.participant_config_id IS NULL
 AND EXISTS (
    SELECT 1
    FROM participants
    WHERE id = NEW.participant_id
      AND participant_type = 'ai'
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'AI tool invocations require a participant configuration'
    );
END;

-- =========================================================
-- RAW API EVENT LOG
-- Sanitized provider payloads for exact debugging/replay.
--
-- Application policy: treat as immutable during normal operation.
-- Database policy: DO NOT hard-lock update/delete because security
-- redaction must remain possible if a secret is ever logged accidentally.
-- =========================================================

CREATE TABLE api_events (
    id INTEGER PRIMARY KEY,

    turn_id INTEGER NOT NULL,
    room_id INTEGER NOT NULL,

    participant_id INTEGER,
    participant_config_id INTEGER,

    tool_invocation_id INTEGER,

    sequence_no INTEGER NOT NULL
        CHECK (sequence_no > 0),

    event_type TEXT NOT NULL
        CHECK (trim(event_type) <> ''),

    related_message_id INTEGER,

    payload_json TEXT NOT NULL
        CHECK (json_valid(payload_json)),

    is_redacted INTEGER NOT NULL DEFAULT 0
        CHECK (is_redacted IN (0, 1)),

    redacted_at TEXT,
    redaction_reason TEXT,

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (participant_config_id IS NULL OR participant_id IS NOT NULL),

    CHECK (
        (is_redacted = 0 AND redacted_at IS NULL)
        OR
        (is_redacted = 1 AND redacted_at IS NOT NULL)
    ),

    UNIQUE (turn_id, sequence_no),

    FOREIGN KEY (turn_id, room_id)
        REFERENCES turns(id, room_id) ON DELETE RESTRICT,

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT,

    FOREIGN KEY (participant_config_id, participant_id)
        REFERENCES participant_configs(id, participant_id)
        ON DELETE RESTRICT,

    FOREIGN KEY (tool_invocation_id)
        REFERENCES tool_invocations(id) ON DELETE RESTRICT,

    FOREIGN KEY (related_message_id, room_id)
        REFERENCES messages(id, room_id) ON DELETE RESTRICT
);

CREATE TRIGGER api_events_ai_config_required
BEFORE INSERT ON api_events
WHEN NEW.participant_id IS NOT NULL
 AND NEW.participant_config_id IS NULL
 AND EXISTS (
    SELECT 1
    FROM participants
    WHERE id = NEW.participant_id
      AND participant_type = 'ai'
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'AI API events require a participant configuration'
    );
END;

-- =========================================================
-- LOCAL DEBUG / ADMIN EVENTS
-- Slash commands are intercepted locally and are not normal chat messages.
-- Administrative changes should be recorded here by the application.
-- =========================================================

CREATE TABLE admin_events (
    id INTEGER PRIMARY KEY,

    room_id INTEGER,
    actor_participant_id INTEGER,

    command TEXT NOT NULL
        CHECK (trim(command) <> ''),

    arguments_json TEXT
        CHECK (arguments_json IS NULL OR json_valid(arguments_json)),

    result_json TEXT
        CHECK (result_json IS NULL OR json_valid(result_json)),

    result_summary TEXT,

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (room_id)
        REFERENCES rooms(id) ON DELETE RESTRICT,

    FOREIGN KEY (actor_participant_id)
        REFERENCES participants(id) ON DELETE RESTRICT
);

-- =========================================================
-- NORMAL INDEXES
-- =========================================================

CREATE INDEX idx_room_participants_participant
    ON room_participants(participant_id);

CREATE INDEX idx_room_participants_room_participant_period
    ON room_participants(
        room_id,
        participant_id,
        joined_at,
        left_at
    );

CREATE INDEX idx_participant_configs_participant_created
    ON participant_configs(participant_id, created_at);

CREATE INDEX idx_turns_room_created
    ON turns(room_id, created_at);

CREATE INDEX idx_messages_room_created
    ON messages(room_id, created_at);

CREATE INDEX idx_messages_participant_created
    ON messages(participant_id, created_at);

CREATE INDEX idx_messages_turn
    ON messages(turn_id);

CREATE INDEX idx_messages_reply_to
    ON messages(reply_to_id);

CREATE INDEX idx_message_topics_topic
    ON message_topics(topic_id);

CREATE INDEX idx_topics_parent
    ON topics(parent_topic_id);

CREATE INDEX idx_seed_memories_owner_active
    ON seed_memories(owner_participant_id, active);

CREATE INDEX idx_seed_memories_batch
    ON seed_memories(seed_batch_id);

CREATE INDEX idx_seed_memory_topics_topic
    ON seed_memory_topics(topic_id);

CREATE INDEX idx_seed_memory_participant_subjects_participant
    ON seed_memory_participant_subjects(participant_id);

CREATE INDEX idx_room_memories_room_owner_active
    ON room_memories(room_id, owner_participant_id, active);

CREATE INDEX idx_room_memories_creator_created
    ON room_memories(creator_participant_id, created_at);

CREATE INDEX idx_room_memory_sources_message
    ON room_memory_sources(message_id, room_id);

CREATE INDEX idx_room_memory_topics_topic
    ON room_memory_topics(topic_id);

CREATE INDEX idx_room_memory_participant_subjects_participant
    ON room_memory_participant_subjects(participant_id);

CREATE INDEX idx_reflections_room_participant_created
    ON reflections(room_id, participant_id, created_at);

CREATE INDEX idx_reflections_source_turn
    ON reflections(source_turn_id, room_id);

CREATE INDEX idx_open_threads_room_status
    ON open_threads(room_id, status);

CREATE INDEX idx_open_threads_source_message
    ON open_threads(source_message_id, room_id);

CREATE INDEX idx_tool_invocations_turn
    ON tool_invocations(turn_id, invocation_sequence_no);

CREATE INDEX idx_tool_invocations_tool_created
    ON tool_invocations(tool_name, requested_at);

CREATE INDEX idx_api_events_room_created
    ON api_events(room_id, created_at);

CREATE INDEX idx_api_events_participant_created
    ON api_events(participant_id, created_at);

CREATE INDEX idx_api_events_related_message
    ON api_events(related_message_id, room_id);

CREATE INDEX idx_api_events_tool_invocation
    ON api_events(tool_invocation_id);

CREATE INDEX idx_admin_events_room_created
    ON admin_events(room_id, created_at);

-- =========================================================
-- FULL-TEXT SEARCH (FTS5)
-- Canonical text itself is immutable, so only INSERT synchronization
-- is required for these external-content indexes.
-- =========================================================

CREATE VIRTUAL TABLE messages_fts USING fts5(
    message_text,
    content='messages',
    content_rowid='id'
);

CREATE VIRTUAL TABLE seed_memories_fts USING fts5(
    memory_text,
    content='seed_memories',
    content_rowid='id'
);

CREATE VIRTUAL TABLE room_memories_fts USING fts5(
    memory_text,
    content='room_memories',
    content_rowid='id'
);

CREATE TRIGGER messages_fts_insert
AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, message_text)
    VALUES (NEW.id, NEW.message_text);
END;

CREATE TRIGGER seed_memories_fts_insert
AFTER INSERT ON seed_memories BEGIN
    INSERT INTO seed_memories_fts(rowid, memory_text)
    VALUES (NEW.id, NEW.memory_text);
END;

CREATE TRIGGER room_memories_fts_insert
AFTER INSERT ON room_memories BEGIN
    INSERT INTO room_memories_fts(rowid, memory_text)
    VALUES (NEW.id, NEW.memory_text);
END;

-- =========================================================
-- HELPFUL VIEWS
-- =========================================================

CREATE VIEW current_room_participants AS
SELECT
    rp.id,
    rp.room_id,
    rp.participant_id,
    rp.joined_at
FROM room_participants AS rp
WHERE rp.left_at IS NULL;

CREATE VIEW active_seed_memories AS
SELECT *
FROM seed_memories
WHERE active = 1
  AND superseded_by IS NULL;

CREATE VIEW active_room_memories AS
SELECT *
FROM room_memories
WHERE active = 1
  AND superseded_by IS NULL;


-- =========================================================
-- PARTICIPANT IDENTITY AND ADDRESSING V1
-- =========================================================

-- Helios Room schema migration v1.2 -> v1.3.
-- Transaction ownership and all data backfill belong to app.migration.

CREATE UNIQUE INDEX messages_identity_scope
    ON messages(id, room_id, participant_id);

CREATE TABLE participant_aliases (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL,
    display_alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (participant_id) REFERENCES participants(id),
    UNIQUE (alias_key),
    UNIQUE (id, participant_id)
);

CREATE TABLE participant_primary_aliases (
    participant_id INTEGER PRIMARY KEY,
    alias_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY (participant_id) REFERENCES participants(id),
    FOREIGN KEY (alias_id, participant_id)
        REFERENCES participant_aliases(id, participant_id)
);

CREATE TABLE message_routes (
    message_id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    sender_participant_id INTEGER NOT NULL,
    sender_alias_id INTEGER NOT NULL,
    destination_kind TEXT NOT NULL
        CHECK (destination_kind IN ('participant', 'room')),
    recipient_participant_id INTEGER,
    destination_alias_id INTEGER NOT NULL,
    routing_mode TEXT NOT NULL
        CHECK (routing_mode IN ('legacy_implicit', 'explicit')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (destination_kind = 'participant' AND recipient_participant_id IS NOT NULL)
        OR (destination_kind = 'room' AND recipient_participant_id IS NULL)
    ),
    FOREIGN KEY (message_id, room_id, sender_participant_id)
        REFERENCES messages(id, room_id, participant_id),
    FOREIGN KEY (sender_alias_id, sender_participant_id)
        REFERENCES participant_aliases(id, participant_id),
    FOREIGN KEY (recipient_participant_id) REFERENCES participants(id),
    FOREIGN KEY (destination_alias_id) REFERENCES participant_aliases(id)
);

CREATE TABLE participant_name_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('bootstrap', 'adopted')),
    room_id INTEGER,
    actor_participant_id INTEGER NOT NULL,
    subject_participant_id INTEGER NOT NULL,
    previous_alias_id INTEGER,
    new_alias_id INTEGER NOT NULL,
    canonical_message_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (actor_participant_id = subject_participant_id),
    CHECK (
        (event_type = 'bootstrap'
         AND room_id IS NULL
         AND previous_alias_id IS NULL
         AND canonical_message_id IS NULL)
        OR
        (event_type = 'adopted'
         AND room_id IS NOT NULL
         AND previous_alias_id IS NOT NULL
         AND canonical_message_id IS NOT NULL)
    ),
    FOREIGN KEY (room_id) REFERENCES rooms(id),
    FOREIGN KEY (actor_participant_id) REFERENCES participants(id),
    FOREIGN KEY (subject_participant_id) REFERENCES participants(id),
    FOREIGN KEY (previous_alias_id, subject_participant_id)
        REFERENCES participant_aliases(id, participant_id),
    FOREIGN KEY (new_alias_id, subject_participant_id)
        REFERENCES participant_aliases(id, participant_id),
    FOREIGN KEY (canonical_message_id, room_id)
        REFERENCES messages(id, room_id)
);

CREATE TRIGGER participant_aliases_no_update
BEFORE UPDATE ON participant_aliases BEGIN
    SELECT RAISE(ABORT, 'participant aliases are immutable');
END;

CREATE TRIGGER participant_aliases_no_delete
BEFORE DELETE ON participant_aliases BEGIN
    SELECT RAISE(ABORT, 'participant aliases are immutable');
END;

CREATE TRIGGER participant_primary_aliases_valid_insert
BEFORE INSERT ON participant_primary_aliases BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM participant_aliases
        WHERE id = NEW.alias_id AND participant_id = NEW.participant_id
    ) THEN RAISE(ABORT, 'primary alias ownership invalid') END;
END;

CREATE TRIGGER participant_primary_aliases_valid_update
BEFORE UPDATE ON participant_primary_aliases BEGIN
    SELECT CASE WHEN NEW.participant_id <> OLD.participant_id
        THEN RAISE(ABORT, 'primary alias participant is immutable') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM participant_aliases
        WHERE id = NEW.alias_id AND participant_id = NEW.participant_id
    ) THEN RAISE(ABORT, 'primary alias ownership invalid') END;
END;

CREATE TRIGGER participant_primary_aliases_no_delete
BEFORE DELETE ON participant_primary_aliases BEGIN
    SELECT RAISE(ABORT, 'primary aliases cannot be deleted');
END;

CREATE TRIGGER participants_identity_no_update
BEFORE UPDATE ON participants BEGIN
    SELECT CASE WHEN
        NEW.id <> OLD.id
        OR NEW.participant_key <> OLD.participant_key
        OR NEW.name <> OLD.name
        OR NEW.participant_type <> OLD.participant_type
        OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'participant identity is immutable') END;
END;

CREATE TRIGGER participants_no_delete
BEFORE DELETE ON participants BEGIN
    SELECT RAISE(ABORT, 'participants cannot be deleted');
END;

CREATE TRIGGER message_routes_valid_insert
BEFORE INSERT ON message_routes BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM messages
        WHERE id = NEW.message_id
          AND room_id = NEW.room_id
          AND participant_id = NEW.sender_participant_id
    ) THEN RAISE(ABORT, 'route message identity invalid') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM participant_aliases
        WHERE id = NEW.sender_alias_id
          AND participant_id = NEW.sender_participant_id
    ) THEN RAISE(ABORT, 'route sender alias invalid') END;
    SELECT CASE WHEN NEW.destination_kind = 'participant' AND NOT EXISTS (
        SELECT 1 FROM participant_aliases
        WHERE id = NEW.destination_alias_id
          AND participant_id = NEW.recipient_participant_id
    ) THEN RAISE(ABORT, 'route destination alias invalid') END;
    SELECT CASE WHEN NEW.destination_kind = 'room' AND NOT EXISTS (
        SELECT 1
        FROM participant_aliases AS pa
        JOIN participants AS p ON p.id = pa.participant_id
        WHERE pa.id = NEW.destination_alias_id
          AND pa.alias_key = 'room'
          AND p.participant_key = 'room-system'
          AND p.participant_type = 'system'
    ) THEN RAISE(ABORT, 'room route destination invalid') END;
END;

CREATE TRIGGER message_routes_no_update
BEFORE UPDATE ON message_routes BEGIN
    SELECT RAISE(ABORT, 'message routes are immutable');
END;

CREATE TRIGGER message_routes_no_delete
BEFORE DELETE ON message_routes BEGIN
    SELECT RAISE(ABORT, 'message routes are immutable');
END;

CREATE TRIGGER participant_name_events_valid_insert
BEFORE INSERT ON participant_name_events BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM participant_aliases
        WHERE id = NEW.new_alias_id
          AND participant_id = NEW.subject_participant_id
    ) THEN RAISE(ABORT, 'name event alias ownership invalid') END;
    SELECT CASE WHEN NEW.event_type = 'adopted' AND NOT EXISTS (
        SELECT 1
        FROM messages AS m
        JOIN participants AS p ON p.id = m.participant_id
        JOIN message_routes AS mr ON mr.message_id = m.id
        JOIN participant_aliases AS sa ON sa.id = mr.sender_alias_id
        JOIN participant_aliases AS da ON da.id = mr.destination_alias_id
        JOIN participants AS dp ON dp.id = da.participant_id
        WHERE m.id = NEW.canonical_message_id
          AND m.room_id = NEW.room_id
          AND m.message_type = 'system'
          AND p.participant_key = 'room-system'
          AND p.participant_type = 'system'
          AND sa.participant_id = p.id
          AND mr.destination_kind = 'room'
          AND mr.routing_mode = 'explicit'
          AND da.alias_key = 'room'
          AND dp.participant_key = 'room-system'
    ) THEN RAISE(ABORT, 'adopted name event canonical message invalid') END;
END;

CREATE TRIGGER participant_name_events_no_update
BEFORE UPDATE ON participant_name_events BEGIN
    SELECT RAISE(ABORT, 'participant name events are immutable');
END;

CREATE TRIGGER participant_name_events_no_delete
BEFORE DELETE ON participant_name_events BEGIN
    SELECT RAISE(ABORT, 'participant name events are immutable');
END;

CREATE INDEX idx_participant_aliases_participant
    ON participant_aliases(participant_id, id);

CREATE INDEX idx_message_routes_room_message
    ON message_routes(room_id, message_id);

CREATE INDEX idx_name_events_subject_created
    ON participant_name_events(subject_participant_id, id);

-- =========================================================
-- RECORD SCHEMA VERSION
-- =========================================================

INSERT INTO schema_migrations (migration_no, schema_label, description)
VALUES
    (1, '1.2', 'Helios Room schema v1.2 baseline.'),
    (2, '1.3', 'Participant identity, immutable aliases and name history, room-system identity, and immutable message routing.');
