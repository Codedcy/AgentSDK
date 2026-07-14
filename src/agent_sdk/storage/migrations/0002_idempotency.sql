CREATE TABLE idempotency_records(
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(scope, key)
);
CREATE INDEX idempotency_records_session
    ON idempotency_records(session_id);
