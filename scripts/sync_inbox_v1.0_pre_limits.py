import argparse
import imaplib
import re
from pathlib import Path
from datetime import timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

import psycopg
from psycopg.types.json import Jsonb

MAIL_ENV = Path("/run/secrets/mail.env")
DB_ENV = Path("/run/secrets/uniplex.env")
BATCH_SIZE = 100


def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
        if dt is None or dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_addresses(message, fields):
    result = []
    for field in fields:
        for name, address in getaddresses(message.get_all(field, [])):
            if address:
                result.append({
                    "type": field.lower(),
                    "name": name,
                    "email": address
                })
    return result


def parse_message(uid, metadata, raw_headers):
    msg = BytesParser(policy=policy.default).parsebytes(raw_headers)

    size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata, re.I)
    date_match = re.search(rb'INTERNALDATE\s+"([^"]+)"', metadata, re.I)

    size_bytes = int(size_match.group(1)) if size_match else None
    internal_date = (
        date_match.group(1).decode("ascii", "replace")
        if date_match else None
    )

    message_id = str(msg["Message-ID"]).strip() if msg["Message-ID"] else None
    in_reply_to = str(msg["In-Reply-To"]).strip() if msg["In-Reply-To"] else None

    references = []
    if msg["References"]:
        references = re.findall(r"<[^>]+>", str(msg["References"]))

    raw_header_list = [
        [name, str(value)]
        for name, value in msg.raw_items()
    ]

    return {
        "uid": uid,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references_header": references or None,
        "subject": str(msg["Subject"]) if msg["Subject"] else None,
        "sender": parse_addresses(msg, ["From"]),
        "recipients": parse_addresses(msg, ["To", "Cc", "Bcc"]),
        "sent_at": parse_datetime(msg["Date"]),
        "received_at": parse_datetime(internal_date),
        "size_bytes": size_bytes,
        "raw_headers": raw_header_list,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.limit < 0:
        raise ValueError("--limit не может быть отрицательным")
    if args.apply and args.limit:
        raise ValueError("--limit разрешён только в режиме dry-run")

    mail_env = load_env(MAIL_ENV)
    db_env = load_env(DB_ENV)

    address = mail_env["YANDEX_EMAIL"]
    password = mail_env["YANDEX_APP_PASSWORD"]

    conn = psycopg.connect(
        host="uniplex-postgres",
        port=5432,
        dbname=db_env["POSTGRES_DB"],
        user=db_env["POSTGRES_USER"],
        password=db_env["POSTGRES_PASSWORD"],
        connect_timeout=10,
    )

    mail = imaplib.IMAP4_SSL("imap.ya.ru", 993)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.uid_validity, f.highest_uid
                FROM mail.mail_folders f
                JOIN mail.mail_accounts a ON a.id = f.account_id
                WHERE a.email_address = %s
                  AND a.provider = 'yandex'
                  AND f.folder_path = 'INBOX'
                  AND a.is_active = true
                """,
                (address,)
            )
            folder = cur.fetchone()

        if not folder:
            raise RuntimeError("Аккаунт или INBOX не найдены в PostgreSQL")

        folder_id, stored_validity, highest_uid = folder

        mail.login(address.split("@", 1)[0], password)

        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Не удалось открыть INBOX")

        response = mail.response("UIDVALIDITY")
        match = (
            re.search(rb"\d+", response[1][0])
            if response and response[1] else None
        )
        current_validity = int(match.group()) if match else None

        if current_validity is None:
            raise RuntimeError("Не удалось получить UIDVALIDITY")

        validity_changed = stored_validity != current_validity
        effective_highest = 0 if validity_changed else highest_uid

        if effective_highest:
            status, data = mail.uid(
                "search", None, "UID", f"{effective_highest + 1}:*"
            )
        else:
            status, data = mail.uid("search", None, "ALL")

        if status != "OK":
            raise RuntimeError("IMAP UID SEARCH завершился ошибкой")

        # Явная фильтрация защищает от граничного UID в ответе сервера.
        uids = sorted(
            uid for uid in (int(x) for x in data[0].split())
            if uid > effective_highest
        )

        if args.limit:
            uids = uids[:args.limit]

        print("UNIPLEX INBOX SYNC")
        print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
        print(f"UIDVALIDITY: {current_validity}")
        print(f"Previous highest UID: {highest_uid}")
        print(f"UIDVALIDITY changed: {'YES' if validity_changed else 'NO'}")
        print(f"Messages to process: {len(uids)}")

        processed = 0
        inserted = 0
        max_uid = effective_highest

        for start in range(0, len(uids), BATCH_SIZE):
            batch = uids[start:start + BATCH_SIZE]
            uid_set = ",".join(map(str, batch))

            status, records = mail.uid(
                "fetch",
                uid_set,
                "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER])"
            )

            if status != "OK":
                raise RuntimeError(
                    f"Ошибка получения пакета UID {batch[0]}–{batch[-1]}"
                )

            parsed = {}

            for record in records:
                if not isinstance(record, tuple) or len(record) < 2:
                    continue

                metadata, raw_headers = record
                if not isinstance(raw_headers, bytes):
                    continue

                uid_match = re.search(rb"\bUID\s+(\d+)", metadata, re.I)
                if not uid_match:
                    continue

                uid = int(uid_match.group(1))
                parsed[uid] = parse_message(uid, metadata, raw_headers)

            if set(parsed) != set(batch):
                raise RuntimeError(
                    f"Неполный ответ IMAP для UID {batch[0]}–{batch[-1]}. "
                    "Пакет не записан."
                )

            processed += len(parsed)

            if args.apply:
                with conn.transaction():
                    with conn.cursor() as cur:
                        for uid in batch:
                            item = parsed[uid]

                            cur.execute(
                                """
                                INSERT INTO mail.emails (
                                    folder_id, uid_validity, uid,
                                    message_id, in_reply_to, references_header,
                                    subject, sender, recipients,
                                    sent_at, received_at, size_bytes, raw_headers
                                )
                                VALUES (
                                    %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s
                                )
                                ON CONFLICT (folder_id, uid_validity, uid)
                                DO NOTHING
                                RETURNING id
                                """,
                                (
                                    folder_id, current_validity, uid,
                                    item["message_id"], item["in_reply_to"],
                                    item["references_header"], item["subject"],
                                    Jsonb(item["sender"]),
                                    Jsonb(item["recipients"]),
                                    item["sent_at"], item["received_at"],
                                    item["size_bytes"], Jsonb(item["raw_headers"])
                                )
                            )

                            row = cur.fetchone()

                            if row:
                                email_id = row[0]
                                inserted += 1
                            else:
                                cur.execute(
                                    """
                                    SELECT id FROM mail.emails
                                    WHERE folder_id = %s
                                      AND uid_validity = %s AND uid = %s
                                    """,
                                    (folder_id, current_validity, uid)
                                )
                                existing = cur.fetchone()

                                if not existing:
                                    raise RuntimeError(
                                        f"Не найдена запись для UID {uid}"
                                    )

                                email_id = existing[0]

                            if item["message_id"]:
                                cur.execute(
                                    """
                                    INSERT INTO mail.email_identity_keys (
                                        email_id, identity_type, identity_value
                                    )
                                    VALUES (%s, 'message_id', %s)
                                    ON CONFLICT DO NOTHING
                                    """,
                                    (email_id, item["message_id"])
                                )

                        max_uid = max(batch)

                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE mail.mail_folders
                            SET uid_validity = %s,
                                highest_uid = %s,
                                last_sync_at = now(),
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (current_validity, max_uid, folder_id)
                        )
            else:
                max_uid = max(batch)

            print(
                f"Batch complete: {batch[0]}–{batch[-1]} "
                f"({len(batch)} messages)"
            )

        if args.apply and not uids:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE mail.mail_folders
                        SET uid_validity = %s,
                            highest_uid = %s,
                            last_sync_at = now(),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (current_validity, effective_highest, folder_id)
                    )

        print("-----")
        print(f"Headers processed: {processed}")
        print(f"New database rows: {inserted}")
        print(f"Highest UID after run: {max_uid}")
        print(f"Database writes: {'YES' if args.apply else 'NONE'}")

    finally:
        try:
            mail.logout()
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    main()
