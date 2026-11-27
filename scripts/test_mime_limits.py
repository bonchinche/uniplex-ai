from email.message import EmailMessage
from test_mime_parser import parse_mime


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def make_large_message():
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Subject"] = "Large attachment test"
    msg.set_content("Test body")

    msg.add_attachment(
        b"X" * (MAX_ATTACHMENT_BYTES + 1),
        maintype="application",
        subtype="octet-stream",
        filename="large.bin",
    )
    return msg.as_bytes()


def make_corrupted_message():
    return (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: Corrupted MIME test\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=test\r\n"
        b"\r\n"
        b"--test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Normal body\r\n"
        b"--test\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment; filename=broken.bin\r\n"
        b"\r\n"
        b"%%%INVALID BASE64%%%\r\n"
        b"--test--\r\n"
    )


def main():
    print("MIME LIMIT DIAGNOSTICS")
    print("----------------------")

    large = parse_mime(make_large_message())
    attachment = large["attachments"][0]

    print("1. Oversized attachment:")
    print(f"   Status: {attachment['status']}")
    print(f"   Size: {attachment['size_bytes']}")
    print(f"   Error: {attachment['error']}")
    print(
        "   Limit enforcement:",
        "PASS" if attachment["status"] == "needs_review"
        and "25 MB" in (attachment["error"] or "")
        else "FAIL",
    )

    corrupted = parse_mime(make_corrupted_message())
    attachment = corrupted["attachments"][0]

    print("2. Corrupted Base64:")
    print(f"   Attachments detected: {len(corrupted['attachments'])}")
    print(f"   Status: {attachment['status']}")
    print(f"   Size: {attachment['size_bytes']}")
    print(f"   Error: {attachment['error']}")
    print(
        "   Strict validation:",
        "PASS" if attachment["status"] == "needs_review"
        and "Invalid Base64" in (attachment["error"] or "")
        else "FAIL",
    )

    print("----------------------")


if __name__ == "__main__":
    main()
