from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

from image_ai.config_loader import load_config, create_provider
from image_ai.contracts import load_schema, validate_input
from image_ai.provider_base import ProviderRequest, ProviderError
from image_ai.provider_registry import ProviderRegistry
from image_ai.providers.mock_provider import MockProvider
from image_ai.providers.deepseek_provider import DeepSeekProvider
from image_ai.queue import (
    connect_db,
    recover_expired_jobs,
    claim_job,
    heartbeat_job,
    fail_job,
)
from image_ai.writer import write_analysis, ImageWriterError


LOG = logging.getLogger("uniplex.image_ai.worker")

STOP_EVENT = threading.Event()

CONFIG_PATH = Path("/opt/uniplex/config/image_ai_providers.json")
INPUT_SCHEMA = Path(
    "/opt/uniplex/config/image_analysis_input_contract_v1.json"
)
OUTPUT_SCHEMA = Path(
    "/opt/uniplex/config/image_analysis_contract_v1.json"
)
STORAGE_ROOT = Path("/data/mail")
PROMPT_VERSION = "1.0"

SUPPORTED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "image/bmp",
}


class WorkerError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def build_provider(config, provider_name=None):
    registry = ProviderRegistry()

    registry.register(
        "mock",
        "image_ai.providers.mock_provider.MockProvider",
        MockProvider,
    )

    deepseek_config = config.get("providers", {}).get("deepseek", {})

    registry.register(
        "deepseek",
        "image_ai.providers.deepseek_provider.DeepSeekProvider",
        lambda: DeepSeekProvider(
            model=deepseek_config.get("model", "deepseek-flash"),
            timeout_seconds=deepseek_config.get("timeout_seconds", 90),
            max_image_bytes=deepseek_config.get(
                "max_image_bytes", 25 * 1024 * 1024
            ),
            schema_path=OUTPUT_SCHEMA,
        ),
    )

    selected = provider_name or config.get("default_provider")
    return create_provider(config, registry, selected)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_attachment(conn, attachment_id, provider):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT filename, content_type, size_bytes,
                   content_hash, storage_key
            FROM mail.attachments
            WHERE id = %s::uuid
            """,
            (attachment_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise WorkerError(
            "attachment_missing", "Attachment does not exist"
        )

    filename, mime_type, size_bytes, expected_hash, storage_key = row

    if mime_type not in SUPPORTED_MIME_TYPES:
        raise WorkerError(
            "unsupported_mime_type", "Unsupported image MIME type"
        )

    capabilities = provider.capabilities()

    if mime_type not in capabilities.supported_mime_types:
        raise WorkerError(
            "unsupported_provider_mime",
            "Provider does not support this image format",
        )

    if not isinstance(storage_key, str) or not re.fullmatch(
        r"tmp/[0-9a-fA-F-]{36}\.bin", storage_key
    ):
        raise WorkerError(
            "invalid_storage_key", "Attachment storage key is invalid"
        )

    root = STORAGE_ROOT.resolve(strict=True)
    tmp_dir = root / "tmp"

    if tmp_dir.is_symlink():
        raise WorkerError(
            "invalid_storage_directory",
            "Attachment storage directory is invalid",
        )

    candidate = root / storage_key

    if candidate.is_symlink():
        raise WorkerError(
            "invalid_storage_file", "Attachment file is invalid"
        )

    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkerError(
            "attachment_file_missing",
            "Attachment file is unavailable",
        ) from exc

    if not path.is_file():
        raise WorkerError(
            "attachment_not_file", "Attachment path is not a file"
        )

    actual_size = path.stat().st_size

    if (
        type(size_bytes) is not int
        or size_bytes <= 0
        or actual_size != size_bytes
        or actual_size > capabilities.max_image_bytes
    ):
        raise WorkerError(
            "attachment_size_invalid",
            "Attachment size validation failed",
        )

    if (
        not isinstance(expected_hash, str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash)
        or sha256_file(path) != expected_hash.lower()
    ):
        raise WorkerError(
            "attachment_hash_mismatch",
            "Attachment integrity validation failed",
        )

    return {
        "path": path,
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": actual_size,
        "sha256": expected_hash.lower(),
    }


class LeaseHeartbeat:
    def __init__(self, job, worker_id, interval_seconds=45):
        self.job = job
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.lost_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="image-ai-heartbeat",
            daemon=True,
        )

    @property
    def lost(self):
        return self.lost_event.is_set()

    def start(self):
        self.thread.start()

        if not self.ready_event.wait(timeout=15):
            self.lost_event.set()
            raise WorkerError(
                "heartbeat_start_timeout",
                "Could not initialize lease heartbeat",
                retryable=True,
            )

        if self.lost:
            raise WorkerError(
                "heartbeat_start_failed",
                "Lease heartbeat initialization failed",
                retryable=True,
            )

    def _run(self):
        conn = None

        try:
            conn = connect_db()
            self.ready_event.set()

            while not self.stop_event.wait(self.interval_seconds):
                renewed = heartbeat_job(
                    conn,
                    self.job["id"],
                    self.worker_id,
                    self.job["lease_token"],
                )

                if not renewed:
                    self.lost_event.set()
                    LOG.warning("Image job lease was lost")
                    return

        except Exception:
            self.lost_event.set()
            LOG.exception("Image job heartbeat failed")

        finally:
            self.ready_event.set()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    LOG.warning("Could not close heartbeat connection")

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=15)

        if self.thread.is_alive():
            self.lost_event.set()
            LOG.error("Heartbeat thread did not stop in time")

        return not self.lost


def build_provider_request(job, attachment, config, provider_name=None):
    provider_name = provider_name or config["default_provider"]
    provider_config = config["providers"][provider_name]

    payload = {
        "contract_version": "1.0",
        "request_id": str(uuid.uuid4()),
        "task": "describe_and_extract",
        "source": {
            "attachment_id": job["attachment_id"],
            "sha256": attachment["sha256"],
            "mime_type": attachment["mime_type"],
            "size_bytes": attachment["size_bytes"],
            "filename": attachment["filename"],
        },
        "image_payload": {
            "transport": "binary_part",
            "part_name": "image",
        },
        "instructions": {
            "language_mode": "auto",
            "extraction_scope": (
                "Опиши изображение и извлеки видимые факты. "
                "Не додумывай нечитаемые или отсутствующие данные."
            ),
            "domain_context": (
                "UNIPLEX — производитель подложки для ламината. "
                "Документ может содержать сведения о продукции, "
                "поставщиках, клиентах, заказах или производстве."
            ),
        },
        "output_contract": {
            "name": "UNIPLEX Image Analysis Contract",
            "version": "1.0",
        },
        "generation": provider_config.get("generation", {}),
    }

    validate_input(payload, INPUT_SCHEMA)

    return ProviderRequest(
        request_id=payload["request_id"],
        image_path=attachment["path"],
        mime_type=attachment["mime_type"],
        source_metadata=payload["source"],
        instructions=payload["instructions"],
        output_contract=payload["output_contract"],
        generation=payload["generation"],
    )


def process_job(conn, job, worker_id, provider, config):
    heartbeat = LeaseHeartbeat(job, worker_id)

    try:
        heartbeat.start()

        if STOP_EVENT.is_set():
            raise WorkerError(
                "shutdown_requested",
                "Worker shutdown requested",
                retryable=True,
            )

        attachment = load_attachment(
            conn, job["attachment_id"], provider
        )

        request = build_provider_request(
            job, attachment, config,
            provider.capabilities().provider_name,
        )

        if heartbeat.lost:
            raise WorkerError(
                "lease_lost", "Job lease was lost", retryable=True
            )

        response = provider.analyze(request)

        if heartbeat.lost:
            raise WorkerError(
                "lease_lost", "Job lease was lost", retryable=True
            )

        if STOP_EVENT.is_set():
            raise WorkerError(
                "shutdown_requested",
                "Worker shutdown requested",
                retryable=True,
            )

        analysis_id = write_analysis(
            conn=conn,
            job=job,
            worker_id=worker_id,
            source_path=attachment["path"],
            response=response,
            prompt_version=PROMPT_VERSION,
            schema_path=OUTPUT_SCHEMA,
        )

        LOG.info("Image analysis saved: %s", analysis_id)
        return True

    except ProviderError as exc:
        if not heartbeat.lost:
            fail_job(
                conn, job, worker_id,
                str(exc.code)[:100],
                bool(exc.retryable),
                "Image provider request failed",
            )
        raw = exc.diagnostics if isinstance(exc.diagnostics, dict) else {}
        safe = {}

        for key in ("request_id", "model", "finish_reason"):
            value = raw.get(key)
            if isinstance(value, str):
                safe[key] = "".join(
                    ch for ch in value[:128]
                    if ch.isalnum() or ch in "._-"
                )

        for key in ("completion_tokens", "content_length"):
            value = raw.get(key)
            if type(value) is int and value >= 0:
                safe[key] = value

        if type(raw.get("markdown_fence")) is bool:
            safe["markdown_fence"] = raw["markdown_fence"]

        LOG.warning(
            "Image provider request failed: %s diagnostics=%r",
            exc.code,
            safe,
        )
        return False

    except WorkerError as exc:
        if not heartbeat.lost:
            fail_job(
                conn, job, worker_id,
                exc.code,
                exc.retryable,
                str(exc),
            )
        LOG.warning("Image job rejected: %s", exc.code)
        return False

    except ImageWriterError:
        if not heartbeat.lost:
            fail_job(
                conn, job, worker_id,
                "writer_rejected",
                False,
                "Image analysis result could not be saved",
            )
        LOG.exception("Image writer rejected the result")
        return False

    except Exception:
        if not heartbeat.lost:
            fail_job(
                conn, job, worker_id,
                "worker_internal_error",
                True,
                "Internal image worker error",
            )
        LOG.exception("Unexpected image worker error")
        return False

    finally:
        if heartbeat.thread.ident is not None:
            heartbeat.stop()


def run_worker(
    once=False, allow_mock=False, poll_seconds=10, provider_name=None
):
    config = load_config(CONFIG_PATH)
    provider = (
        build_provider(config)
        if provider_name is None
        else build_provider(config, provider_name)
    )

    if provider.capabilities().provider_name == "mock" and not allow_mock:
        raise WorkerError(
            "mock_provider_disabled",
            "Mock provider is test-only; explicit opt-in required",
        )

    worker_id = (
        f"{socket.gethostname()}-image-{uuid.uuid4().hex[:12]}"
    )

    conn = connect_db()

    try:
        recovered = recover_expired_jobs(conn)
        LOG.info("Expired image jobs recovered: %s", recovered)

        while True:
            if STOP_EVENT.is_set():
                LOG.info("Shutdown requested; stopping before next job")
                return

            job = claim_job(conn, worker_id)

            if job is None:
                if once:
                    LOG.info("No image jobs available")
                    return
                STOP_EVENT.wait(poll_seconds)
                continue

            LOG.info("Claimed image job: %s", job["id"])
            process_job(conn, job, worker_id, provider, config)

            if once:
                return

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="UNIPLEX AI image analysis worker"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued job and exit",
    )
    parser.add_argument(
        "--provider",
        help="Configured provider name; defaults to default_provider",
    )
    parser.add_argument(
        "--allow-mock",
        action="store_true",
        help="Allow the synthetic test-only mock provider",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=10,
    )
    args = parser.parse_args()

    if not 1 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must be between 1 and 300")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    STOP_EVENT.clear()

    def request_shutdown(signum, frame):
        LOG.info("Received signal %s; graceful shutdown requested", signum)
        STOP_EVENT.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    try:
        run_worker(
            once=args.once,
            allow_mock=args.allow_mock,
            poll_seconds=args.poll_seconds,
            provider_name=args.provider,
        )
    except Exception:
        LOG.exception("Image worker stopped")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
