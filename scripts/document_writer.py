import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


VERSION = "1.0"

ALLOWED_STATUSES = {"processed", "needs_review"}

ALLOWED_SEGMENT_TYPES = {
    "page",
    "cell",
    "paragraph",
    "table",
    "text_block",
    "image_text",
    "other",
}

ALLOWED_REVIEW_STATUSES = {
    "pending",
    "needs_review",
    "accepted",
    "corrected",
    "rejected",
}



def load_env(path="/run/secrets/uniplex.env"):
    env_file = Path(path)

    if not env_file.is_file():
        raise RuntimeError(f"Environment file not found: {path}")

    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key and key not in os.environ:
            os.environ[key] = value

    required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [key for key in required if not os.environ.get(key)]

    if missing:
        raise RuntimeError(
            "Missing required database environment variables: "
            + ", ".join(missing)
        )


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def load_json(path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def validate_payload(payload):
    required = {
        "processor_name",
        "processor_version",
        "status",
        "extraction_method",
        "extracted_text",
        "extracted_structure",
        "quality_metrics",
        "settings",
        "segments",
    }

    missing = required - payload.keys()

    if missing:
        raise ValueError(
            f"Missing payload fields: {sorted(missing)}"
        )

    if payload["status"] not in ALLOWED_STATUSES:
        raise ValueError("Unsupported extraction status")

    if not isinstance(payload["extracted_text"], str):
        raise ValueError("extracted_text must be a string")

    for field in (
        "extracted_structure",
        "quality_metrics",
        "settings",
    ):
        if not isinstance(payload[field], dict):
            raise ValueError(f"{field} must be a JSON object")

    segments = payload["segments"]

    if not isinstance(segments, list):
        raise ValueError("segments must be a list")

    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"Segment {index} must be an object")

        if segment.get("segment_type") not in ALLOWED_SEGMENT_TYPES:
            raise ValueError(f"Invalid segment_type at index {index}")

        if not isinstance(segment.get("text", ""), str):
            raise ValueError(f"Invalid segment text at index {index}")

        for field in ("source_location", "metadata"):
            if not isinstance(segment.get(field, {}), dict):
                raise ValueError(
                    f"Segment {index}: {field} must be an object"
                )

        review_status = segment.get("review_status", "pending")

        if review_status not in ALLOWED_REVIEW_STATUSES:
            raise ValueError(
                f"Invalid review_status at index {index}"
            )

        confidence = segment.get("confidence")

        if confidence is not None:
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 100
            ):
                raise ValueError(
                    f"Invalid confidence at index {index}"
                )

    if not payload["processor_name"] or not payload["processor_version"]:
        raise ValueError("Processor name and version are required")

    if not payload["extraction_method"]:
        raise ValueError("Extraction method is required")

    if not payload["extracted_text"].strip():
        raise ValueError("Extracted text is empty")


def main():
    parser = argparse.ArgumentParser(
        description="UNIPLEX universal document writer"
    )

    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    try:
        attachment_id = str(uuid.UUID(args.attachment_id))
        job_id = str(uuid.UUID(args.job_id)) if args.job_id else None
    except ValueError:
        raise SystemExit("ERROR: invalid UUID")

    source = Path(args.source)
    result_file = Path(args.result)

    if not source.is_file() or not result_file.is_file():
        raise SystemExit("ERROR: source or result file does not exist")

    payload = load_json(result_file)
    validate_payload(payload)

    source_hash = sha256_file(source)

    structure_filename = payload["extracted_structure"].get("filename")

    load_env("/run/secrets/uniplex.env")

    with psycopg.connect(
        host="uniplex-postgres",
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    filename,
                    content_hash,
                    extraction_status
                FROM mail.attachments
                WHERE id = %s::uuid
                FOR UPDATE
                """,
                (attachment_id,),
            )

            attachment = cur.fetchone()

            if attachment is None:
                raise RuntimeError("Attachment not found")

            db_filename, db_hash, db_status = attachment

            if db_hash != source_hash:
                raise RuntimeError("Source SHA256 does not match database")

            if structure_filename and structure_filename != db_filename:
                raise RuntimeError("Document filename mismatch")

            if args.job_id:
                cur.execute(
                    """
                    SELECT id
                    FROM mail.document_extractions
                    WHERE job_id = %s::uuid
                      AND attachment_id = %s::uuid
                    LIMIT 1
                    """,
                    (job_id, attachment_id),
                )

                if cur.fetchone():
                    raise RuntimeError(
                        "Extraction for this job already exists"
                    )

            print("MODE:", "APPLY" if args.apply else "DRY RUN")
            print("Attachment:", attachment_id)
            print("Filename:", db_filename)
            print("SHA256: VERIFIED")
            print("Previous attachment status:", db_status)
            print("Processor:", payload["processor_name"])
            print("Processor version:", payload["processor_version"])
            print("Extraction status:", payload["status"])
            print("Segments:", len(payload["segments"]))

            if not args.apply:
                print("DATABASE WRITE: NONE")
                return

            cur.execute(
                """
                UPDATE mail.document_extractions
                SET is_current = FALSE
                WHERE attachment_id = %s::uuid
                  AND is_current = TRUE
                """,
                (attachment_id,),
            )

            cur.execute(
                """
                INSERT INTO mail.document_extractions (
                    attachment_id,
                    job_id,
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
                    %s::uuid,
                    %s::uuid,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    now(),
                    now()
                )
                RETURNING id
                """,
                (
                    attachment_id,
                    job_id,
                    payload["processor_name"],
                    payload["processor_version"],
                    payload["status"],
                    payload["extraction_method"],
                    source_hash,
                    payload["extracted_text"],
                    Jsonb(payload["extracted_structure"]),
                    Jsonb(payload["quality_metrics"]),
                    Jsonb(payload["settings"]),
                ),
            )

            extraction_id = cur.fetchone()[0]

            segment_rows = []

            for ordinal, segment in enumerate(payload["segments"]):
                segment_rows.append((
                    extraction_id,
                    ordinal,
                    segment["segment_type"],
                    segment.get("text", ""),
                    Jsonb(segment.get("source_location", {})),
                    segment.get("confidence"),
                    segment.get("review_status", "pending"),
                    Jsonb(segment.get("metadata", {})),
                ))

            if segment_rows:
                cur.executemany(
                    """
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
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    segment_rows,
                )

            cur.execute(
                """
                UPDATE mail.attachments
                SET extracted_text = %s,
                    extracted_structure = %s,
                    extraction_method = %s,
                    extraction_version = %s,
                    extracted_at = now(),
                    extraction_error = NULL,
                    extraction_status = %s
                WHERE id = %s::uuid
                  AND content_hash = %s
                """,
                (
                    payload["extracted_text"],
                    Jsonb(payload["extracted_structure"]),
                    payload["extraction_method"],
                    payload["processor_version"],
                    payload["status"],
                    attachment_id,
                    source_hash,
                ),
            )

            if cur.rowcount != 1:
                raise RuntimeError("Attachment update failed")

    print("Extraction ID:", extraction_id)
    print("Segments saved:", len(segment_rows))
    print("DATABASE WRITE: COMMITTED")


if __name__ == "__main__":
    main()
