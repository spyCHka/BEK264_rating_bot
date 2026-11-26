"""Copy a private SQLite journal to your Worker. Activate Telegram separately."""
import argparse
from contextlib import closing
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from build import TABLES, connect, schema
from protocol import add_digest, encode, secret, start_digest


def post(url, payload, headers=None):
    req = Request(url, data=encode(payload), headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # The Telegram URL itself contains the token. Never print it or a traceback.
        if exc.code == 409:
            try:
                raise SystemExit(json.load(exc).get("error", "Конфликт при переносе."))
            except (ValueError, AttributeError):
                pass
        raise SystemExit(f"Запрос отклонён (HTTP {exc.code}). Проверь настройки и повтори.") from None
    except OSError:
        raise SystemExit("Соединение прервано. Повтори ту же команду для продолжения.") from None


def chunks(db, columns):
    for table in TABLES:
        cursor = db.execute(f"SELECT {','.join(columns[table])} FROM {table} ORDER BY rowid")
        while rows := cursor.fetchmany(20):
            yield table, [list(row) for row in rows]


def transfer(db, bot_id, username, send):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Проверка целостности копии не прошла.")
    identity = db.execute("SELECT value FROM settings WHERE key='bot_id'").fetchone()
    if not identity or identity[0] != str(bot_id):
        raise ValueError("Эта копия принадлежит другому боту.")
    if not db.execute("SELECT 1 FROM settings WHERE key='owner' AND trim(value)!=''").fetchone():
        raise ValueError("В копии отсутствует владелец.")
    _, columns = schema()
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    high_id = db.execute("SELECT COALESCE(MAX(seq),0) FROM sqlite_sequence WHERE name='students'").fetchone()[0]
    meta = dict(bot_id=str(bot_id), counts=counts, high_id=high_id)
    digest = start_digest(bot_id, counts, high_id)
    for table, rows in chunks(db, columns):
        digest = add_digest(digest, table, rows)
    begin = send(dict(op="begin", digest=digest, username=username, **meta))
    n = 0
    if not begin["ready"]:
        for n, (table, rows) in enumerate(chunks(db, columns), 1):
            if n > begin["next"]:
                reply = send(dict(op="chunk", digest=digest, index=n-1, table=table, rows=rows))
                if reply["next"] != n:
                    raise ValueError("Номер загруженной части не совпал.")
        begin = send(dict(op="finish", digest=digest, chunks=n))
    if not begin["ready"] or begin["digest"] != digest:
        raise ValueError("Перенос не подтверждён.")
    print(f"Журнал проверен. Карточек: {counts['students']}, оценок: {counts['votes']}.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("worker", help="https://<worker>.<your-subdomain>.workers.dev")
    parser.add_argument("--activate", action="store_true", help="Включить webhook после проверки копии.")
    args = parser.parse_args()
    worker = args.worker.rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", worker):
        parser.error("Укажи HTTPS-адрес своего Worker из панели Cloudflare.")
    if not args.database.is_file():
        parser.error("Файл базы не найден.")
    token = os.environ.get("BOT_TOKEN") or getpass.getpass("Токен этого бота из BotFather (ввод скрыт): ").strip()
    if not re.fullmatch(r"[0-9]+:[\w-]+", token, flags=re.ASCII):
        parser.error("Неверный формат токена.")
    def api(method, **params):
        data = post("https://api.telegram.org/bot" + token + "/" + method, params)
        if not data.get("ok"):
            raise ValueError("Telegram отклонил запрос.")
        return data["result"]
    me = api("getMe")
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "copy.sqlite3"
        with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as copy:
                source.backup(copy)
        with closing(connect(backup)) as db:
            transfer(db, me["id"], me["username"], lambda data: post(
                worker + "/migration", data, {"X-Migration-Key": secret(token, "/migration")}))
    if args.activate:
        url = worker + "/telegram"
        api("setWebhook", url=url, secret_token=secret(token, "/telegram"), max_connections=1,
            allowed_updates=["message", "callback_query"], drop_pending_updates=False)
        if api("getWebhookInfo").get("url") != url:
            raise ValueError("Telegram ещё не подтвердил адрес.")
        print("Telegram подтвердил переключение. Проверь /table в своём боте.")


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, ValueError, TypeError) as exc:
        sys.exit(str(exc))
