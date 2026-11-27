import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg


DB_ENV = Path("/run/secrets/uniplex.env")
PROCESSOR = Path("/app/process_email.py")

DEFAULT_LEASE_SECONDS = 180
DEFAULT_HEARTBEAT_SECONDS = 30
DEFAULT_POLL_SECONDS = 10


SHUTDOWN_REQUESTED = False


def handle_shutdown(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True


def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def connect_db():
    env = load_env(DB_ENV)
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


def recover_expired_jobs(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH expired AS (
                SELECT id
                FROM mail.processing_jobs
                WHERE status = 'running'
                  AND job_type = 'extract_document'
                  AND lease_expires_at <= clock_timestamp()
                ORDER BY lease_expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 50
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
                last_error = COALESCE(
                    j.last_error,
                    'Worker lease expired'
                ),
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
            """
        )
        recovered = cur.rowcount

        cur.execute(
            """
            WITH exhausted AS (
                SELECT id
                FROM mail.processing_jobs
                WHERE status = 'queued'
                  AND job_type = 'extract_document'
                  AND attempts >= max_attempts
                ORDER BY scheduled_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 50
            )
            UPDATE mail.processing_jobs AS j
            SET status = 'failed',
                error_code = 'attempts_exhausted',
                last_error = COALESCE(
                    j.last_error,
                    'Maximum attempts already reached'
                ),
                finished_at = clock_timestamp(),
                lease_expires_at = NULL,
                lease_token = NULL,
                updated_at = clock_timestamp()
            FROM exhausted
            WHERE j.id = exhausted.id
            """
        )
        exhausted = cur.rowcount

    if recovered or exhausted:
        print(
            f"QUEUE RECOVERY: expired={recovered}, "
            f"exhausted={exhausted}",
            flush=True
        )


def claim_job(conn, worker_id, lease_seconds):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM mail.processing_jobs
                    WHERE status = 'queued'
                      AND job_type = 'extract_document'
                      AND scheduled_at <= clock_timestamp()
                      AND attempts < max_attempts
                    ORDER BY scheduled_at, created_at, id
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
                    started_at = COALESCE(
                        j.started_at,
                        clock_timestamp()
                    ),
                    finished_at = NULL,
                    last_error = NULL,
                    error_code = NULL,
                    updated_at = clock_timestamp()
                FROM candidate
                WHERE j.id = candidate.id
                RETURNING
                    j.id,
                    j.attachment_id,
                    j.job_type,
                    j.lease_token,
                    j.attempts,
                    j.max_attempts
                """,
                (worker_id, lease_seconds)
            )
            row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": str(row[0]),
        "attachment_id": str(row[1]) if row[1] else None,
        "job_type": row[2],
        "lease_token": str(row[3]),
        "attempts": row[4],
        "max_attempts": row[5],
    }


def heartbeat(conn, job, worker_id, lease_seconds):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mail.processing_jobs
            SET heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp()
                    + make_interval(secs => %s),
                updated_at = clock_timestamp()
            WHERE id = %s::uuid
              AND status = 'running'
              AND worker_id = %s
              AND lease_token = %s::uuid
              AND lease_expires_at > clock_timestamp()
            RETURNING id
            """,
            (
                lease_seconds,
                job["id"],
                worker_id,
                job["lease_token"],
            )
        )
        return cur.fetchone() is not None


def complete_job(conn, job, worker_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mail.processing_jobs
            SET status = 'completed',
                finished_at = clock_timestamp(),
                heartbeat_at = clock_timestamp(),
                lease_expires_at = NULL,
                lease_token = NULL,
                updated_at = clock_timestamp()
            WHERE id = %s::uuid
              AND status = 'running'
              AND worker_id = %s
              AND lease_token = %s::uuid
              AND lease_expires_at > clock_timestamp()
            RETURNING id
            """,
            (job["id"], worker_id, job["lease_token"])
        )
        return cur.fetchone() is not None


