BEGIN;

ALTER TABLE mail.attachments
    ADD COLUMN content_disposition TEXT,
    ADD COLUMN content_id TEXT;

CREATE UNIQUE INDEX processing_jobs_active_email_unique
    ON mail.processing_jobs (email_id, job_type)
    WHERE email_id IS NOT NULL
      AND status IN ('queued', 'running');

CREATE UNIQUE INDEX processing_jobs_active_attachment_unique
    ON mail.processing_jobs (attachment_id, job_type)
    WHERE attachment_id IS NOT NULL
      AND status IN ('queued', 'running');

INSERT INTO mail.schema_migrations (version)
VALUES ('005_attachment_metadata_and_job_dedup');

COMMIT;
