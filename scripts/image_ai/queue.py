from pathlib import Path

import psycopg


DB_ENV = Path("/run/secrets/uniplex.env")


class QueueError(Exception):
    pass


def load_env(path=DB_ENV):
    path = Path(path)

    if not path.is_file():
        raise QueueError("Database environment file is missing")

    result = {}

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")

    required = (
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    )

    missing = [key for key in required if not result.get(key)]

    if missing:
        raise QueueError(
            "Missing required database environment variables: "
            + ", ".join(missing)
        )

    return result


def connect_db(env_path=DB_ENV):
    env = load_env(env_path)

    return psycopg.connect(
        host="uniplex-postgres",
        port=5432,
        dbname=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        connect_timeout=10,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
        autocommit=True,
    )


def recover_expired_jobs(conn, limit=50):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise QueueError("Recovery limit must be between 1 and 500")

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH expired AS (
                SELECT id
                FROM mail.processing_jobs
                WHERE status = 'running'
                  AND job_type = 'analyze_image'
                  AND attachment_id IS NOT NULL
                  AND lease_expires_at <= clock_timestamp()
                ORDER BY lease_expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE mail.processing_jobs AS j
            SET status = CASE
                    WHEN j.attempts >= j.max_attempts THEN 'failed'
                    ELSE 'queued'
                END,
                scheduled_at = CASE
                    WHEN j.attempts >= j.max_attempts
                        THEN j.scheduled_at
                    ELSE clock_timestamp() + make_interval(
                        secs => CASE
                            WHEN j.attempts <= 1 THEN 30
                            WHEN j.attempts = 2 THEN 120
                            WHEN j.attempts = 3 THEN 600
                            ELSE 1800
                        END
                    )
                END,
                last_error = 'Worker lease expired',
                error_code = 'lease_expired',
                lease_expires_at = NULL,
                lease_token = NULL,
                finished_at = CASE
                    WHEN j.attempts >= j.max_attempts
                        THEN clock_timestamp()
                    ELSE NULL
                END,
                updated_at = clock_timestamp()
            FROM expired
            WHERE j.id = expired.id
            RETURNING j.id, j.status
            """,
            (limit,),
        )

        recovered = cur.fetchall()

    return recovered


def claim_job(conn, worker_id, lease_seconds=180):
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise QueueError("worker_id must be a non-empty string")

    if len(worker_id) > 256:
        raise QueueError("worker_id is too long")

    if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
        raise QueueError("lease_seconds must be between 30 and 3600")

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH candidate AS (
                SELECT id
                FROM mail.processing_jobs
                WHERE status = 'queued'
                  AND job_type = 'analyze_image'
                  AND attachment_id IS NOT NULL
                  AND scheduled_at <= clock_timestamp()
                  AND attempts < max_attempts
                ORDER BY scheduled_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE mail.processing_jobs AS j
            SET status = 'running',
                worker_id = %s,
                lease_token = gen_random_uuid(),
                heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp()
                    + make_interval(secs => %s),
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, clock_timestamp()),
                finished_at = NULL,
                updated_at = clock_timestamp()
            FROM candidate
            WHERE j.id = candidate.id
              AND j.status = 'queued'
            RETURNING
                j.id::text,
                j.attachment_id::text,
                j.lease_token::text,
                j.attempts,
                j.max_attempts
            """,
            (worker_id, lease_seconds),
        )

        row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "attachment_id": row[1],
        "lease_token": row[2],
        "attempts": row[3],
        "max_attempts": row[4],
    }


def heartbeat_job(conn, job_id, worker_id, lease_token, lease_seconds=180):
    if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
        raise QueueError("lease_seconds must be between 30 and 3600")

    if not worker_id or not lease_token or not job_id:
        raise QueueError("job_id, worker_id and lease_token are required")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mail.processing_jobs
            SET heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp()
                    + make_interval(secs => %s),
                updated_at = clock_timestamp()
            WHERE id = %s
              AND job_type = 'analyze_image'
              AND attachment_id IS NOT NULL
              AND status = 'running'
              AND worker_id = %s
              AND lease_token = %s
              AND lease_expires_at > clock_timestamp()
            RETURNING id
            """,
            (lease_seconds, job_id, worker_id, lease_token),
        )
        return cur.fetchone() is not None


def fail_job(
    conn,
    job,
    worker_id,
    error_code,
    retryable,
    safe_message="Image analysis failed",
):
    if not isinstance(job, dict):
        raise QueueError("job must be an object")

    if type(retryable) is not bool:
        raise QueueError("retryable must be a boolean")

    job_id = job.get("id")
    lease_token = job.get("lease_token")

    if not job_id or not lease_token or not worker_id:
        raise QueueError("Job lease information is incomplete")

    if not isinstance(error_code, str) or not error_code.strip():
        raise QueueError("error_code is required")

    if len(error_code) > 100:
        raise QueueError("error_code is too long")

    if not isinstance(safe_message, str) or not safe_message.strip():
        raise QueueError("safe_message is required")

    safe_message = safe_message.strip()[:500]

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mail.processing_jobs
            SET status = CASE
                    WHEN %s AND attempts < max_attempts
                        THEN 'queued'
                    ELSE 'failed'
                END,
                scheduled_at = CASE
                    WHEN %s AND attempts < max_attempts
                        THEN clock_timestamp() + make_interval(
                            secs => CASE
                                WHEN attempts <= 1 THEN 30
                                WHEN attempts = 2 THEN 120
                                WHEN attempts = 3 THEN 600
                                ELSE 1800
                            END
                        )
                    ELSE scheduled_at
                END,
                last_error = %s,
                error_code = %s,
                lease_expires_at = NULL,
                lease_token = NULL,
                finished_at = CASE
                    WHEN %s AND attempts < max_attempts
                        THEN NULL
                    ELSE clock_timestamp()
                END,
                updated_at = clock_timestamp()
            WHERE id = %s::uuid
              AND job_type = 'analyze_image'
              AND attachment_id IS NOT NULL
              AND status = 'running'
              AND worker_id = %s
              AND lease_token = %s::uuid
              AND lease_expires_at > clock_timestamp()
            RETURNING status
            """,
            (
                retryable,
                retryable,
                safe_message,
                error_code,
                retryable,
                job_id,
                worker_id,
                lease_token,
            ),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return row[0]
