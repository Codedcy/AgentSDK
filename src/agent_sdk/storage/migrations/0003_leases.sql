CREATE TABLE leases(
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(run_id)) > 0),
    owner TEXT NOT NULL CHECK(length(trim(owner)) > 0),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    acquired_at TEXT NOT NULL CHECK(length(acquired_at) > 0),
    renewed_at TEXT NOT NULL CHECK(length(renewed_at) > 0),
    expires_at TEXT NOT NULL CHECK(
        length(expires_at) > 0
        AND renewed_at >= acquired_at
        AND expires_at > renewed_at
    )
);
CREATE INDEX leases_expires_at ON leases(expires_at);

CREATE TABLE external_operations(
    operation_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(operation_id)) > 0),
    operation_kind TEXT NOT NULL CHECK(operation_kind IN ('model_call', 'tool_call')),
    session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    turn INTEGER NOT NULL CHECK(turn >= 0),
    request_fingerprint TEXT NOT NULL CHECK(length(trim(request_fingerprint)) > 0),
    provider_identity TEXT,
    tool_identity TEXT,
    lease_generation INTEGER NOT NULL CHECK(lease_generation >= 1),
    status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
    data_json TEXT NOT NULL CHECK(
        json_valid(data_json) AND json_type(data_json) = 'object'
    ),
    UNIQUE(run_id, turn, operation_kind, operation_id),
    UNIQUE(operation_id, run_id, session_id),
    CHECK(
        (operation_kind = 'model_call'
            AND provider_identity IS NOT NULL
            AND length(trim(provider_identity)) > 0
            AND tool_identity IS NULL)
        OR
        (operation_kind = 'tool_call'
            AND provider_identity IS NULL
            AND tool_identity IS NOT NULL
            AND length(trim(tool_identity)) > 0)
    )
);
CREATE INDEX external_operations_session ON external_operations(session_id);
CREATE INDEX external_operations_run_status ON external_operations(run_id, status);

CREATE TABLE run_checkpoints(
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(run_id)) > 0),
    session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
    checkpoint_version INTEGER NOT NULL CHECK(checkpoint_version >= 1),
    turn INTEGER NOT NULL CHECK(turn >= 0),
    phase TEXT NOT NULL CHECK(phase IN (
        'ready_for_model', 'model_in_flight', 'ready_for_tool',
        'tool_in_flight', 'waiting', 'terminal'
    )),
    operation_id TEXT,
    data_json TEXT NOT NULL CHECK(
        json_valid(data_json) AND json_type(data_json) = 'object'
    ),
    FOREIGN KEY(operation_id, run_id, session_id)
        REFERENCES external_operations(operation_id, run_id, session_id)
        ON DELETE RESTRICT,
    CHECK(
        (phase IN ('model_in_flight', 'tool_in_flight') AND operation_id IS NOT NULL)
        OR
        (phase NOT IN ('model_in_flight', 'tool_in_flight') AND operation_id IS NULL)
    )
);
CREATE INDEX run_checkpoints_session ON run_checkpoints(session_id);
CREATE INDEX run_checkpoints_phase ON run_checkpoints(phase);
CREATE INDEX run_checkpoints_operation ON run_checkpoints(operation_id);

CREATE TABLE reconciliation_requests(
    request_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(request_id)) > 0),
    session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    operation_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
    data_json TEXT NOT NULL CHECK(
        json_valid(data_json) AND json_type(data_json) = 'object'
    ),
    FOREIGN KEY(operation_id, run_id, session_id)
        REFERENCES external_operations(operation_id, run_id, session_id)
        ON DELETE RESTRICT
);
CREATE INDEX reconciliation_requests_session ON reconciliation_requests(session_id);
CREATE INDEX reconciliation_requests_run_status
    ON reconciliation_requests(run_id, status);
CREATE INDEX reconciliation_requests_operation
    ON reconciliation_requests(operation_id);
