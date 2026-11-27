import hashlib

import pytest

from image_ai import worker
from image_ai.providers.mock_provider import MockProvider


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.fake_cursor = FakeCursor(row)

    def cursor(self):
        return self.fake_cursor


def test_load_attachment_accepts_valid_file(tmp_path, monkeypatch):
    storage = tmp_path / "mail"
    temp_dir = storage / "tmp"
    temp_dir.mkdir(parents=True)

    content = b"test image bytes"
    filename = "image.png"
    storage_name = "12345678-1234-1234-1234-123456789abc.bin"
    (temp_dir / storage_name).write_bytes(content)

    digest = hashlib.sha256(content).hexdigest()
    row = (
        filename,
        "image/png",
        len(content),
        digest,
        f"tmp/{storage_name}",
    )

    monkeypatch.setattr(worker, "STORAGE_ROOT", storage)

    result = worker.load_attachment(
        FakeConnection(row),
        "00000000-0000-0000-0000-000000000001",
        MockProvider(),
    )

    assert result["path"].read_bytes() == content
    assert result["sha256"] == digest
    assert result["mime_type"] == "image/png"


@pytest.mark.parametrize(
    "storage_key",
    [
        "../outside.bin",
        "/etc/passwd",
        "tmp/../../outside.bin",
        "tmp/not-a-uuid.bin",
    ],
)
def test_load_attachment_rejects_unsafe_storage_key(
    tmp_path, monkeypatch, storage_key
):
    storage = tmp_path / "mail"
    (storage / "tmp").mkdir(parents=True)
    monkeypatch.setattr(worker, "STORAGE_ROOT", storage)

    row = (
        "image.png",
        "image/png",
        10,
        "a" * 64,
        storage_key,
    )

    with pytest.raises(worker.WorkerError):
        worker.load_attachment(
            FakeConnection(row),
            "00000000-0000-0000-0000-000000000001",
            MockProvider(),
        )


def test_load_attachment_rejects_hash_mismatch(tmp_path, monkeypatch):
    storage = tmp_path / "mail"
    temp_dir = storage / "tmp"
    temp_dir.mkdir(parents=True)

    storage_name = "12345678-1234-1234-1234-123456789abc.bin"
    (temp_dir / storage_name).write_bytes(b"actual image")

    row = (
        "image.png",
        "image/png",
        len(b"actual image"),
        "0" * 64,
        f"tmp/{storage_name}",
    )

    monkeypatch.setattr(worker, "STORAGE_ROOT", storage)

    with pytest.raises(worker.WorkerError) as exc:
        worker.load_attachment(
            FakeConnection(row),
            "00000000-0000-0000-0000-000000000001",
            MockProvider(),
        )

    assert exc.value.code == "attachment_hash_mismatch"


def test_mock_provider_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(worker, "load_config", lambda path: {})
    monkeypatch.setattr(worker, "build_provider", lambda config: MockProvider())

    def unexpected_db_connection():
        pytest.fail("Worker must reject mock before connecting to database")

    monkeypatch.setattr(worker, "connect_db", unexpected_db_connection)

    with pytest.raises(worker.WorkerError) as exc:
        worker.run_worker(once=True, allow_mock=False)

    assert exc.value.code == "mock_provider_disabled"


def test_mock_provider_can_run_explicitly_with_empty_queue(monkeypatch):
    class EmptyConnection:
        closed = False

        def close(self):
            self.closed = True

    conn = EmptyConnection()

    monkeypatch.setattr(worker, "load_config", lambda path: {})
    monkeypatch.setattr(worker, "build_provider", lambda config: MockProvider())
    monkeypatch.setattr(worker, "connect_db", lambda: conn)
    monkeypatch.setattr(worker, "recover_expired_jobs", lambda conn: 0)
    monkeypatch.setattr(worker, "claim_job", lambda conn, worker_id: None)

    worker.run_worker(once=True, allow_mock=True)

    assert conn.closed


def test_shutdown_after_claim_requeues_job(monkeypatch):
    calls = {}

    class FakeHeartbeat:
        lost = False

        def __init__(self, job, worker_id):
            self.thread = type("FakeThread", (), {"ident": 1})()

        def start(self):
            pass

        def stop(self):
            calls["stopped"] = True
            return True

    def fake_fail_job(conn, job, worker_id, code, retryable, message):
        calls["failure"] = (code, retryable, message)

    monkeypatch.setattr(worker, "LeaseHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(worker, "fail_job", fake_fail_job)
    monkeypatch.setattr(
        worker, "load_attachment",
        lambda *args: pytest.fail("Attachment must not be loaded"),
    )
    worker.STOP_EVENT.set()

    try:
        result = worker.process_job(
            conn=None,
            job={"id": "job-1", "attachment_id": "attachment-1"},
            worker_id="test-worker",
            provider=MockProvider(),
            config={},
        )
    finally:
        worker.STOP_EVENT.clear()

    assert result is False
    assert calls["failure"][0] == "shutdown_requested"
    assert calls["failure"][1] is True
    assert calls["stopped"] is True


def test_shutdown_interrupts_queue_wait(monkeypatch):
    class EmptyConnection:
        closed = False

        def close(self):
            self.closed = True

    conn = EmptyConnection()

    monkeypatch.setattr(worker, "load_config", lambda path: {})
    monkeypatch.setattr(worker, "build_provider", lambda config: MockProvider())
    monkeypatch.setattr(worker, "connect_db", lambda: conn)
    monkeypatch.setattr(worker, "recover_expired_jobs", lambda conn: 0)

    def claim_and_request_shutdown(conn, worker_id):
        worker.STOP_EVENT.set()
        return None

    monkeypatch.setattr(worker, "claim_job", claim_and_request_shutdown)
    worker.STOP_EVENT.clear()

    try:
        worker.run_worker(
            once=False,
            allow_mock=True,
            poll_seconds=300,
        )
    finally:
        worker.STOP_EVENT.clear()

    assert conn.closed
