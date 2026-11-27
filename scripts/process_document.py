#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

from pathlib import Path
from uuid import UUID

import psycopg

from extract_pdf import extract_pdf
from extract_xlsx import extract_xlsx, build_extracted_text
from prepare_pdf_payload import (
    prepare_payload as prepare_pdf_payload
)
from prepare_xlsx_payload import (
    prepare_payload as prepare_xlsx_payload
)


DB_ENV = Path("/run/secrets/uniplex.env")
SOURCE_DIR = Path("/data/mail/tmp")
WRITER = Path("/app/document_writer.py")

MAX_FILE_BYTES = 25 * 1024 * 1024

PDF_MIME = "application/pdf"
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)

STORAGE_KEY_PATTERN = re.compile(
    r"^tmp/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.bin$"
)


def load_env(path="/run/secrets/uniplex.env"):
    env_file = Path(path)

    if not env_file.is_file():
        raise RuntimeError("Database environment file not found")

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


def connect_db():
    return psycopg.connect(
        host="uniplex-postgres",
        port=5432,
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )


def validate_uuid(value, name):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"Invalid {name}")

    if str(parsed) != value.lower():
        raise ValueError(f"Non-canonical {name}")

    return str(parsed)


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def get_attachment(attachment_id, job_id, lease_token):
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    filename,
                    content_type,
                    size_bytes,
                    storage_key,
                    content_hash,
                    extraction_status
                FROM mail.attachments
                WHERE id = %s::uuid
                """,
                (attachment_id,)
            )
            attachment = cur.fetchone()

            if attachment is None:
                raise RuntimeError("Attachment not found")

            cur.execute(
                """
                SELECT id
                FROM mail.processing_jobs
                WHERE id = %s::uuid
                  AND attachment_id = %s::uuid
                  AND job_type = 'extract_document'
                  AND status = 'running'
                  AND lease_token = %s::uuid
                  AND lease_expires_at > clock_timestamp()
                """,
                (job_id, attachment_id, lease_token)
            )

            if cur.fetchone() is None:
                raise RuntimeError("Document job lease is not valid")

    return attachment


def resolve_source(storage_key):
    match = STORAGE_KEY_PATTERN.fullmatch(storage_key or "")

    if not match:
        raise ValueError("Unsafe or unsupported storage_key")

    file_uuid = UUID(match.group(1))

    if str(file_uuid) != match.group(1).lower():
        raise ValueError("Non-canonical storage UUID")

    base = SOURCE_DIR.resolve(strict=True)
    source = base / f"{file_uuid}.bin"

    if source.is_symlink() or not source.is_file():
        raise ValueError("Source file is missing or is a symlink")

    resolved = source.resolve(strict=True)

    if resolved.parent != base:
        raise ValueError("Source path escapes storage directory")

    return resolved


def process_document(attachment_id, job_id, lease_token):
    (
        filename,
        content_type,
        expected_size,
        storage_key,
        expected_hash,
        extraction_status,
    ) = get_attachment(
        attachment_id, job_id, lease_token
    )

    if extraction_status != "pending":
        raise RuntimeError(
            f"Attachment status is {extraction_status!r}; "
            "expected 'pending'"
        )

    if not filename or not expected_hash:
        raise ValueError("Attachment metadata is incomplete")

    source = resolve_source(storage_key)

    actual_size = source.stat().st_size

    if actual_size != expected_size:
        raise ValueError("Source file size does not match database")

    if actual_size <= 0 or actual_size > MAX_FILE_BYTES:
        raise ValueError("Document size is outside supported limits")

    actual_hash = sha256_file(source)

    if actual_hash.lower() != expected_hash.lower():
        raise ValueError("Source SHA256 does not match database")

    extension = Path(filename).suffix.lower()

    if content_type == PDF_MIME and extension == ".pdf":
        print("DOCUMENT TYPE: PDF", flush=True)

        result = extract_pdf(
            str(source),
            language="rus+eng",
            psm=3,
            dpi=300,
        )

        result["filename"] = filename
        payload = prepare_pdf_payload(result)

    elif content_type == XLSX_MIME and extension == ".xlsx":
        print("DOCUMENT TYPE: XLSX", flush=True)

        result = extract_xlsx(source)
        result["filename"] = filename
        result["extracted_text"] = build_extracted_text(result)
        payload = prepare_xlsx_payload(result)

    else:
        raise ValueError(
            "Unsupported or mismatched MIME type and extension"
        )

    with tempfile.TemporaryDirectory(
        prefix="uniplex-document-",
        dir="/tmp",
    ) as temp_dir:
        result_path = Path(temp_dir) / "payload.json"

        with result_path.open(
            "w", encoding="utf-8"
        ) as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                allow_nan=False,
            )

        command = [
            sys.executable,
            str(WRITER),
            "--attachment-id", attachment_id,
            "--source", str(source),
            "--result", str(result_path),
            "--job-id", job_id,
            "--lease-token", lease_token,
            "--apply",
        ]

        print("DOCUMENT WRITER: START", flush=True)

        subprocess.run(command, check=True)

    print("DOCUMENT PROCESSING: SUCCESS", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="UNIPLEX document processor"
    )
    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    if not args.apply:
        parser.error("Document queue processing requires --apply")

    attachment_id = validate_uuid(
        args.attachment_id, "attachment-id"
    )
    job_id = validate_uuid(args.job_id, "job-id")
    lease_token = validate_uuid(
        args.lease_token, "lease-token"
    )

    try:
        load_env()
        process_document(
            attachment_id,
            job_id,
            lease_token,
        )
        return 0

    except Exception as error:
        print(
            f"DOCUMENT PROCESSING ERROR: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
