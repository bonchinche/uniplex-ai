BEGIN;

CREATE TABLE mail.image_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    attachment_id UUID NOT NULL
        REFERENCES mail.attachments(id) ON DELETE CASCADE,

    job_id UUID
        REFERENCES mail.processing_jobs(id) ON DELETE SET NULL,

    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT,
    prompt_version TEXT NOT NULL,

    source_content_hash TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'processing',

    description TEXT,
    analysis_result JSONB,

    quality_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    error_code TEXT,
    error_message TEXT,

    is_current BOOLEAN NOT NULL DEFAULT FALSE,

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT image_analyses_status_check
        CHECK (status IN (
            'processing',
            'processed',
            'needs_review',
            'failed',
            'unsupported'
        )),

    CONSTRAINT image_analyses_result_check
        CHECK (
            analysis_result IS NULL
            OR jsonb_typeof(analysis_result) = 'object'
        ),

    CONSTRAINT image_analyses_quality_check
        CHECK (jsonb_typeof(quality_metrics) = 'object'),

    CONSTRAINT image_analyses_metadata_check
        CHECK (jsonb_typeof(request_metadata) = 'object'),

    CONSTRAINT image_analyses_current_status_check
        CHECK (
            NOT is_current
            OR status IN ('processed', 'needs_review')
        )
);

CREATE UNIQUE INDEX image_analyses_one_current_idx
    ON mail.image_analyses (attachment_id)
    WHERE is_current = TRUE;

CREATE INDEX image_analyses_attachment_history_idx
    ON mail.image_analyses (attachment_id, created_at DESC);

CREATE INDEX image_analyses_job_idx
    ON mail.image_analyses (job_id)
    WHERE job_id IS NOT NULL;

CREATE INDEX image_analyses_status_idx
    ON mail.image_analyses (status, created_at DESC);


CREATE TABLE mail.image_analysis_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    analysis_id UUID NOT NULL
        REFERENCES mail.image_analyses(id) ON DELETE CASCADE,

    ordinal INTEGER NOT NULL,

    item_type TEXT NOT NULL,
    field_key TEXT NOT NULL,

    value JSONB NOT NULL,
    normalized_value JSONB,

    confidence NUMERIC(5,2),

    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,

    review_status TEXT NOT NULL DEFAULT 'pending',

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    corrected_value JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT image_analysis_items_ordinal_check
        CHECK (ordinal >= 0),

    CONSTRAINT image_analysis_items_confidence_check
        CHECK (
            confidence IS NULL
            OR confidence BETWEEN 0 AND 100
        ),

    CONSTRAINT image_analysis_items_review_check
        CHECK (review_status IN (
            'pending',
            'needs_review',
            'accepted',
            'corrected',
            'rejected'
        )),

    CONSTRAINT image_analysis_items_location_check
        CHECK (jsonb_typeof(source_location) = 'object'),

    CONSTRAINT image_analysis_items_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),

    CONSTRAINT image_analysis_items_unique_ordinal
        UNIQUE (analysis_id, ordinal)
);

CREATE INDEX image_analysis_items_analysis_idx
    ON mail.image_analysis_items (analysis_id, ordinal);

CREATE INDEX image_analysis_items_review_idx
    ON mail.image_analysis_items (review_status)
    WHERE review_status IN ('pending', 'needs_review');

CREATE INDEX image_analysis_items_field_idx
    ON mail.image_analysis_items (field_key);


COMMENT ON TABLE mail.image_analyses IS
    'Versioned history of AI image analysis runs';

COMMENT ON COLUMN mail.image_analyses.source_content_hash IS
    'SHA-256 hash of the original image attachment';

COMMENT ON COLUMN mail.image_analyses.is_current IS
    'Marks the currently selected AI analysis for an attachment';

COMMENT ON TABLE mail.image_analysis_items IS
    'Structured facts extracted from images by AI, with provenance and human review';

COMMENT ON COLUMN mail.image_analysis_items.source_location IS
    'JSONB coordinates or region identifying the source of an extracted fact';

COMMENT ON COLUMN mail.image_analysis_items.corrected_value IS
    'Human-corrected value; original AI value remains unchanged';


INSERT INTO mail.schema_migrations (version)
VALUES ('010_image_analyses');

COMMIT;
