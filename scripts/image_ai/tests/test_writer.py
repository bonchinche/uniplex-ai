import uuid

import pytest

from image_ai.providers.mock_provider import MockProvider
from image_ai.provider_base import ProviderRequest
from image_ai.writer import (
    ImageWriterError,
    _validate_response,
    write_analysis,
)


SCHEMA = "/opt/uniplex/config/image_analysis_contract_v1.json"


def make_response(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"test image")
    request = ProviderRequest(
        request_id="writer-test",
        image_path=image,
        mime_type="image/png",
        source_metadata={},
        instructions={},
        output_contract={},
    )
    return image, MockProvider().analyze(request)


def test_mock_response_validation(tmp_path):
    _, response = make_response(tmp_path)
    output, status = _validate_response(response, "1.0", SCHEMA)

    assert output["contract_version"] == "1.0"
    assert status == "needs_review"


def test_pending_response_is_processed(tmp_path):
    _, response = make_response(tmp_path)
    response.output["overall_review"]["recommendation"] = "pending"

    _, status = _validate_response(response, "1.0", SCHEMA)

    assert status == "processed"


def test_invalid_output_is_rejected(tmp_path):
    _, response = make_response(tmp_path)
    del response.output["image_summary"]

    with pytest.raises(Exception):
        _validate_response(response, "1.0", SCHEMA)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-uuid"),
        ("attachment_id", "not-a-uuid"),
        ("lease_token", "not-a-uuid"),
    ],
)
def test_invalid_job_uuid_rejected_before_database(
    tmp_path, field, value
):
    image, response = make_response(tmp_path)
    job = {
        "id": str(uuid.uuid4()),
        "attachment_id": str(uuid.uuid4()),
        "lease_token": str(uuid.uuid4()),
    }
    job[field] = value

    with pytest.raises(ImageWriterError, match="Invalid"):
        write_analysis(
            None,
            job,
            "test-worker",
            image,
            response,
            "1.0",
            SCHEMA,
        )


def test_missing_job_identifier_rejected(tmp_path):
    image, response = make_response(tmp_path)

    with pytest.raises(ImageWriterError):
        write_analysis(
            None,
            {},
            "test-worker",
            image,
            response,
            "1.0",
            SCHEMA,
        )


def test_invalid_latency_rejected(tmp_path):
    _, response = make_response(tmp_path)
    response.latency_ms = True

    with pytest.raises(ImageWriterError, match="latency_ms"):
        _validate_response(response, "1.0", SCHEMA)


def test_non_json_usage_rejected(tmp_path):
    _, response = make_response(tmp_path)
    response.usage = {"tokens": object()}

    with pytest.raises(ImageWriterError, match="valid JSON"):
        _validate_response(response, "1.0", SCHEMA)


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.transaction_started = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.connection.rolled_back = True
        else:
            self.connection.committed = True
        return False


class FakeDBCursor:
    def __init__(self, source_hash, analysis_id, complete_job=True):
        self.source_hash = source_hash
        self.analysis_id = analysis_id
        self.complete_job = complete_job
        self.rowcount = 1
        self.last_result = None
        self.sql_log = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql_log.append(sql)
        normalized = " ".join(sql.split())

        if "FROM mail.processing_jobs" in normalized:
            self.last_result = ("job-locked",)
        elif "FROM mail.attachments" in normalized:
            self.last_result = (self.source_hash,)
        elif "FROM mail.image_analyses" in normalized:
            self.last_result = None
        elif "INSERT INTO mail.image_analyses" in normalized:
            self.last_result = (self.analysis_id,)
        elif "UPDATE mail.processing_jobs" in normalized:
            self.rowcount = 1 if self.complete_job else 0
            self.last_result = None
        else:
            self.last_result = None

    def executemany(self, sql, rows):
        self.sql_log.append(sql)
        self.inserted_fact_rows = list(rows)

    def fetchone(self):
        return self.last_result


class FakeDBConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.transaction_started = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.fake_cursor

    def transaction(self):
        return FakeTransaction(self)


def make_valid_job():
    return {
        "id": str(uuid.uuid4()),
        "attachment_id": str(uuid.uuid4()),
        "lease_token": str(uuid.uuid4()),
    }


def test_write_analysis_commits_atomically(tmp_path):
    import hashlib

    image, response = make_response(tmp_path)
    response.output["facts"] = [{
        "item_type": "text",
        "field_key": "document_number",
        "value": "TEST-001",
        "confidence": 95,
        "source_location": {"source_text": "TEST-001"},
        "review_recommendation": "pending",
    }]
    job = make_valid_job()
    expected_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    expected_analysis_id = uuid.uuid4()

    cursor = FakeDBCursor(
        expected_hash,
        expected_analysis_id,
        complete_job=True,
    )
    conn = FakeDBConnection(cursor)

    result = write_analysis(
        conn,
        job,
        "test-worker",
        image,
        response,
        "1.0",
        SCHEMA,
    )

    assert result == str(expected_analysis_id)
    assert conn.transaction_started is True
    assert conn.committed is True
    assert conn.rolled_back is False
    assert any("INSERT INTO mail.image_analyses" in sql for sql in cursor.sql_log)
    assert any("INSERT INTO mail.image_analysis_items" in sql for sql in cursor.sql_log)
    assert any("UPDATE mail.processing_jobs" in sql for sql in cursor.sql_log)


def test_write_analysis_rolls_back_if_job_completion_fails(tmp_path):
    import hashlib

    image, response = make_response(tmp_path)
    job = make_valid_job()
    expected_hash = hashlib.sha256(image.read_bytes()).hexdigest()

    cursor = FakeDBCursor(
        expected_hash,
        uuid.uuid4(),
        complete_job=False,
    )
    conn = FakeDBConnection(cursor)

    with pytest.raises(ImageWriterError, match="complete processing job"):
        write_analysis(
            conn,
            job,
            "test-worker",
            image,
            response,
            "1.0",
            SCHEMA,
        )

    assert conn.transaction_started is True
    assert conn.rolled_back is True
    assert conn.committed is False
