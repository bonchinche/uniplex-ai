import argparse
import hashlib
import imaplib
import os
import re
import tempfile
import uuid
from pathlib import Path

import psycopg
from email import policy
from email.parser import BytesParser


MAX_MESSAGE_BYTES = 35 * 1024 * 1024
MAIL_ENV = Path("/run/secrets/mail.env")
DB_ENV = Path("/run/secrets/uniplex.env")
STORAGE_ROOT = Path("/data/mail")


def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def fetch_item(data):
    return next(
        (
            item for item in data
            if isinstance(item, tuple)
            and isinstance(item[1], bytes)
        ),
        None
    )


def fetch_size(imap, uid):
    status, data = imap.uid("fetch", uid, "(UID RFC822.SIZE)")
    if status != "OK":
        raise RuntimeError("Cannot fetch RFC822.SIZE")

    for item in data:
        if isinstance(item, tuple):
            response = item[0]
        elif isinstance(item, bytes):
            response = item
        else:
            continue

        match = re.search(rb"RFC822\.SIZE\s+(\d+)", response)
        if match:
            return int(match.group(1))

    raise RuntimeError("RFC822.SIZE missing")


def fetch_message(imap, uid):
    status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError("BODY.PEEK fetch failed")

    item = fetch_item(data)
    if item is None:
        raise RuntimeError("Message body not returned")

    return item[1]