def fail_job(conn, job, worker_id, error_code, error, retryable=True):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mail.processing_jobs
            SET status = CASE
                    WHEN %s AND attempts < max_attempts THEN 'queued'
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
                finished_at = CASE
                    WHEN %s AND attempts < max_attempts THEN NULL
                    ELSE clock_timestamp()
                END,
                lease_expires_at = NULL,
                lease_token = NULL,
                updated_at = clock_timestamp()
            WHERE id = %s::uuid
              AND status = 'running'
              AND worker_id = %s
              AND lease_token = %s::uuid
              AND lease_expires_at > clock_timestamp()
            RETURNING status
            """,
            (
                retryable,
                retryable,
                error[:4000],
                error_code,
                retryable,
                job["id"],
                worker_id,
                job["lease_token"],
            )
        )
        row = cur.fetchone()

    return row[0] if row else None


def get_email_state(conn, email_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.uid, e.processing_status, e.processed_at
            FROM mail.emails e
            JOIN mail.mail_folders f ON f.id = e.folder_id
            JOIN mail.mail_accounts a ON a.id = f.account_id
            WHERE e.id = %s::uuid
              AND f.folder_path = 'INBOX'
              AND a.is_active = TRUE
            """,
            (email_id,)
        )
        return cur.fetchone()


def run_processor(conn, job, worker_id, lease_seconds, heartbeat_seconds):
    if not job["email_id"]:
        status = fail_job(
            conn, job, worker_id,
            "missing_email_id",
            "process_email job has no email_id",
            retryable=False,
        )
        print("JOB FAILED:", status, flush=True)
        return

    email = get_email_state(conn, job["email_id"])

    if email is None:
        status = fail_job(
            conn, job, worker_id,
            "email_not_found",
            "Email or active INBOX account not found",
            retryable=False,
        )
        print("JOB FAILED:", status, flush=True)
        return

    uid, email_status, processed_at = email

    if email_status in ("processed", "needs_review") and processed_at:
        if complete_job(conn, job, worker_id):
            print(
                "JOB RECONCILED: email was already processed",
                flush=True
            )
        else:
            print("JOB RECONCILIATION: lease lost", flush=True)
        return

    if email_status != "new":
        status = fail_job(
            conn, job, worker_id,
            "email_not_new",
            f"Email status is {email_status!r}; expected 'new'",
            retryable=False,
        )
        print("JOB FAILED:", status, flush=True)
        return

    command = [
        sys.executable,
        str(PROCESSOR),
        "--uid", str(uid),
        "--job-id", job["id"],
        "--lease-token", job["lease_token"],
        "--email-id", job["email_id"],
        "--apply",
    ]

    print(
        f"PROCESSOR START: job={job['id']} "
        f"email={job['email_id']} uid={uid} "
        f"attempt={job['attempts']}/{job['max_attempts']}",
        flush=True
    )

    try:
        lease_valid = heartbeat(
            conn, job, worker_id, lease_seconds
        )
    except Exception as error:
        print(
            "PRE-START HEARTBEAT FAILED:",
            str(error)[:500],
            flush=True
        )
        return

    if not lease_valid:
        print(
            "PRE-START LEASE CHECK FAILED; PROCESSOR NOT STARTED",
            flush=True
        )
        return

    if SHUTDOWN_REQUESTED:
        print("SHUTDOWN REQUESTED: processor not started", flush=True)
        return

    try:
        process = subprocess.Popen(command)
    except OSError as error:
        status = fail_job(
            conn, job, worker_id,
            "processor_start_failed",
            f"Could not start processor: {error}",
            retryable=True,
        )
        print(
            "PROCESSOR START FAILED:",
            str(error)[:500],
            "job_status=",
            status,
            flush=True,
        )
        return

    last_successful_heartbeat = time.monotonic()
    next_heartbeat = last_successful_heartbeat + heartbeat_seconds
    lease_lost = False

    while process.poll() is None and not SHUTDOWN_REQUESTED:
        time.sleep(1)

        if time.monotonic() >= next_heartbeat:
            try:
                valid = heartbeat(
                    conn, job, worker_id, lease_seconds
                )
            except Exception as error:
                print(
                    "HEARTBEAT ERROR:",
                    str(error)[:500],
                    flush=True
                )

                # Do not let a child continue beyond the known lease
                # with no successful heartbeat.
                elapsed = time.monotonic() - last_successful_heartbeat
                if elapsed >= lease_seconds - 10:
                    lease_lost = True
                    print(
                        "LEASE STATUS UNKNOWN: heartbeat unavailable "
                        "until safety deadline",
                        flush=True
                    )
                    break

                next_heartbeat = time.monotonic() + 5
                continue

            if not valid:
                lease_lost = True
                print(
                    "LEASE LOST: current token is no longer valid",
                    flush=True
                )
                break

            last_successful_heartbeat = time.monotonic()
            next_heartbeat = time.monotonic() + heartbeat_seconds

    if SHUTDOWN_REQUESTED:
        if process.poll() is None:
            print("STOPPING PROCESSOR FOR WORKER SHUTDOWN", flush=True)
            try:
                process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("PROCESSOR DID NOT STOP; SENDING KILL", flush=True)
                process.kill()
                process.wait()
            except ProcessLookupError:
                process.wait()

        print(
            "WORKER SHUTDOWN: job left for lease recovery",
            flush=True
        )
        return

    if lease_lost:
        if process.poll() is None:
            print(
                "STOPPING PROCESSOR AFTER LEASE LOSS",
                flush=True
            )
            try:
                process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(
                    "PROCESSOR DID NOT STOP; SENDING KILL",
                    flush=True
                )
                process.kill()
                process.wait()
            except ProcessLookupError:
                process.wait()

        print(
            "PROCESSOR STOPPED AFTER LEASE LOSS; "
            "job state belongs to the current lease holder",
            flush=True
        )
        return

    return_code = process.wait()

    if return_code == 0:
        if complete_job(conn, job, worker_id):
            print("JOB COMPLETED:", job["id"], flush=True)
        else:
            print(
                "JOB COMPLETION NOT RECORDED: lease expired or changed",
                flush=True
            )
    else:
        status = fail_job(
            conn, job, worker_id,
            "processor_exit",
            f"Processor exited with code {return_code}",
            retryable=True,
        )
        print(
            f"PROCESSOR FAILED: exit={return_code}, "
            f"job_status={status}",
            flush=True
        )


