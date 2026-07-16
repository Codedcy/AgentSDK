CREATE TABLE schema_migrations_next(
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL CHECK(
        length(checksum) = 64
        AND checksum NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at TEXT NOT NULL CHECK(length(applied_at) > 0)
);

CREATE TABLE artifact_generations(
    digest TEXT NOT NULL CHECK(
        length(digest) = 64
        AND digest NOT GLOB '*[^0-9a-f]*'
    ),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    physical_path TEXT NOT NULL UNIQUE CHECK(length(trim(physical_path)) > 0),
    size INTEGER NOT NULL CHECK(size >= 0),
    mime_type TEXT NOT NULL CHECK(length(trim(mime_type)) > 0),
    redaction_json TEXT NOT NULL DEFAULT '{}' CHECK(
        json_valid(redaction_json) AND json_type(redaction_json) = 'object'
    ),
    state TEXT NOT NULL CHECK(
        state IN ('publishing', 'ready', 'delete_pending', 'deleting')
    ),
    claim_token TEXT,
    claim_expires_at TEXT,
    PRIMARY KEY(digest, generation),
    CHECK(
        (state IN ('publishing', 'deleting')
            AND claim_token IS NOT NULL
            AND length(trim(claim_token)) > 0
            AND claim_expires_at IS NOT NULL
            AND length(claim_expires_at) > 0)
        OR
        (state IN ('ready', 'delete_pending')
            AND claim_token IS NULL
            AND claim_expires_at IS NULL)
    )
);

CREATE TABLE artifact_heads(
    digest TEXT PRIMARY KEY NOT NULL,
    generation INTEGER NOT NULL,
    FOREIGN KEY(digest, generation)
        REFERENCES artifact_generations(digest, generation)
        ON DELETE RESTRICT
);

CREATE TABLE artifact_owners(
    session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
    digest TEXT NOT NULL,
    generation INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'active')),
    created_at TEXT NOT NULL CHECK(length(created_at) > 0),
    PRIMARY KEY(session_id, digest),
    FOREIGN KEY(digest, generation)
        REFERENCES artifact_generations(digest, generation)
        ON DELETE RESTRICT
);

CREATE TABLE artifact_cleanup_jobs(
    job_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(job_id)) > 0),
    digest TEXT NOT NULL CHECK(
        length(digest) = 64
        AND digest NOT GLOB '*[^0-9a-f]*'
    ),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    physical_path TEXT NOT NULL CHECK(length(trim(physical_path)) > 0),
    state TEXT NOT NULL CHECK(state IN ('delete_pending', 'deleting', 'complete')),
    claim_token TEXT,
    claim_expires_at TEXT,
    created_at TEXT NOT NULL CHECK(length(created_at) > 0),
    UNIQUE(digest, generation, physical_path),
    CHECK(
        (state = 'delete_pending' AND claim_token IS NULL AND claim_expires_at IS NULL)
        OR
        (state = 'deleting'
            AND claim_token IS NOT NULL
            AND length(trim(claim_token)) > 0
            AND claim_expires_at IS NOT NULL
            AND length(claim_expires_at) > 0)
        OR
        (state = 'complete' AND claim_token IS NULL AND claim_expires_at IS NULL)
    )
);

CREATE INDEX artifact_generations_state_claim
    ON artifact_generations(state, claim_expires_at);
CREATE INDEX artifact_owners_session
    ON artifact_owners(session_id);
CREATE INDEX artifact_owners_generation
    ON artifact_owners(digest, generation, state);
CREATE INDEX artifact_cleanup_jobs_state_claim
    ON artifact_cleanup_jobs(state, claim_expires_at);

DROP TABLE schema_migrations;
ALTER TABLE schema_migrations_next RENAME TO schema_migrations;
