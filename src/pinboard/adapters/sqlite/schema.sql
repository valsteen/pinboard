-- SQLite authority schema version 4.
PRAGMA foreign_keys = ON;

CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 4),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE artifact_refs (
    artifact_ref_id INTEGER PRIMARY KEY,
    artifact_key TEXT NOT NULL,
    artifact_revision INTEGER NOT NULL CHECK (artifact_revision >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('requirements', 'brief', 'result', 'evidence')),
    relative_path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    accepted_revision INTEGER NOT NULL CHECK (accepted_revision >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (kind, artifact_key, artifact_revision),
    UNIQUE (artifact_ref_id, kind)
) STRICT;

CREATE TABLE work_items (
    item_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN (
        'intake', 'ready', 'active', 'paused', 'blocked', 'deferred', 'review',
        'done', 'superseded', 'dropped'
    )),
    timing TEXT CHECK (timing IN ('must-now', 'cheaper-now', 'safe-to-defer')),
    source TEXT,
    outcome_evidence TEXT,
    next_action TEXT,
    notes TEXT,
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    queue_position INTEGER UNIQUE CHECK (queue_position IS NULL OR queue_position >= 1),
    UNIQUE (item_id, state, outcome_evidence),
    CHECK ((state IN ('done', 'superseded', 'dropped')) = (outcome_evidence IS NOT NULL)),
    CHECK ((state IN ('done', 'superseded', 'dropped')) = (queue_position IS NULL))
) STRICT;

CREATE TABLE work_item_definition_revisions (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    definition_digest TEXT NOT NULL CHECK (length(definition_digest) = 64),
    definition_json BLOB NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) >= 1),
    source_task_id TEXT NOT NULL CHECK (length(source_task_id) >= 1),
    before_digest TEXT CHECK (before_digest IS NULL OR length(before_digest) = 64),
    after_digest TEXT NOT NULL CHECK (length(after_digest) = 64),
    accepted_project_revision INTEGER NOT NULL CHECK (accepted_project_revision >= 0),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (item_id, definition_revision),
    UNIQUE (item_id, definition_revision, definition_digest),
    CHECK (definition_digest = after_digest),
    CHECK ((definition_revision = 1) = (before_digest IS NULL))
) STRICT;

