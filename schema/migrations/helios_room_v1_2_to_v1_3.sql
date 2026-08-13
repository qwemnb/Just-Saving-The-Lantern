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
