BEGIN;

-- Связь повторного появления письма с первоначальной записью
ALTER TABLE mail.emails
ADD COLUMN duplicate_of_email_id UUID
REFERENCES mail.emails(id);

ALTER TABLE mail.emails
ADD CONSTRAINT emails_no_self_duplicate
CHECK (
    duplicate_of_email_id IS NULL
    OR duplicate_of_email_id <> id
);

-- Идентификаторы и контрольные суммы писем
CREATE TABLE mail.email_identity_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    email_id UUID NOT NULL
        REFERENCES mail.emails(id) ON DELETE CASCADE,

    identity_type TEXT NOT NULL
        CHECK (identity_type IN ('message_id', 'raw_sha256')),

    identity_value TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (email_id, identity_type, identity_value)
);

-- Message-ID может встречаться у нескольких разных писем
CREATE INDEX email_identity_message_id_idx
ON mail.email_identity_keys (identity_value)
WHERE identity_type = 'message_id';

-- Одинаковый SHA-256 исходного письма регистрируется один раз
CREATE UNIQUE INDEX email_identity_raw_sha256_idx
ON mail.email_identity_keys (identity_value)
WHERE identity_type = 'raw_sha256';

INSERT INTO mail.schema_migrations (version)
VALUES ('002_email_identity');

COMMIT;