def show_queue_status(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, count(*)
            FROM mail.processing_jobs
            GROUP BY status
            ORDER BY status
            """
        )
        rows = cur.fetchall()

    print("QUEUE STATUS:")
    if not rows:
        print("  No jobs")
    else:
        for status, count in rows:
            print(f"  {status}: {count}")


def main():
    parser = argparse.ArgumentParser(
        description="UNIPLEX queue worker"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_SECONDS
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=DEFAULT_HEARTBEAT_SECONDS
    )
    args = parser.parse_args()

    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    if args.lease_seconds < 30:
        parser.error("--lease-seconds must be at least 30")
    if args.heartbeat_seconds < 5:
        parser.error("--heartbeat-seconds must be at least 5")
    if args.heartbeat_seconds * 2 >= args.lease_seconds:
        parser.error("Heartbeat interval must be less than half the lease")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    conn = connect_db()
    print("DATABASE CONNECTION: OK", flush=True)

    try:
        if args.check:
            show_queue_status(conn)
            return

        while not SHUTDOWN_REQUESTED:
            recover_expired_jobs(conn)

            if SHUTDOWN_REQUESTED:
                break

            job = claim_job(
                conn, worker_id, args.lease_seconds
            )

            if job is None:
                if args.once:
                    print("NO QUEUED JOBS", flush=True)
                    return
                for _ in range(args.poll_seconds):
                    if SHUTDOWN_REQUESTED:
                        break
                    time.sleep(1)
                continue

            print(
                f"JOB CLAIMED: {job['id']} "
                f"attempt={job['attempts']}",
                flush=True
            )

            if SHUTDOWN_REQUESTED:
                print(
                    "SHUTDOWN REQUESTED: claimed job left for lease recovery",
                    flush=True
                )
                return

            run_processor(
                conn,
                job,
                worker_id,
                args.lease_seconds,
                args.heartbeat_seconds,
            )

            if args.once:
                return

        if SHUTDOWN_REQUESTED:
            print("WORKER SHUTDOWN: no new jobs will be claimed", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
