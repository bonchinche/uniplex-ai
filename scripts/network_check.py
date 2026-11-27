from pathlib import Path
import imaplib
import psycopg

def load(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result

mail_env = load("/run/secrets/mail.env")
db_env = load("/run/secrets/uniplex.env")

mail = imaplib.IMAP4_SSL("imap.ya.ru", 993, timeout=20)
mail.login(
    mail_env["YANDEX_EMAIL"].split("@", 1)[0],
    mail_env["YANDEX_APP_PASSWORD"]
)
status, _ = mail.select("INBOX", readonly=True)
assert status == "OK"
print("YANDEX IMAP: AUTHENTICATED, INBOX READ-ONLY")
mail.logout()

conn = psycopg.connect(
    host="uniplex-postgres",
    port=5432,
    dbname=db_env["POSTGRES_DB"],
    user=db_env["POSTGRES_USER"],
    password=db_env["POSTGRES_PASSWORD"],
    connect_timeout=10
)

with conn.cursor() as cur:
    cur.execute("SELECT current_database(), current_user")
    database, role = cur.fetchone()

print("POSTGRES: CONNECTED")
print("Database:", database)
print("Role:", role)

conn.close()
print("NETWORK CHECK: SUCCESS")
