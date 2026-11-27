from pathlib import Path

import pytest

from image_ai import queue
from image_ai.queue import QueueError


class FakeCursor:
    def __init__(self, fetchone=None, fetchall=None):
        self.fetchone_result = fetchone
        self.fetchall_result = fetchall or []
        self.executed_sql = None
        self.executed_params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed_sql = sql
        self.executed_params = params

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self):
        return self.fake_cursor


def test_load_env_reads_required_values(tmp_path):
    env_file = tmp_path / "db.env"
    env_file.write_text(
        'POSTGRES_DB=uniplex\n'
        'POSTGRES_USER=uniplex_app\n'
        'POSTGRES_PASSWORD=test-password\n'
    )

    result = queue.load_env(env_file)

    assert result["POSTGRES_DB"] == "uniplex"
    assert result["POSTGRES_USER"] == "uniplex_app"
    assert result["POSTGRES_PASSWORD"] == "test-password"


def test_load_env_rejects_missing_file(tmp_path):
    with pytest.raises(QueueError, match="missing"):
        queue.load_env(tmp_path / "missing.env")


def test_load_env_rejects_missing_required_variable(tmp_path):
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_DB=uniplex\n")

    with pytest.raises(QueueError, match="Missing required"):
        queue.load_env(env_file)


def test_connect_db_uses_loaded_configuration(monkeypatch, tmp_path):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return "fake-connection"

    monkeypatch.setattr(
        queue,
        "load_env",
        lambda path: {
            "POSTGRES_DB": "uniplex",
            "POSTGRES_USER": "uniplex_app",
            "POSTGRES_PASSWORD": "test-password",
        },
    )
    monkeypatch.setattr(queue.psycopg, "connect", fake_connect)

    result = queue.connect_db(tmp_path / "db.env")

    assert result == "fake-connection"
    assert captured["host"] == "uniplex-postgres"
    assert captured["port"] == 5432
    assert captured["dbname"] == "uniplex"
    assert captured["user"] == "uniplex_app"
    assert captured["password"] == "test-password"
    assert captured["autocommit"] is True


@pytest.mark.parametrize("limit", [0, -1, 501, True, "10"])
def test_recover_expired_jobs_rejects_invalid_limit(limit):
    with pytest.raises(QueueError):
        queue.recover_expired_jobs(FakeConnection(FakeCursor()), limit)


def test_recover_expired_jobs_returns_recovered_rows():
    expected = [("job-1", "queued"), ("job-2", "failed")]
    cursor = FakeCursor(fetchall=expected)

    result = queue.recover_expired_jobs(FakeConnection(cursor), 10)

    assert result == expected
    assert "FOR UPDATE SKIP LOCKED" in cursor.executed_sql
    assert "job_type = 'analyze_image'" in cursor.executed_sql
    assert "lease_expires_at <= clock_timestamp()" in cursor.executed_sql
    assert cursor.executed_params == (10,)


@pytest.mark.parametrize(
    "worker_id, lease_seconds",
    [
        ("", 180),
        ("   ", 180),
        ("x" * 257, 180),
        ("worker-1", 29),
        ("worker-1", 3601),
        ("worker-1", True),
    ],
)
def test_claim_job_rejects_invalid_arguments(worker_id, lease_seconds):
    with pytest.raises(QueueError):
        queue.claim_job(
            FakeConnection(FakeCursor()),
            worker_id,
            lease_seconds,
        )


def test_claim_job_returns_claimed_job():
    cursor = FakeCursor(
        fetchone=("job-1", "attachment-1", "lease-1", 1, 5)
    )

    result = queue.claim_job(
        FakeConnection(cursor),
        "worker-1",
        180,
    )

    assert result == {
        "id": "job-1",
        "attachment_id": "attachment-1",
        "lease_token": "lease-1",
        "attempts": 1,
        "max_attempts": 5,
    }
    assert "FOR UPDATE SKIP LOCKED" in cursor.executed_sql
    assert "job_type = 'analyze_image'" in cursor.executed_sql
    assert "attachment_id IS NOT NULL" in cursor.executed_sql
    assert cursor.executed_params == ("worker-1", 180)


def test_claim_job_returns_none_for_empty_queue():
    cursor = FakeCursor(fetchone=None)

    result = queue.claim_job(
        FakeConnection(cursor),
        "worker-1",
    )

    assert result is None


@pytest.mark.parametrize("lease_seconds", [29, 3601, True, "180"])
def test_heartbeat_rejects_invalid_lease(lease_seconds):
    with pytest.raises(QueueError):
        queue.heartbeat_job(
            FakeConnection(FakeCursor()),
            "job-1",
            "worker-1",
            "lease-1",
            lease_seconds,
        )


@pytest.mark.parametrize(
    "job_id, worker_id, lease_token",
    [
        ("", "worker-1", "lease-1"),
        ("job-1", "", "lease-1"),
        ("job-1", "worker-1", ""),
    ],
)
def test_heartbeat_rejects_missing_identifiers(
    job_id,
    worker_id,
    lease_token,
):
    with pytest.raises(QueueError):
        queue.heartbeat_job(
            FakeConnection(FakeCursor()),
            job_id,
            worker_id,
            lease_token,
        )


def test_heartbeat_returns_true_when_lease_updated():
    cursor = FakeCursor(fetchone=("job-1",))

    result = queue.heartbeat_job(
        FakeConnection(cursor),
        "job-1",
        "worker-1",
        "lease-1",
        180,
    )

    assert result is True
    assert "lease_token = %s" in cursor.executed_sql
    assert "lease_expires_at > clock_timestamp()" in cursor.executed_sql
    assert cursor.executed_params == (
        180,
        "job-1",
        "worker-1",
        "lease-1",
    )


def test_heartbeat_returns_false_when_lease_not_updated():
    cursor = FakeCursor(fetchone=None)

    result = queue.heartbeat_job(
        FakeConnection(cursor),
        "job-1",
        "worker-1",
        "stale-lease",
    )

    assert result is False


def test_fail_job_requeues_retryable_failure():
    cursor = FakeCursor(fetchone=("queued",))

    result = queue.fail_job(
        FakeConnection(cursor),
        {"id": "job-1", "lease_token": "lease-1"},
        "worker-1",
        "provider_timeout",
        True,
    )

    assert result == "queued"
    assert "status = 'running'" in cursor.executed_sql
    assert "lease_token = %s::uuid" in cursor.executed_sql
    assert "lease_expires_at > clock_timestamp()" in cursor.executed_sql
    assert cursor.executed_params[0] is True


def test_fail_job_marks_permanent_failure():
    cursor = FakeCursor(fetchone=("failed",))

    result = queue.fail_job(
        FakeConnection(cursor),
        {"id": "job-1", "lease_token": "lease-1"},
        "worker-1",
        "unsupported_image",
        False,
    )

    assert result == "failed"
    assert cursor.executed_params[0] is False


def test_fail_job_returns_none_for_lost_lease():
    cursor = FakeCursor(fetchone=None)

    result = queue.fail_job(
        FakeConnection(cursor),
        {"id": "job-1", "lease_token": "stale-token"},
        "worker-1",
        "provider_error",
        True,
    )

    assert result is None


@pytest.mark.parametrize(
    "retryable",
    [1, "yes", None],
)
def test_fail_job_rejects_non_boolean_retryable(retryable):
    with pytest.raises(QueueError):
        queue.fail_job(
            FakeConnection(FakeCursor()),
            {"id": "job-1", "lease_token": "lease-1"},
            "worker-1",
            "provider_error",
            retryable,
        )
