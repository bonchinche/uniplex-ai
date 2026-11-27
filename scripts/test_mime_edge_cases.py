from email.message import EmailMessage
from test_mime_parser import parse_mime


def make_message():
    outer = EmailMessage()
    outer["From"] = "sender@example.com"
    outer["To"] = "recipient@example.com"
    outer["Subject"] = "UNIPLEX edge cases"

    outer.set_content("Outer message body.")

    # Кириллическое имя файла
    outer.add_attachment(
        b"PDF TEST",
        maintype="application",
        subtype="pdf",
        filename=("utf-8", "", "спецификация.pdf"),
    )

    # Inline-изображение
    outer.add_attachment(
        b"IMAGE ONE",
        maintype="image",
        subtype="png",
        filename="logo.png",
        disposition="inline",
        cid="<duplicate-id@example.com>",
    )

    # Второе изображение с тем же Content-ID
    outer.add_attachment(
        b"IMAGE TWO",
        maintype="image",
        subtype="png",
        filename="logo-copy.png",
        disposition="inline",
        cid="<duplicate-id@example.com>",
    )

    # Вложенное письмо
    inner = EmailMessage()
    inner["From"] = "inner@example.com"
    inner["Subject"] = "Forwarded message"
    inner.set_content("This text belongs to the attached email.")

    outer.add_attachment(
        inner,
        filename="forwarded.eml",
    )

    return outer.as_bytes()


def main():
    result = parse_mime(make_message())
    attachments = result["attachments"]

    print("MIME EDGE CASE DIAGNOSTICS")
    print("--------------------------")

    print(
        "1. Cyrillic filename:",
        "FOUND" if any(
            item["filename"] == "спецификация.pdf"
            for item in attachments
        ) else "ISSUE",
    )

    inline_items = [
        item for item in attachments
        if item["filename"] in ("logo.png", "logo-copy.png")
    ]

    print(
        "2. Inline images detected:",
        len(inline_items),
        "(expected 2)",
    )

    part_ids = [item["mime_part_id"] for item in inline_items]
    print(
        "3. Duplicate Content-ID handling:",
        "UNIQUE" if len(set(part_ids)) == len(part_ids) else "ISSUE: COLLISION",
    )

    print(
        "4. Nested email detected:",
        "FOUND" if any(
            item["filename"] == "forwarded.eml"
            for item in attachments
        ) else "ISSUE",
    )

    body = result["body_text"] or ""
    print(
        "5. Nested email text isolated:",
        "YES" if "This text belongs to the attached email." not in body
        else "ISSUE: LEAKED INTO OUTER BODY",
    )

    print("--------------------------")
    print("Attachments:", len(attachments))

    for item in attachments:
        print(
            f"- {item['filename']!r}; "
            f"{item['content_type']}; "
            f"{item['size_bytes']} bytes; "
            f"part={item['mime_part_id']}"
        )


if __name__ == "__main__":
    main()
