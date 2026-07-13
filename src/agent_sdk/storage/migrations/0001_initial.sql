CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE events(
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE snapshots(
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY(kind, entity_id)
);

CREATE INDEX events_session_cursor ON events(session_id, cursor);
CREATE UNIQUE INDEX events_aggregate_sequence
    ON events(COALESCE(run_id, session_id), sequence);
CREATE INDEX snapshots_session ON snapshots(session_id);