CREATE TABLE item_dependencies (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    dependency_id TEXT NOT NULL REFERENCES work_items(item_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (item_id, dependency_id),
    UNIQUE (item_id, position),
    CHECK (item_id <> dependency_id)
) STRICT;

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES work_items(item_id),
    state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'blocked', 'review', 'done')),
    branch TEXT NOT NULL,
    base_revision TEXT NOT NULL,
    provenance TEXT NOT NULL,
    brief_artifact_ref_id INTEGER NOT NULL,
    brief_artifact_kind TEXT NOT NULL CHECK (brief_artifact_kind = 'brief'),
    result_artifact_ref_id INTEGER,
    result_artifact_kind TEXT CHECK (result_artifact_kind = 'result'),
    candidate_revision TEXT,
    candidate_recorded_at TEXT,
    accepted_scope_revision INTEGER NOT NULL CHECK (accepted_scope_revision >= 1),
    accepted_scope_digest TEXT NOT NULL CHECK (length(accepted_scope_digest) = 64),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (attempt_id, item_id),
    FOREIGN KEY (brief_artifact_ref_id, brief_artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    FOREIGN KEY (result_artifact_ref_id, result_artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    CHECK ((result_artifact_ref_id IS NULL) = (result_artifact_kind IS NULL)),
    CHECK ((candidate_revision IS NULL) = (candidate_recorded_at IS NULL)),
    CHECK (
        (state = 'review' AND candidate_revision IS NOT NULL)
        OR state = 'done'
        OR (state IN ('active', 'paused', 'blocked') AND candidate_revision IS NULL)
    )
) STRICT;

CREATE UNIQUE INDEX one_live_attempt_per_item
ON attempts(item_id)
WHERE state != 'done';

CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    user_label TEXT NOT NULL,
    trigger TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN (
        'independent', 'prerequisite', 'follow-up', 'duplicate', 'contradiction', 'clarification'
    )),
    relation_item_id TEXT REFERENCES work_items(item_id),
    effect TEXT NOT NULL,
    unlock TEXT NOT NULL,
    urgency_evidence TEXT NOT NULL,
    disposition TEXT CHECK (disposition IN ('accepted', 'merged', 'returned', 'rejected')),
    disposition_target_item_id TEXT REFERENCES work_items(item_id),
    disposition_reason TEXT,
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    disposition_recorded_at TEXT,
    CHECK ((disposition IS NULL) = (disposition_recorded_at IS NULL)),
    CHECK (
        (disposition IS NULL AND disposition_target_item_id IS NULL AND disposition_reason IS NULL)
        OR (disposition IN ('accepted', 'merged') AND disposition_target_item_id IS NOT NULL AND disposition_reason IS NULL)
        OR (disposition IN ('returned', 'rejected') AND disposition_target_item_id IS NULL AND disposition_reason IS NOT NULL)
    ),
    CHECK (
        (relation_kind IN ('independent', 'clarification') AND relation_item_id IS NULL)
        OR (relation_kind IN ('prerequisite', 'follow-up', 'duplicate', 'contradiction') AND relation_item_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE proposal_evidence (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    selector TEXT NOT NULL,
    PRIMARY KEY (proposal_id, position)
) STRICT;

CREATE TABLE proposal_freshness (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    assumption TEXT NOT NULL,
    PRIMARY KEY (proposal_id, position)
) STRICT;

CREATE TABLE attempt_lease_counters (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    generation_high_water INTEGER NOT NULL CHECK (generation_high_water >= 0)
) STRICT;

CREATE TABLE attempt_lease_generations (
    attempt_id TEXT NOT NULL REFERENCES attempt_lease_counters(attempt_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    PRIMARY KEY (attempt_id, generation),
    UNIQUE (attempt_id, lease_id, generation, task_id, host_id)
) STRICT;

CREATE TABLE attempt_leases (
    attempt_id TEXT PRIMARY KEY REFERENCES attempt_lease_counters(attempt_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked', 'expired')),
    FOREIGN KEY (attempt_id, generation)
        REFERENCES attempt_lease_generations(attempt_id, generation)
) STRICT;

CREATE TABLE preparation_lease_counters (
    item_id TEXT PRIMARY KEY REFERENCES work_items(item_id) ON DELETE CASCADE,
    generation_high_water INTEGER NOT NULL CHECK (generation_high_water >= 0)
) STRICT;

CREATE TABLE preparation_lease_generations (
    item_id TEXT NOT NULL REFERENCES preparation_lease_counters(item_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    PRIMARY KEY (item_id, generation),
    UNIQUE (item_id, lease_id, generation, task_id, host_id)
) STRICT;

CREATE TABLE preparation_leases (
    item_id TEXT PRIMARY KEY REFERENCES preparation_lease_counters(item_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    definition_digest TEXT NOT NULL CHECK (length(definition_digest) = 64),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked', 'expired')),
    FOREIGN KEY (item_id, generation)
        REFERENCES preparation_lease_generations(item_id, generation),
    FOREIGN KEY (item_id, definition_revision, definition_digest)
        REFERENCES work_item_definition_revisions(item_id, definition_revision, definition_digest)
) STRICT;

CREATE TABLE transition_history (
    history_id INTEGER PRIMARY KEY,
    project_revision INTEGER NOT NULL UNIQUE CHECK (project_revision >= 1),
    action_id TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    artifact_ref_id INTEGER,
    artifact_kind TEXT CHECK (artifact_kind = 'evidence'),
    authorization_kind TEXT NOT NULL,
    actor_task_id TEXT,
    actor_host_id TEXT,
    input_schema TEXT NOT NULL,
    input_json TEXT NOT NULL,
    outcome_schema TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    FOREIGN KEY (artifact_ref_id, artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    CHECK ((artifact_ref_id IS NULL) = (artifact_kind IS NULL))
) STRICT;
