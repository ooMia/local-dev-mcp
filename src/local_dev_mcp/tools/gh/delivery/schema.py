"""Explicit, opt-in GitHub Delivery PostgreSQL schema statements.

No DDL executes on import or during normal webhook/worker observation.
Keep schema installation and migrations outside receiver and worker.
"""

ENVELOPE_SCHEMA_VERSION = "github-target:1"

RECEIPTS_DDL = """
CREATE TABLE github_event_receipts (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_instance_id text NOT NULL,
    event_key text NOT NULL,
    event_type text NOT NULL,
    event_schema_version text NOT NULL,
    payload_digest text NOT NULL,
    observed_at timestamptz NOT NULL,
    envelope_schema_version text NOT NULL,
    repository text NOT NULL,
    repository_id bigint NOT NULL CHECK (repository_id > 0),
    target_kind text NOT NULL CHECK (target_kind IN ('issue', 'pull_request')),
    target_number integer NOT NULL CHECK (target_number > 0),
    action text NOT NULL,
    target_updated_at text NOT NULL,
    job_id bigint UNIQUE,
    CONSTRAINT github_event_identity UNIQUE (source_instance_id, event_key),
    CONSTRAINT github_job_id_positive CHECK (job_id IS NULL OR job_id > 0)
)
"""

# Existing Stage 1 receipts cannot be backfilled with a trustworthy target:
# EventObservation contained no repository/Issue/PR target. Migration is
# intentionally permitted only on an EMPTY legacy table and is never automatic.
# Apply inside a DB transaction after checking the owning schema.
MIGRATE_EMPTY_V1_TO_V2 = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM github_event_receipts) THEN
        RAISE EXCEPTION 'Existing receipts cannot be reconstructed; migration requires review.';
    END IF;
END
$$;
ALTER TABLE github_event_receipts
    ADD COLUMN envelope_schema_version text NOT NULL,
    ADD COLUMN repository text NOT NULL,
    ADD COLUMN repository_id bigint NOT NULL CHECK (repository_id > 0),
    ADD COLUMN target_kind text NOT NULL CHECK (target_kind IN ('issue', 'pull_request')),
    ADD COLUMN target_number integer NOT NULL CHECK (target_number > 0),
    ADD COLUMN action text NOT NULL,
    ADD COLUMN target_updated_at text NOT NULL;
"""


# Explicit opt-in DDL for the additional, append-only structural plan.
# This table is NOT installed by the receiver, pre-claim probe or plan API.
# No existing receipt rows or Procrastinate schema are altered.
PLANS_DDL = """
CREATE TABLE github_sealed_plans (
    event_id bigint PRIMARY KEY REFERENCES github_event_receipts(event_id),
    structure_version text NOT NULL,
    structure_digest text NOT NULL,
    structure_bytes bytea NOT NULL,
    sealed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT github_plan_digest_format
        CHECK (structure_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_plan_structure_nonempty CHECK (octet_length(structure_bytes) > 0)
);
CREATE FUNCTION github_reject_sealed_plan_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'sealed plan is append-only';
END
$$;
CREATE TRIGGER github_sealed_plan_immutable
BEFORE UPDATE OR DELETE ON github_sealed_plans
FOR EACH ROW EXECUTE FUNCTION github_reject_sealed_plan_change();
"""


# Explicit additive schema for append-only local Delivery Claim Generations.
# It does not alter Procrastinate's schema or grant a worker execution rights.
CLAIM_GENERATIONS_DDL = """
CREATE TABLE github_delivery_claim_generations (
    event_id bigint NOT NULL REFERENCES github_event_receipts(event_id),
    generation bigint NOT NULL CHECK (generation > 0),
    job_id bigint NOT NULL CHECK (job_id > 0),
    started_event_id bigint NOT NULL CHECK (started_event_id > 0),
    worker_id bigint NOT NULL CHECK (worker_id > 0),
    attempts integer NOT NULL CHECK (attempts >= 0),
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, generation),
    CONSTRAINT github_claim_started_unique UNIQUE (event_id, started_event_id)
);
CREATE FUNCTION github_reject_claim_generation_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'delivery claim generations are append-only';
END
$$;
CREATE TRIGGER github_claim_generation_immutable
BEFORE UPDATE OR DELETE ON github_delivery_claim_generations
FOR EACH ROW EXECUTE FUNCTION github_reject_claim_generation_change();
"""

# Explicit opt-in append-only Intent evidence. Identity is event + Operation,
# never Claim Generation; the generation FK is origin evidence, not identity.
# No automatic DDL execution, existing schema mutation, or remote side effects.
INTENTS_DDL = """
CREATE TABLE github_operation_intents (
    event_id bigint NOT NULL REFERENCES github_event_receipts(event_id),
    operation_id text NOT NULL,
    created_generation bigint NOT NULL CHECK (created_generation > 0),
    structure_digest text NOT NULL,
    intent_version text NOT NULL,
    intent_digest text NOT NULL,
    intent_bytes bytea NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, operation_id),
    CONSTRAINT github_intent_generation_fk
        FOREIGN KEY (event_id, created_generation)
        REFERENCES github_delivery_claim_generations(event_id, generation),
    CONSTRAINT github_intent_structure_digest CHECK (structure_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_intent_digest_format CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_intent_nonempty CHECK (octet_length(intent_bytes) > 0)
);
CREATE FUNCTION github_reject_operation_intent_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operation intent is append-only';
END
$$;
CREATE TRIGGER github_operation_intent_immutable
BEFORE UPDATE OR DELETE ON github_operation_intents
FOR EACH ROW EXECUTE FUNCTION github_reject_operation_intent_change();
"""

# Explicit opt-in Q71/Q73 evidence. Separate from remote execution.
# Checkpoint output is one immutable result per event/operation. Lineage has
# one immutable record per dependent Intent, committed in the same transaction.
CHECKPOINTS_DDL = """
CREATE TABLE github_operation_checkpoints (
    event_id bigint NOT NULL,
    operation_id text NOT NULL,
    recorded_generation bigint NOT NULL CHECK (recorded_generation > 0),
    intent_digest text NOT NULL,
    checkpoint_version text NOT NULL,
    checkpoint_digest text NOT NULL,
    checkpoint_bytes bytea NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, operation_id),
    CONSTRAINT github_checkpoint_intent_fk
        FOREIGN KEY (event_id, operation_id)
        REFERENCES github_operation_intents(event_id, operation_id),
    CONSTRAINT github_checkpoint_generation_fk
        FOREIGN KEY (event_id, recorded_generation)
        REFERENCES github_delivery_claim_generations(event_id, generation),
    CONSTRAINT github_checkpoint_intent_digest_format
        CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_checkpoint_digest_format
        CHECK (checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_checkpoint_nonempty CHECK (octet_length(checkpoint_bytes) > 0)
);
CREATE FUNCTION github_reject_operation_checkpoint_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operation checkpoint is append-only';
END
$$;
CREATE TRIGGER github_operation_checkpoint_immutable
BEFORE UPDATE OR DELETE ON github_operation_checkpoints
FOR EACH ROW EXECUTE FUNCTION github_reject_operation_checkpoint_change();

