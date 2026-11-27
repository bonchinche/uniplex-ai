from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from image_ai.contracts import validate_output
from image_ai.provider_base import ProviderResponse


class ImageWriterError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(value: Any, name: str, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ImageWriterError(f"Invalid {name}")
    return value.strip()


def _validate_response(
    response: ProviderResponse,
    prompt_version: str,
    schema_path: str | Path,
) -> tuple[dict[str, Any], str]:
    if not isinstance(response, ProviderResponse):
        raise ImageWriterError("response must be ProviderResponse")

    _required_text(response.provider_name, "provider_name", 128)
    _required_text(response.model_name, "model_name", 256)
    _required_text(prompt_version, "prompt_version", 128)

    if response.model_version is not None:
        _required_text(response.model_version, "model_version", 128)

    if not isinstance(response.output, dict):
        raise ImageWriterError("Provider output must be an object")

    validate_output(response.output, schema_path)

    if not isinstance(response.request_metadata, dict):
        raise ImageWriterError("request_metadata must be an object")
    if not isinstance(response.usage, dict):
        raise ImageWriterError("usage must be an object")

    if response.latency_ms is not None:
        if type(response.latency_ms) is not int or response.latency_ms < 0:
            raise ImageWriterError("latency_ms must be a non-negative integer")

    try:
        json.dumps(response.request_metadata, ensure_ascii=False, allow_nan=False)
        json.dumps(response.usage, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ImageWriterError("Provider metadata is not valid JSON") from exc

    status = (
        "needs_review"
        if response.output["overall_review"]["recommendation"] == "needs_review"
        else "processed"
    )
    return response.output, status


def write_analysis(
    conn,
    job: dict[str, Any],
    worker_id: str,
    source_path: str | Path,
    response: ProviderResponse,
    prompt_version: str,
    schema_path: str | Path = "/opt/uniplex/config/image_analysis_contract_v1.json",
) -> str:
    worker_id = _required_text(worker_id, "worker_id", 256)
    if not isinstance(job, dict):
        raise ImageWriterError("job must be an object")

    job_id = _required_text(str(job.get("id", "")), "job.id", 64)
    attachment_id = _required_text(
        str(job.get("attachment_id", "")), "job.attachment_id", 64
    )
    lease_token = _required_text(
        str(job.get("lease_token", "")), "job.lease_token", 64
    )
    for name, value in (
        ("job.id", job_id),
        ("job.attachment_id", attachment_id),
        ("job.lease_token", lease_token),
    ):
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ImageWriterError(f"Invalid {name}") from exc

    output, analysis_status = _validate_response(
        response, prompt_version, schema_path
    )

    source_hash = sha256_file(source_path)
    request_metadata = {
        **response.request_metadata,
        "usage": response.usage,
        "latency_ms": response.latency_ms,
    }
    quality_metrics = {
        "document_classification_confidence":
            output["document_classification"]["confidence"],
        "fact_count": len(output["facts"]),
        "overall_review": output["overall_review"]["recommendation"],
    }

    try:
        json.dumps(request_metadata, ensure_ascii=False, allow_nan=False)
        json.dumps(quality_metrics, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ImageWriterError("Analysis metadata is not valid JSON") from exc

    analysis_id = None
    saved_facts = 0

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM mail.processing_jobs
                WHERE id = %s::uuid
                  AND job_type = 'analyze_image'
                  AND attachment_id = %s::uuid
                  AND status = 'running'
                  AND worker_id = %s
                  AND lease_token = %s::uuid
                  AND lease_expires_at > clock_timestamp()
                FOR UPDATE
                """,
                (job_id, attachment_id, worker_id, lease_token),
            )
            if cur.fetchone() is None:
                raise ImageWriterError("Job lease is invalid or expired")

            cur.execute(
                """
                SELECT content_hash
                FROM mail.attachments
                WHERE id = %s::uuid
                FOR UPDATE
                """,
                (attachment_id,),
            )
            attachment = cur.fetchone()
            if attachment is None:
                raise ImageWriterError("Attachment does not exist")

            if attachment[0] != source_hash:
                raise ImageWriterError("Source image SHA-256 mismatch")

            cur.execute(
                """
                SELECT id
                FROM mail.image_analyses
                WHERE job_id = %s::uuid
                LIMIT 1
                """,
                (job_id,),
            )
            if cur.fetchone() is not None:
                raise ImageWriterError("Analysis already exists for this job")

            cur.execute(
                """
                UPDATE mail.image_analyses
                SET is_current = FALSE
                WHERE attachment_id = %s::uuid
                  AND is_current = TRUE
                """,
                (attachment_id,),
            )

            cur.execute(
                """
                INSERT INTO mail.image_analyses (
                    attachment_id,
                    job_id,
                    model_provider,
                    model_name,
                    model_version,
                    prompt_version,
                    source_content_hash,
                    status,
                    description,
                    analysis_result,
                    quality_metrics,
                    request_metadata,
                    is_current,
                    finished_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, TRUE, clock_timestamp()
                )
                RETURNING id
                """,
                (
                    attachment_id,
                    job_id,
                    response.provider_name,
                    response.model_name,
                    response.model_version,
                    prompt_version,
                    source_hash,
                    analysis_status,
                    output["image_summary"],
                    Jsonb(output),
                    Jsonb(quality_metrics),
                    Jsonb(request_metadata),
                ),
            )
            analysis_id = cur.fetchone()[0]

            fact_rows = []
            for ordinal, fact in enumerate(output["facts"]):
                fact_rows.append((
                    analysis_id,
                    ordinal,
                    fact["item_type"],
                    fact["field_key"],
                    Jsonb(fact["value"]),
                    (
                        Jsonb(fact["normalized_value"])
                        if "normalized_value" in fact else None
                    ),
                    fact["confidence"],
                    Jsonb(fact["source_location"]),
                    fact["review_recommendation"],
                    Jsonb(fact.get("metadata", {})),
                ))

            if fact_rows:
                cur.executemany(
                    """
                    INSERT INTO mail.image_analysis_items (
                        analysis_id, ordinal, item_type, field_key,
                        value, normalized_value, confidence,
                        source_location, review_status, metadata
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    fact_rows,
                )
                saved_facts = len(fact_rows)

            cur.execute(
                """
                UPDATE mail.processing_jobs
                SET status = 'completed',
                    lease_expires_at = NULL,
                    lease_token = NULL,
                    finished_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND job_type = 'analyze_image'
                  AND attachment_id = %s::uuid
                  AND status = 'running'
                  AND worker_id = %s
                  AND lease_token = %s::uuid
                  AND lease_expires_at > clock_timestamp()
                """,
                (job_id, attachment_id, worker_id, lease_token),
            )
            if cur.rowcount != 1:
                raise ImageWriterError("Could not complete processing job")

    return str(analysis_id)