def write_attachment(payload):
    file_id = str(uuid.uuid4())
    final_path = STORAGE_ROOT / "tmp" / f"{file_id}.bin"
    final_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_path = tempfile.mkstemp(
        prefix=".uniplex-",
        suffix=".part",
        dir=final_path.parent
    )

    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, final_path)
        return final_path, f"tmp/{file_id}.bin"

    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def main():
    cli = argparse.ArgumentParser(
        description="Process one UNIPLEX email by IMAP UID"
    )
    cli.add_argument("--uid", type=int, required=True)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = cli.parse_args()

    mail_env = load_env(MAIL_ENV)
    db_env = load_env(DB_ENV)

    email_address = mail_env["YANDEX_EMAIL"]
    username = email_address.split("@", 1)[0]
    uid = str(args.uid)

    conn = None
    imap = None

    try:
        conn = psycopg.connect(
            host="uniplex-postgres",
            port=5432,
            dbname=db_env["POSTGRES_DB"],
            user=db_env["POSTGRES_USER"],
            password=db_env["POSTGRES_PASSWORD"],
            connect_timeout=10,
            autocommit=True,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id, e.folder_id, e.uid_validity,
                       e.size_bytes, e.processing_status,
                       f.uid_validity
                FROM mail.emails e
                JOIN mail.mail_folders f ON f.id = e.folder_id
                JOIN mail.mail_accounts a ON a.id = f.account_id
                WHERE e.uid = %s
                  AND f.folder_path = 'INBOX'
                  AND a.email_address = %s
                  AND a.is_active = TRUE
                """,
                (args.uid, email_address)
            )
            rows = cur.fetchall()

        if len(rows) != 1:
            raise RuntimeError(
                f"Expected exactly one database email, found {len(rows)}"
            )

        email_id, folder_id, db_validity, db_size, db_status, folder_validity = rows[0]

        if db_status != "new":
            raise RuntimeError(
                f"Email status is {db_status!r}; only new emails can be processed"
            )

        if db_validity != folder_validity:
            raise RuntimeError("Database UIDVALIDITY mismatch")

        imap = imaplib.IMAP4_SSL("imap.ya.ru", 993)
        imap.login(username, mail_env["YANDEX_APP_PASSWORD"])

        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Cannot select INBOX read-only")

        validity_response = imap.response("UIDVALIDITY")
        validity_match = re.search(
            rb"\d+",
            validity_response[1][0]
            if validity_response[1] and isinstance(validity_response[1][0], bytes)
            else b""
        )
        if not validity_match:
            raise RuntimeError("Cannot read IMAP UIDVALIDITY")

        imap_validity = int(validity_match.group())

        if imap_validity != db_validity:
            raise RuntimeError("IMAP UIDVALIDITY differs from database")

        live_size = fetch_size(imap, uid)

        if db_size is not None and live_size != db_size:
            raise RuntimeError(
                f"Size mismatch: database={db_size}, IMAP={live_size}"
            )

        if live_size > MAX_MESSAGE_BYTES:
            raise RuntimeError(
                f"Message exceeds 35 MB limit: {live_size} bytes"
            )

        raw_message = fetch_message(imap, uid)

        if len(raw_message) != live_size:
            raise RuntimeError(
                f"Fetched size mismatch: expected={live_size}, "
                f"received={len(raw_message)}"
            )

        message = BytesParser(policy=policy.default).parsebytes(raw_message)

        # Import the tested MIME parser from the bundled module.
        import mime_parser
        parsed = mime_parser.parse_mime(raw_message)

        content_hash = hashlib.sha256(raw_message).hexdigest()
        attachments = parsed["attachments"]

        print("UNIPLEX EMAIL PROCESSOR")
        print("Mode:", "DRY RUN" if args.dry_run else "APPLY")
        print("UID:", uid)
        print("Email ID:", email_id)
        print("Raw message bytes:", len(raw_message))
        print("Subject present:", bool(message["Subject"]))
        print("Body text present:", bool(parsed["body_text"]))
        print("Body HTML present:", bool(parsed["body_html"]))
        print("Content SHA256:", content_hash)
        print("Attachments:", len(attachments))

        for number, attachment in enumerate(attachments, 1):
            print(
                f"{number}. filename={attachment['filename']!r}; "
                f"type={attachment['content_type']}; "
                f"disposition={attachment['content_disposition']}; "
                f"size={attachment['size_bytes']}; "
                f"status={attachment['status']}"
            )

        if args.dry_run:
            print("DATABASE WRITES: NONE")
            print("FILES WRITTEN: NONE")
            return

        processing_error = None
        if any(a["status"] == "needs_review" for a in attachments):
            processing_error = "One or more attachments require review"
        final_status = "needs_review" if processing_error else "processed"

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT body_text, body_html, content_hash
                    FROM mail.emails
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (email_id,)
                )
                existing_email = cur.fetchone()

                if existing_email[2] and existing_email[2] != content_hash:
                    raise RuntimeError(
                        "Message content changed since previous processing"
                    )

                for attachment in attachments:
                    part_id = attachment["mime_part_id"]
                    payload = attachment["payload"]
                    digest = attachment["content_hash"]

                    cur.execute(
                        """
                        SELECT id, content_hash, storage_key
                        FROM mail.attachments
                        WHERE email_id = %s AND mime_part_id = %s
                        """,
                        (email_id, part_id)
                    )
                    existing = cur.fetchone()

                    if existing:
                        attachment_id, old_hash, old_storage_key = existing

                        if old_hash and digest and old_hash != digest:
                            raise RuntimeError(
                                f"Attachment hash mismatch: {part_id}"
                            )

                        if payload is not None and not old_storage_key:
                            path, storage_key = write_attachment(payload)
                            cur.execute(
                                """
                                UPDATE mail.attachments
                                SET storage_key = %s,
                                    content_hash = %s,
                                    size_bytes = %s,
                                    extraction_status = 'pending'
                                WHERE id = %s
                                """,
                                (storage_key, digest, len(payload), attachment_id)
                            )

                    else:
                        storage_key = None

                        if payload is not None:
                            path, storage_key = write_attachment(payload)

                        cur.execute(
                            """
                            INSERT INTO mail.attachments (
                                email_id, filename, content_type,
                                content_disposition, content_id,
                                size_bytes, content_hash, mime_part_id,
                                storage_key, extraction_status,
                                extraction_error
                            )
                            VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s
                            )
                            ON CONFLICT (email_id, mime_part_id) DO NOTHING
                            RETURNING id
                            """,
                            (
                                email_id,
                                attachment["filename"],
                                attachment["content_type"],
                                attachment["content_disposition"],
                                attachment["content_id"],
                                attachment["size_bytes"],
                                digest,
                                part_id,
                                storage_key,
                                attachment["status"],
                                attachment["error"],
                            )
                        )
                        inserted = cur.fetchone()

                        if inserted:
                            attachment_id = inserted[0]
                        else:
                            cur.execute(
                                """
                                SELECT id, content_hash, storage_key
                                FROM mail.attachments
                                WHERE email_id = %s AND mime_part_id = %s
                                """,
                                (email_id, part_id)
                            )
                            existing = cur.fetchone()
                            if not existing:
                                raise RuntimeError(
                                    "Attachment conflict without existing row"
                                )
                            attachment_id, old_hash, old_storage_key = existing
                            if old_hash and digest and old_hash != digest:
                                raise RuntimeError(
                                    f"Attachment hash mismatch: {part_id}"
                                )

                cur.execute(
                    """
                    UPDATE mail.emails
                    SET body_text = %s,
                        body_html = %s,
                        content_hash = %s,
                        processing_status = %s,
                        processing_error = %s,
                        processed_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        parsed["body_text"],
                        parsed["body_html"],
                        content_hash,
                        final_status,
                        processing_error,
                        email_id,
                    )
                )

        print("DATABASE WRITES: COMMITTED")
        print("ATTACHMENT FILES: saved during transaction")
        print("EMAIL STATUS:", final_status)

    except Exception:
        raise

    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
