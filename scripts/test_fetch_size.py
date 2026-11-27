import imaplib
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
    mail.select("INBOX", readonly=True)

    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        raise RuntimeError("No messages returned by SEARCH")

    first_uid = data[0].split()[0]
    print("Testing UID:", first_uid)

    status, records = mail.uid(
        "fetch", first_uid, "(UID RFC822.SIZE)"
    )

    print("FETCH status:", status)
    print("Response type:", type(records).__name__)
    print("Response:", repr(records))

finally:
    try:
        mail.logout()
    except Exception:
        pass
