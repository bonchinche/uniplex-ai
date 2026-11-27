import importlib.util

spec = importlib.util.spec_from_file_location(
    "sync_v11", "/app/sync_inbox_v1.1.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

headers = (
    b"From: supplier@example.com\r\n"
    b"To: office@example.com\r\n"
    b"Subject: Test message\r\n"
    b"Message-ID: <test@example.com>\r\n"
    b"\r\n"
)

limit = module.MAX_MESSAGE_BYTES

cases = [
    ("Normal", f"1 (UID 1 RFC822.SIZE 1024)".encode(), "new"),
    ("Exactly 35 MB", f"1 (UID 2 RFC822.SIZE {limit})".encode(), "new"),
    ("35 MB + 1", f"1 (UID 3 RFC822.SIZE {limit + 1})".encode(), "needs_review"),
    ("Missing size", b"1 (UID 4)", "needs_review"),
    ("BIGINT overflow", b"1 (UID 5 RFC822.SIZE 999999999999999999999999)", "needs_review"),
]

failed = False

for name, metadata, expected in cases:
    result = module.parse_message(1, metadata, headers)
    actual = result["processing_status"]
    passed = actual == expected

    print(f"{name}: {'PASS' if passed else 'FAIL'}")
    print(f"  Status: {actual}")
    print(f"  Size: {result['size_bytes']}")
    print(f"  Error: {result['processing_error']}")

    if not passed:
        failed = True

print("OVERALL:", "FAIL" if failed else "PASS")

if failed:
    raise SystemExit(1)
