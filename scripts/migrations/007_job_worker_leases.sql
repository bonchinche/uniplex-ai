BEGIN;

ALTER TABLE mail.processing_jobs
    ADD COLUMN worker_id TEXT,
    ADD COLUMN heartbeat_at TIMESTAMPTZ,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN error_code TEXT,
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD CONSTRAINT processing_jobs_lease_state_check
        CHECK (
            (
                status = 'running'
                AND worker_id IS NOT NULL
                AND lease_expires_at IS NOT NULL
            )
            OR
            (
                status <> 'running'
                AND lease_expires_at IS NULL
            )
        );

CREATE INDEX processing_jobs_ready_idx
    ON mail.processing_jobs (scheduled_at, id)
    WHERE status = 'queued';

CREATE INDEX processing_jobs_expired_lease_idx
    ON mail.processing_jobs (lease_expires_at)
    WHERE status = 'running';

COMMENT ON COLUMN mail.processing_jobs.worker_id IS
    'Identifier of the worker processing or last processing this job';

COMMENT ON COLUMN mail.processing_jobs.heartbeat_at IS
    'Timestamp of the last worker heartbeat';

COMMENT ON COLUMN mail.processing_jobs.lease_expires_at IS
    'Expiration time of the active worker lease';

COMMENT ON COLUMN mail.processing_jobs.error_code IS
    'Machine-readable processing error code';

INSERT INTO mail.schema_migrations (version)
VALUES ('007_job_worker_leases');

COMMIT;
