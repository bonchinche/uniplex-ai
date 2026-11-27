BEGIN;

ALTER TABLE mail.attachments
    ADD COLUMN IF NOT EXISTS extracted_text TEXT,
    ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS extraction_error TEXT,
    ADD COLUMN IF NOT EXISTS extraction_method TEXT,
    ADD COLUMN IF NOT EXISTS extraction_version TEXT,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

COMMENT ON COLUMN mail.attachments.storage_key IS
    'Relative path to permanently archived attachment; NULL when not archived';

COMMENT ON COLUMN mail.attachments.extracted_text IS
    'Text extracted from the attachment and stored for knowledge processing';

INSERT INTO mail.schema_migrations (version)
VALUES ('003_attachment_extraction');

COMMIT;
