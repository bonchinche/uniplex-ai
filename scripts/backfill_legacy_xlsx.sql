BEGIN;

DO $$
DECLARE
    v_attachment_id UUID :=
        'df9fd007-8ea0-4c81-ae3d-4e3c22b7b47a';
    v_extraction_id UUID;
    v_attachment RECORD;
    v_segment_count INTEGER;
BEGIN
    SELECT
        id,
        filename,
        content_hash,
        extracted_text,
        extracted_structure,
        extraction_status,
        extraction_method,
        extraction_version,
        extracted_at
    INTO v_attachment
    FROM mail.attachments
    WHERE id = v_attachment_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Source attachment not found';
    END IF;

    IF v_attachment.filename <> 'КП_Униплекс.xlsx'
       OR v_attachment.extraction_status <> 'processed'
       OR v_attachment.extraction_method <> 'openpyxl'
       OR v_attachment.extraction_version <> '1.2'
       OR v_attachment.content_hash IS NULL
       OR v_attachment.extracted_text IS NULL
       OR v_attachment.extracted_structure IS NULL
    THEN
        RAISE EXCEPTION 'Source attachment validation failed';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM mail.document_extractions
        WHERE attachment_id = v_attachment_id
    ) THEN
        RAISE EXCEPTION
            'Extraction history already exists; refusing duplicate backfill';
    END IF;

    INSERT INTO mail.document_extractions (
        attachment_id,
        processor_name,
        processor_version,
        status,
        extraction_method,
        source_content_hash,
        extracted_text,
        extracted_structure,
        quality_metrics,
        settings,
        is_current,
        started_at,
        finished_at
    )
    VALUES (
        v_attachment_id,
        'uniplex-document-processor',
        '1.2',
        'processed',
        'openpyxl',
        v_attachment.content_hash,
        v_attachment.extracted_text,
        v_attachment.extracted_structure,
        jsonb_build_object(
            'legacy_backfill', TRUE,
            'sheet_count',
                jsonb_array_length(
                    v_attachment.extracted_structure->'sheets'
                ),
            'cell_count',
                (
                    SELECT COUNT(*)
                    FROM jsonb_array_elements(
                        v_attachment.extracted_structure->'sheets'
                    ) AS s(sheet_data)
                    CROSS JOIN LATERAL
                    jsonb_array_elements(s.sheet_data->'cells') AS c(cell_data)
                )
        ),
        jsonb_build_object(
            'backfilled_from', 'mail.attachments',
            'original_extraction_method', 'openpyxl',
            'original_extraction_version', '1.2'
        ),
        TRUE,
        v_attachment.extracted_at,
        v_attachment.extracted_at
    )
    RETURNING id INTO v_extraction_id;

    INSERT INTO mail.document_extraction_segments (
        extraction_id,
        ordinal,
        segment_type,
        text,
        source_location,
        confidence,
        review_status,
        metadata
    )
    SELECT
        v_extraction_id,
        (
            ROW_NUMBER() OVER (
                ORDER BY sheet_order, cell_order
            ) - 1
        )::INTEGER,
        'cell',
        COALESCE(cell_data->>'value', ''),
        jsonb_build_object(
            'sheet', sheet_data->>'name',
            'coordinate', cell_data->>'coordinate',
            'row', cell_data->'row',
            'column', cell_data->'column'
        ),
        NULL,
        'pending',
        cell_data
    FROM jsonb_array_elements(
        v_attachment.extracted_structure->'sheets'
    ) WITH ORDINALITY AS sheets(sheet_data, sheet_order)
    CROSS JOIN LATERAL
        jsonb_array_elements(
            sheets.sheet_data->'cells'
        ) WITH ORDINALITY AS cells(cell_data, cell_order);

    GET DIAGNOSTICS v_segment_count = ROW_COUNT;

    IF v_segment_count <> 232 THEN
        RAISE EXCEPTION
            'Expected 232 segments, created %', v_segment_count;
    END IF;

    RAISE NOTICE
        'Backfill prepared: extraction %, segments %',
        v_extraction_id,
        v_segment_count;
END $$;

COMMIT;
