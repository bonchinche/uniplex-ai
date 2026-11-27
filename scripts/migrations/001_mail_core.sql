BEGIN;

CREATE SCHEMA mail;

CREATE TABLE mail.mail_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL DEFAULT 'yandex',
    email_address TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, email_address)
);

CREATE TABLE mail.mail_folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES mail.mail_accounts(id),
    folder_path TEXT NOT NULL,
    folder_name TEXT,
    uid_validity BIGINT,
    highest_uid BIGINT NOT NULL DEFAULT 0,
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, folder_path)
);

CREATE TABLE mail.emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    folder_id UUID NOT NULL REFERENCES mail.mail_folders(id),
    uid_validity BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    message_id TEXT,
    in_reply_to TEXT,
    references_header TEXT[],
    subject TEXT,
    sender JSONB,
    recipients JSONB,
    sent_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ,
    size_bytes BIGINT,
    raw_headers JSONB,
    body_text TEXT,
    body_html TEXT,
    content_hash TEXT,
    processing_status TEXT NOT NULL DEFAULT 'new'
        CHECK (processing_status IN (
            'new', 'processing', 'processed',
            'needs_review', 'failed'
        )),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (folder_id, uid_validity, uid)
);

CREATE INDEX emails_message_id_idx
    ON mail.emails (message_id);

CREATE INDEX emails_received_at_idx
    ON mail.emails (received_at DESC);

CREATE INDEX emails_processing_status_idx
    ON mail.emails (processing_status);

CREATE TABLE mail.attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id UUID NOT NULL REFERENCES mail.emails(id) ON DELETE CASCADE,
    filename TEXT,
    content_type TEXT,
    size_bytes BIGINT,
    content_hash TEXT,
    mime_part_id TEXT,
    storage_key TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN (
            'pending', 'processing', 'processed',
            'needs_review', 'failed', 'unsupported'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (email_id, mime_part_id)
);

CREATE INDEX attachments_email_id_idx
    ON mail.attachments (email_id);

CREATE TABLE mail.processing_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id UUID REFERENCES mail.emails(id) ON DELETE CASCADE,
    attachment_id UUID REFERENCES mail.attachments(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'running', 'completed',
            'failed', 'cancelled'
        )),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (email_id IS NOT NULL OR attachment_id IS NOT NULL)
);

CREATE INDEX processing_jobs_queue_idx
    ON mail.processing_jobs (status, scheduled_at);

CREATE INDEX processing_jobs_email_idx
    ON mail.processing_jobs (email_id);

CREATE TABLE mail.schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO mail.schema_migrations (version)
VALUES ('001_mail_core');

COMMIT;
