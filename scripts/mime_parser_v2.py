import base64
import binascii
import hashlib
from io import BytesIO
from email import policy
from email.generator import BytesGenerator
from email.parser import BytesParser


MAX_MESSAGE_BYTES = 35 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def serialize_message(message):
    output = BytesIO()
    BytesGenerator(output, policy=policy.default).flatten(message)
    return output.getvalue()


def parse_mime(raw_message):
    if len(raw_message) > MAX_MESSAGE_BYTES:
        raise ValueError("Message exceeds 35 MB limit")

    msg = BytesParser(policy=policy.default).parsebytes(raw_message)

    body_text = []
    body_html = []
    attachments = []

    def walk(part, mime_path):
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = part.get("Content-ID")
        transfer_encoding = (
            str(part.get("Content-Transfer-Encoding", ""))
            .strip().lower()
        )

        if content_type == "message/rfc822":
            nested = part.get_payload()

            if isinstance(nested, list):
                payload = b"\n".join(
                    serialize_message(item) for item in nested
                )
            elif isinstance(nested, bytes):
                payload = nested
            else:
                payload = str(nested or "").encode("utf-8")

            if len(payload) > MAX_ATTACHMENT_BYTES:
                add_attachment(
                    mime_path, None, content_type,
                    disposition, filename, content_id,
                    status="needs_review",
                    error="Attached email exceeds 25 MB limit"
                )
            else:
                add_attachment(
                    mime_path, payload, content_type,
                    disposition, filename, content_id
                )
            return

        if part.is_multipart():
            for index, child in enumerate(part.iter_parts(), start=1):
                walk(child, f"{mime_path}.{index}")
            return

        is_attachment = (
            disposition == "attachment"
            or filename is not None
            or content_type not in ("text/plain", "text/html")
        )

        if is_attachment:
            raw_payload = part.get_payload(decode=False)

            if transfer_encoding == "base64":
                normalized = "".join(str(raw_payload or "").split())
                max_encoded = 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3)

                if len(normalized) > max_encoded:
                    add_attachment(
                        mime_path, None, content_type,
                        disposition, filename, content_id,
                        status="needs_review",
                        error="Attachment exceeds 25 MB limit"
                    )
                    return

                try:
                    payload = base64.b64decode(
                        normalized,
                        validate=True
                    )
                except (binascii.Error, ValueError) as error:
                    add_attachment(
                        mime_path, None, content_type,
                        disposition, filename, content_id,
                        status="needs_review",
                        error=f"Invalid Base64: {error}"
                    )
                    return
            else:
                payload = part.get_payload(decode=True)

                if payload is None:
                    payload = b""

            if len(payload) > MAX_ATTACHMENT_BYTES:
                add_attachment(
                    mime_path, None, content_type,
                    disposition, filename, content_id,
                    status="needs_review",
                    error="Attachment exceeds 25 MB limit"
                )
                return

            add_attachment(
                mime_path, payload, content_type,
                disposition, filename, content_id
            )
            return

        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            content = ""

        if content_type == "text/plain":
            body_text.append(content)
        elif content_type == "text/html":
            body_html.append(content)

    def add_attachment(
        mime_path, payload, content_type,
        disposition, filename, content_id,
        status="pending", error=None
    ):
        attachments.append({
            "filename": filename,
            "content_type": content_type,
            "content_disposition": disposition,
            "content_id": (
                str(content_id).strip("<>") if content_id else None
            ),
            "size_bytes": len(payload) if payload is not None else None,
            "content_hash": (
                hashlib.sha256(payload).hexdigest()
                if payload is not None else None
            ),
            "mime_part_id": f"part-{mime_path}",
            "payload": payload,
            "status": status,
            "error": error,
        })

    walk(msg, "1")

    return {
        "subject": str(msg["Subject"]) if msg["Subject"] else None,
        "body_text": "\n".join(body_text) or None,
        "body_html": "\n".join(body_html) or None,
        "attachments": attachments,
    }


if __name__ == "__main__":
    print("MIME parser module loaded successfully")
