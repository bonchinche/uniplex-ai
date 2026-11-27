import uuid
from pathlib import Path

import psycopg

from image_ai.provider_base import ProviderResponse
from image_ai.writer import ImageWriterError, write_analysis

ATTACHMENT = "28b125d9-1dbd-4f05-936a-eff746484aa4"
WORKER = "writer-failure-test"
IMAGE = "/data/test-image.png"
SCHEMA = "/config/image_analysis_contract_v1.json"


class RollbackCase(Exception):
    pass


def load_env():
    result = {}
    for line in Path("/run/secrets/uniplex.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip("'\"")
    return result


def make_response():
    return ProviderResponse(
        provider_name="mock",
        model_name="failure-test",
        model_version="1.0",
        output={
            "contract_version": "1.0",
            "image_summary": "TEST ONLY",
            "detected_language": "unknown",
            "document_classification": {
                "category": "test_fixture",
                "confidence": 99,
                "alternatives": [],
            },
            "facts": [],
            "overall_review": {
                "recommendation": "needs_review",
                "reasons": ["integration_test"],
            },
            "warnings": ["TEST ONLY"],
        },
    )


env = load_env()
conn = psycopg.connect(
    host="uniplex-postgres",
    dbname="uniplex_migration_test",
    user=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"],
    autocommit=True,
)


def run_rejection(name, expected, mode):
    job_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    job = {
        "id": job_id,
        "attachment_id": ATTACHMENT,
        "lease_token": token,
    }

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                expiry = (
                    "clock_timestamp() - interval '1 minute'"
                    if mode == "expired" else
                    "clock_timestamp() + interval '5 minutes'"
                )
                cur.execute(
                    f"""
                    INSERT INTO mail.processing_jobs (
                        id, attachment_id, job_type, status, attempts,
                        max_attempts, worker_id, heartbeat_at,
                        lease_expires_at, lease_token, started_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, 'analyze_image', 'running', 1, 3,
                        %s, clock_timestamp(), {expiry},
                        %s::uuid, clock_timestamp()
                    )
                    """,
                    (job_id, ATTACHMENT, WORKER, token),
                )

            source = IMAGE
            if mode == "wrong_hash":
                source = "/tmp/wrong-image.png"
                Path(source).write_bytes(b"not the original image")

            if mode == "wrong_token":
                job["lease_token"] = str(uuid.uuid4())

            if mode == "duplicate":
                write_analysis(
                    conn, job, WORKER, source, make_response(),
                    "failure-test", SCHEMA,
                )
                new_token = str(uuid.uuid4())
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE mail.processing_jobs
                        SET status = 'running',
                            lease_token = %s::uuid,
                            lease_expires_at =
                                clock_timestamp() + interval '5 minutes',
                            finished_at = NULL
                        WHERE id = %s::uuid
                        """,
                        (new_token, job_id),
                    )
                job["lease_token"] = new_token

            try:
                write_analysis(
                    conn, job, WORKER, source, make_response(),
                    "failure-test", SCHEMA,
                )
            except ImageWriterError as exc:
                assert expected in str(exc), (
                    f"{name}: unexpected error: {exc}"
                )
                print(f"{name}: REJECTED AS EXPECTED")
            else:
                raise AssertionError(f"{name}: invalid write was accepted")

            raise RollbackCase()

    except RollbackCase:
        pass

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM mail.processing_jobs WHERE id=%s::uuid),
                (SELECT count(*) FROM mail.image_analyses WHERE job_id=%s::uuid)
            """,
            (job_id, job_id),
        )
        assert cur.fetchone() == (0, 0), f"{name}: rollback failed"


try:
    run_rejection(
        "EXPIRED LEASE", "invalid or expired", "expired"
    )
    run_rejection(
        "WRONG TOKEN", "invalid or expired", "wrong_token"
    )
    run_rejection(
        "HASH MISMATCH", "SHA-256 mismatch", "wrong_hash"
    )
    run_rejection(
        "DUPLICATE ANALYSIS", "already exists", "duplicate"
    )
    print("ALL FAILURE CASES ROLLED BACK")
    print("POSTGRES WRITER FAILURE TESTS PASSED")
finally:
    conn.close()
