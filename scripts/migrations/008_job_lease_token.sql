BEGIN;

ALTER TABLE mail.processing_jobs
    ADD COLUMN lease_token UUID;

ALTER TABLE mail.processing_jobs
    ADD CONSTRAINT processing_jobs_lease_token_check
    CHECK (
        (
            status = 'running'
            AND lease_token IS NOT NULL
        )
        OR
        (
            status <> 'running'
            AND lease_token IS NULL
        )
    );

COMMENT ON COLUMN mail.processing_jobs.lease_token IS
    'Unique token for the current worker lease; prevents stale workers from updating reclaimed jobs';

INSERT INTO mail.schema_migrations (version)
VALUES ('008_job_lease_token');

COMMIT;
