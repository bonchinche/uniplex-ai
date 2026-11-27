import imaplib
import re
from pathlib import Path

env = {}
for line in Path("/run/secrets/mail.env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")

mail = imaplib.IMAP4_SSL("imap.ya.ru", 993)

try:
    username = env["YANDEX_EMAIL"].split("@", 1)[0]
    mail.login(username, env["YANDEX_APP_PASSWORD"])

    select_status, select_data = mail.select("INBOX", readonly=True)
    print("SELECT status:", select_status)
    print("SELECT response:", repr(select_data))
    print("EXISTS:", repr(mail.response("EXISTS")))
    print("UIDVALIDITY:", repr(mail.response("UIDVALIDITY")))

    if select_status != "OK":
        raise RuntimeError("Cannot select INBOX")

    search_status, search_data = mail.uid("search", None, "ALL")
    print("SEARCH status:", search_status)
    print("SEARCH response:", repr(search_data))

    if search_status != "OK":
        raise RuntimeError("UID SEARCH failed")

    uids = search_data[0].split() if search_data and search_data[0] else []
    print("UID count:", len(uids))

    sizes = []

    for start in range(0, len(uids), 100):
        batch = uids[start:start + 100]
        status, records = mail.uid(
            "fetch", b",".join(batch), "(UID RFC822.SIZE)"
        )
        if status != "OK":
            raise RuntimeError("FETCH failed")

        for record in records:
            if isinstance(record, tuple):
                raw = record[0]
            elif isinstance(record, bytes):
                raw = record
            else:
                continue

            uid = re.search(rb"\bUID\s+(\d+)", raw)
            size = re.search(rb"\bRFC822.SIZE\s+(\d+)", raw)

            if uid and size:
                sizes.append((int(uid.group(1)), int(size.group(1))))

    total = sum(size for _, size in sizes)

    print("MAILBOX SIZE CHECK")
    print("Messages with sizes:", len(sizes))
    print("Total size: %.2f MB" % (total / 1024 / 1024))
    print("Largest message: %.2f MB" % (
        max((size for _, size in sizes), default=0) / 1024 / 1024
    ))
    print("Top 10 largest messages (UID, MB):")

    for uid, size in sorted(sizes, key=lambda item: item[1], reverse=True)[:10]:
        print(uid, "%.2f" % (size / 1024 / 1024))

finally:
    try:
        mail.logout()
    except Exception:
        pass
