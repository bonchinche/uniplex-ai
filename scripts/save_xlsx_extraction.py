import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()

ATTACHMENT_ID = "df9fd007-8ea0-4c81-ae3d-4e3c22b7b47a"
EXPECTED_HASH = "6da678f36b21d46a655bf10735dd4cb6110ef745f9706644f1c9067979129aac"
EXPECTED_FILENAME = "КП_Униплекс.xlsx"

SOURCE = Path("/input/source.xlsx")
STRUCTURE_FILE = Path("/input/structure.json")
TEXT_FILE = Path("/input/extracted.txt")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


source_hash = sha256_file(SOURCE)
if source_hash != EXPECTED_HASH:
    raise SystemExit("ERROR: source SHA256 mismatch")

structure = json.loads(STRUCTURE_FILE.read_text(encoding="utf-8"))
extracted_text = TEXT_FILE.read_text(encoding="utf-8")

if structure.get("filename") != EXPECTED_FILENAME:
    raise SystemExit("ERROR: filename mismatch")

if structure.get("format") != "xlsx":
    raise SystemExit("ERROR: unexpected document format")

if len(structure.get("sheets", [])) != 3:
    raise SystemExit("ERROR: expected exactly 3 sheets")

if "extracted_text" in structure:
    raise SystemExit("ERROR: text must be stored separately")

if not extracted_text.strip():
    raise SystemExit("ERROR: extracted text is empty")

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
            SELECT content_hash, filename, extraction_status
            FROM mail.attachments
            WHERE id = %s
            FOR UPDATE
            """,
            (ATTACHMENT_ID,),
        )
        row = cur.fetchone()

        if row is None:
            raise RuntimeError("Attachment not found")

        db_hash, db_filename, db_status = row

        if db_hash != EXPECTED_HASH:
            raise RuntimeError("Database SHA256 mismatch")

        if db_filename != EXPECTED_FILENAME:
            raise RuntimeError("Database filename mismatch")

        if db_status != "pending":
            raise RuntimeError(
                f"Expected pending status, got {db_status!r}"
            )

        if args.apply:
            cur.execute(
                """
                UPDATE mail.attachments
                SET extracted_text = %s,
                    extracted_structure = %s,
                    extraction_method = %s,
                    extraction_version = %s,
                    extracted_at = now(),
                    extraction_error = NULL,
                    extraction_status = 'processed'
                WHERE id = %s
                  AND content_hash = %s
                  AND extraction_status = 'pending'
                """,
                (
                    extracted_text,
                    Jsonb(structure),
                    "openpyxl",
                    "1.2",
                    ATTACHMENT_ID,
                    EXPECTED_HASH,
                ),
            )

            if cur.rowcount != 1:
                raise RuntimeError("Expected exactly one updated row")

print("MODE:", "APPLY" if args.apply else "DRY RUN")
print("DATABASE WRITE:", "COMMITTED" if args.apply else "NONE")
print("Attachment:", ATTACHMENT_ID)
print("Filename:", EXPECTED_FILENAME)
print("SHA256: VERIFIED")
print("Sheets:", len(structure["sheets"]))
print("Extracted text characters:", len(extracted_text))
print("Extraction version: 1.2")
print("Status: processed")