CREATE TABLE github_operation_lineages (
    event_id bigint NOT NULL,
    operation_id text NOT NULL,
    lineage_version text NOT NULL,
    lineage_digest text NOT NULL,
    lineage_bytes bytea NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, operation_id),
    CONSTRAINT github_lineage_intent_fk
        FOREIGN KEY (event_id, operation_id)
        REFERENCES github_operation_intents(event_id, operation_id),
    CONSTRAINT github_lineage_digest_format
        CHECK (lineage_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_lineage_nonempty CHECK (octet_length(lineage_bytes) > 0)
);
CREATE FUNCTION github_reject_operation_lineage_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operation lineage is append-only';
END
$$;
CREATE TRIGGER github_operation_lineage_immutable
BEFORE UPDATE OR DELETE ON github_operation_lineages
FOR EACH ROW EXECUTE FUNCTION github_reject_operation_lineage_change();
"""


# Q79=A opt-in immutable pre-POST Attempt evidence. One possible remote write
# per event/operation across all Claim Generations. Commit precedes the HTTP
# request. An Attempt is NOT proof a request was sent or remotely accepted.
# No DDL executes automatically.
ATTEMPTS_DDL = """
CREATE TABLE github_operation_attempts (
    event_id bigint NOT NULL,
    operation_id text NOT NULL,
    origin_generation bigint NOT NULL CHECK (origin_generation > 0),
    intent_digest text NOT NULL,
    attempt_version text NOT NULL,
    attempt_digest text NOT NULL,
    attempt_bytes bytea NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, operation_id),
    CONSTRAINT github_attempt_intent_fk
        FOREIGN KEY (event_id, operation_id)
        REFERENCES github_operation_intents(event_id, operation_id),
    CONSTRAINT github_attempt_generation_fk
        FOREIGN KEY (event_id, origin_generation)
        REFERENCES github_delivery_claim_generations(event_id, generation),
    CONSTRAINT github_attempt_intent_digest_format
        CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_attempt_digest_format
        CHECK (attempt_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT github_attempt_nonempty CHECK (octet_length(attempt_bytes) > 0)
);
CREATE FUNCTION github_reject_operation_attempt_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operation attempts are append-only';
END
$$;
CREATE TRIGGER github_operation_attempt_immutable
BEFORE UPDATE OR DELETE ON github_operation_attempts
FOR EACH ROW EXECUTE FUNCTION github_reject_operation_attempt_change();
"""
