import os
import re
import imaplib
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import Path

ENV_FILE = Path("/opt/uniplex/.mail.env")
BATCH_SIZE = 100

def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result

def main():
    env = load_env(ENV_FILE)
    address = env["YANDEX_EMAIL"]
    password = env["YANDEX_APP_PASSWORD"]
    username = address.split("@", 1)[0]

    mail = imaplib.IMAP4_SSL("imap.ya.ru", 993)
    try:
        mail.login(username, password)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Не удалось открыть INBOX в режиме чтения")

        response = mail.response("UIDVALIDITY")
        match = re.search(rb"\d+", response[1][0]) if response and response[1] else None
        uid_validity = int(match.group()) if match else None

        status, data = mail.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP UID SEARCH завершился ошибкой")

        uids = [int(x) for x in data[0].split()]
        message_ids = Counter()
        fetched = 0
        missing_message_id = 0
        fetch_errors = 0
        total_header_bytes = 0

        for start in range(0, len(uids), BATCH_SIZE):
            batch = uids[start:start + BATCH_SIZE]
            uid_set = ",".join(map(str, batch))

            status, records = mail.uid(
                "fetch",
                uid_set,
                "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER])"
            )

            if status != "OK":
                fetch_errors += len(batch)
                continue

            for record in records:
                if not isinstance(record, tuple) or len(record) < 2:
                    continue

                metadata, header_bytes = record
                if not isinstance(header_bytes, bytes):
                    continue

                msg = BytesParser(policy=policy.default).parsebytes(header_bytes)
                message_id = msg.get("Message-ID")

                if message_id:
                    normalized = str(message_id).strip().casefold()
                    message_ids[normalized] += 1
                else:
                    missing_message_id += 1

                fetched += 1
                total_header_bytes += len(header_bytes)

        duplicate_ids = sum(1 for count in message_ids.values() if count > 1)
        duplicate_excess = sum(count - 1 for count in message_ids.values() if count > 1)

        print("IMAP DRY RUN: SUCCESS")
        print(f"Folder: INBOX (READ-ONLY)")
        print(f"UIDVALIDITY: {uid_validity}")
        print(f"Messages found: {len(uids)}")
        print(f"Headers fetched: {fetched}")
        print(f"Unique Message-ID values: {len(message_ids)}")
        print(f"Messages without Message-ID: {missing_message_id}")
        print(f"Message-ID values appearing more than once: {duplicate_ids}")
        print(f"Additional messages with repeated Message-ID: {duplicate_excess}")
        print(f"Header fetch errors: {fetch_errors}")
        print(f"Total header data: {total_header_bytes} bytes")
        print(f"First UID: {min(uids) if uids else 'none'}")
        print(f"Last UID: {max(uids) if uids else 'none'}")
        print("Database writes: NONE")

        if fetched + fetch_errors < len(uids):
            print("WARNING: Some messages were not accounted for in the fetch response.")

    finally:
        try:
            mail.logout()
        except Exception:
            pass

if __name__ == "__main__":
    main()
