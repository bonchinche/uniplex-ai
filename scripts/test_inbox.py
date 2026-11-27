import os
import ssl
import imaplib
import email
from email.header import decode_header, make_header
from pathlib import Path

def decode_value(value):
    if not value:
        return "(без темы)"
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value

credentials = {}

for line in Path("/opt/uniplex/.mail.env").read_text().splitlines():
    key, value = line.split("=", 1)
    credentials[key] = value

email_address = credentials["YANDEX_EMAIL"]
username = email_address.split("@", 1)[0]
password = credentials["YANDEX_APP_PASSWORD"]

mail = None

try:
    context = ssl.create_default_context()

    mail = imaplib.IMAP4_SSL(
        "imap.ya.ru",
        993,
        ssl_context=context
    )

    mail.login(username, password)

    status, data = mail.select("INBOX", readonly=True)

    if status != "OK":
        raise RuntimeError("Не удалось открыть папку INBOX")

    uidvalidity = mail.response("UIDVALIDITY")
    print("IMAP LOGIN: SUCCESS")
    print("FOLDER: INBOX (READ-ONLY)")
    print("UIDVALIDITY:", uidvalidity[1])
    print("TOTAL MESSAGES:", data[0].decode())

    status, result = mail.uid("search", None, "ALL")

    if status != "OK":
        raise RuntimeError("Не удалось получить список UID")

    uids = result[0].split()
    print("TOTAL UIDS:", len(uids))

    print("\nПоследние 5 писем:")

    for uid in uids[-5:]:
        status, response = mail.uid(
            "fetch",
            uid,
            "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM MESSAGE-ID)])"
        )

        if status != "OK" or not response:
            print("UID", uid.decode(), "- ошибка чтения заголовков")
            continue

        raw_header = next(
            (item[1] for item in response if isinstance(item, tuple)),
            None
        )

        if not raw_header:
            continue

        msg = email.message_from_bytes(raw_header)

        print("-" * 40)
        print("UID:", uid.decode())
        print("DATE:", decode_value(msg.get("Date")))
        print("FROM:", decode_value(msg.get("From")))
        print("SUBJECT:", decode_value(msg.get("Subject")))
        print("MESSAGE-ID:", msg.get("Message-ID", "(нет)"))

    print("\nINBOX TEST: SUCCESS")

except Exception as error:
    print("INBOX TEST FAILED:", type(error).__name__, str(error))

finally:
    if mail is not None:
        try:
            mail.logout()
        except Exception:
            pass
