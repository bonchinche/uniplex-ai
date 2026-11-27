BEGIN;

-- История запусков извлечения документов.
CREATE TABLE mail.document_extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    attachment_id UUID NOT NULL
        REFERENCES mail.attachments(id) ON DELETE CASCADE,

    job_id UUID
        REFERENCES mail.processing_jobs(id) ON DELETE SET NULL,

    processor_name TEXT NOT NULL,
    processor_version TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'processing',

    extraction_method TEXT,
    source_content_hash TEXT,

    extracted_text TEXT,
    extracted_structure JSONB,

    quality_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,

    error_code TEXT,
    error_message TEXT,

    is_current BOOLEAN NOT NULL DEFAULT FALSE,

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT document_extractions_status_check
        CHECK (status IN (
            'processing',
            'processed',
            'needs_review',
            'failed',
            'unsupported'
        )),

    CONSTRAINT document_extractions_structure_check
        CHECK (
            extracted_structure IS NULL
            OR jsonb_typeof(extracted_structure) = 'object'
        ),

    CONSTRAINT document_extractions_quality_check
        CHECK (jsonb_typeof(quality_metrics) = 'object'),

    CONSTRAINT document_extractions_settings_check
        CHECK (jsonb_typeof(settings) = 'object'),

    CONSTRAINT document_extractions_current_status_check
        CHECK (
            NOT is_current
            OR status IN ('processed', 'needs_review')
        )
);

-- Только один текущий результат на вложение.
CREATE UNIQUE INDEX document_extractions_one_current_idx
    ON mail.document_extractions (attachment_id)
    WHERE is_current = TRUE;

-- История результатов конкретного вложения.
CREATE INDEX document_extractions_attachment_history_idx
    ON mail.document_extractions (attachment_id, created_at DESC);

-- Поиск результатов по задаче Worker.
CREATE INDEX document_extractions_job_idx
    ON mail.document_extractions (job_id)
    WHERE job_id IS NOT NULL;

-- Поиск по статусу обработки.
CREATE INDEX document_extractions_status_idx
    ON mail.document_extractions (status, created_at DESC);

COMMENT ON TABLE mail.document_extractions IS
    'Versioned history of document extraction runs';

COMMENT ON COLUMN mail.document_extractions.source_content_hash IS
    'SHA-256 hash of the original attachment used for this extraction';

COMMENT ON COLUMN mail.document_extractions.is_current IS
    'Marks the currently selected successful extraction for an attachment';


-- Фрагменты извлечённого содержимого с происхождением.
CREATE TABLE mail.document_extraction_segments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    extraction_id UUID NOT NULL
        REFERENCES mail.document_extractions(id) ON DELETE CASCADE,

    ordinal INTEGER NOT NULL,

    segment_type TEXT NOT NULL,

    text TEXT NOT NULL DEFAULT '',
    corrected_text TEXT,

    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC(5,2),

    review_status TEXT NOT NULL DEFAULT 'pending',

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT document_extraction_segments_ordinal_check
        CHECK (ordinal >= 0),

    CONSTRAINT document_extraction_segments_type_check
        CHECK (segment_type IN (
            'page',
            'cell',
            'paragraph',
            'table',
            'text_block',
            'image_text',
            'other'
        )),

    CONSTRAINT document_extraction_segments_confidence_check
        CHECK (
            confidence IS NULL
            OR confidence BETWEEN 0 AND 100
        ),

    CONSTRAINT document_extraction_segments_review_check
        CHECK (review_status IN (
            'pending',
            'needs_review',
            'accepted',
            'corrected',
            'rejected'
        )),

    CONSTRAINT document_extraction_segments_location_check
        CHECK (jsonb_typeof(source_location) = 'object'),

    CONSTRAINT document_extraction_segments_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),

    CONSTRAINT document_extraction_segments_unique_ordinal
        UNIQUE (extraction_id, ordinal)
);

CREATE INDEX document_extraction_segments_extraction_idx
    ON mail.document_extraction_segments (extraction_id, ordinal);

CREATE INDEX document_extraction_segments_review_idx
    ON mail.document_extraction_segments (review_status)
    WHERE review_status IN ('pending', 'needs_review');

COMMENT ON TABLE mail.document_extraction_segments IS
    'Extracted document segments with source locations and review data';

COMMENT ON COLUMN mail.document_extraction_segments.source_location IS
    'JSONB source coordinates such as PDF page and bounding box or XLSX sheet and cell';

COMMENT ON COLUMN mail.document_extraction_segments.corrected_text IS
    'Human-corrected text; original extracted text remains unchanged';


INSERT INTO mail.schema_migrations (version)
VALUES ('009_document_extraction_history');

COMMIT;
