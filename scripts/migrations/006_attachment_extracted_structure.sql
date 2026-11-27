BEGIN;

ALTER TABLE mail.attachments
    ADD COLUMN extracted_structure JSONB,
    ADD CONSTRAINT attachments_extracted_structure_object_check
        CHECK (
            extracted_structure IS NULL
            OR jsonb_typeof(extracted_structure) = 'object'
        );

COMMENT ON COLUMN mail.attachments.extracted_structure IS
    'Structured extraction result, including document sheets, cells and metadata';

COMMENT ON COLUMN mail.attachments.storage_key IS
    'Relative path to attachment in temporary or permanent storage';

INSERT INTO mail.schema_migrations (version)
VALUES ('006_attachment_extracted_structure');

COMMIT;
