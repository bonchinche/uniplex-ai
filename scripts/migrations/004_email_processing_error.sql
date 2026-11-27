BEGIN;

ALTER TABLE mail.emails
    ADD COLUMN IF NOT EXISTS processing_error TEXT;

COMMENT ON COLUMN mail.emails.processing_error IS
    'Reason why email processing requires review or failed';

INSERT INTO mail.schema_migrations (version)
VALUES ('004_email_processing_error');

COMMIT;
