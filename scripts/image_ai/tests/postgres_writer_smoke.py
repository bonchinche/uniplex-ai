import os
import uuid
from pathlib import Path

import psycopg

from image_ai.provider_base import ProviderResponse
from image_ai.writer import write_analysis

DB_ENV = Path("/run/secrets/uniplex.env")
SCHEMA = "/config/image_analysis_contract_v1.json"
IMAGE = "/data/test-image.png"
ATTACHMENT_ID = "28b125d9-1dbd-4f05-936a-eff746484aa4"
WORKER_ID = "writer-integration-test"


def load_env():
    result = {}
    for line in DB_ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("'\"")
    return result


class RollbackTest(Exception):
    pass


env = load_env()
conn = psycopg.connect(
    host="uniplex-postgres",
    port=5432,
    dbname="uniplex_migration_test",
    user=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"],
    autocommit=True,
)

job_id = None
try:
    try:
        with conn.transaction():
            lease_token = str(uuid.uuid4())

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mail.processing_jobs (
                        attachment_id, job_type, status, attempts,
                        max_attempts, worker_id, heartbeat_at,
                        lease_expires_at, lease_token, started_at
                    )
                    VALUES (
                        %s::uuid, 'analyze_image', 'running', 1,
                        3, %s, clock_timestamp(),
                        clock_timestamp() + interval '5 minutes',
                        %s::uuid, clock_timestamp()
                    )
                    RETURNING id
                    """,
                    (ATTACHMENT_ID, WORKER_ID, lease_token),
                )
                job_id = str(cur.fetchone()[0])

            output = {
                "contract_version": "1.0",
                "image_summary": "TEST ONLY: rollback integration test",
                "detected_language": "unknown",
                "document_classification": {
                    "category": "test_fixture",
                    "confidence": 99,
                    "alternatives": [],
                },
                "facts": [{
                    "item_type": "identifier",
                    "field_key": "document_number",
                    "value": "TEST-ROLLBACK-ONLY",
                    "normalized_value": "TEST-ROLLBACK-ONLY",
                    "confidence": 99,
                    "source_location": {
                        "source_text": "TEST-ROLLBACK-ONLY"
                    },
                    "review_recommendation": "pending",
                    "metadata": {"test_fixture": True},
                }],
                "overall_review": {
                    "recommendation": "needs_review",
                    "reasons": ["integration_test"],
                },
                "warnings": ["TEST ONLY: no AI model was called"],
            }

            response = ProviderResponse(
                provider_name="mock",
                model_name="integration-test",
                model_version="1.0",
                output=output,
                request_metadata={"test": True},
                usage={},
                latency_ms=1,
            )

            analysis_id = write_analysis(
                conn=conn,
                job={
                    "id": job_id,
                    "attachment_id": ATTACHMENT_ID,
                    "lease_token": lease_token,
                },
                worker_id=WORKER_ID,
                source_path=IMAGE,
                response=response,
                prompt_version="integration-test-1",
                schema_path=SCHEMA,
            )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, is_current, source_content_hash
                    FROM mail.image_analyses
                    WHERE id = %s::uuid
                    """,
                    (analysis_id,),
                )
                analysis = cur.fetchone()
                assert analysis is not None
                assert analysis[0] == "needs_review"
                assert analysis[1] is True

                cur.execute(
                    """
                    SELECT review_status, field_key
                    FROM mail.image_analysis_items
                    WHERE analysis_id = %s::uuid
                    """,
                    (analysis_id,),
                )
                fact = cur.fetchone()
                assert fact == ("pending", "document_number")

                cur.execute(
                    """
                    SELECT status, lease_token, lease_expires_at
                    FROM mail.processing_jobs
                    WHERE id = %s::uuid
                    """,
                    (job_id,),
                )
                completed = cur.fetchone()
                assert completed == ("completed", None, None)

            print("WRITER SQL INSERTS OK")
            print("ANALYSIS STATUS:", analysis[0])
            print("FACT REVIEW STATUS:", fact[0])
            print("JOB STATUS:", completed[0])
            raise RollbackTest()

    except RollbackTest:
        pass

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM mail.processing_jobs WHERE id = %s::uuid",
            (job_id,),
        )
        jobs_left = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM mail.image_analyses WHERE job_id = %s::uuid",
            (job_id,),
        )
        analyses_left = cur.fetchone()[0]

        assert jobs_left == 0
        assert analyses_left == 0

    print("ROLLBACK VERIFIED: no test rows remain")
    print("POSTGRES WRITER INTEGRATION TEST PASSED")

finally:
    conn.close()
