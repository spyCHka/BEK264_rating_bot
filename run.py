"""Запуск: python3 run.py. Требуется только стандартная библиотека Python."""
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bot import Journal, connect, grade_notification, personal_grades, setting


class ApiError(Exception):
    def __init__(self, code, description="", retry=2):
        self.code = code
        self.description = description
        self.retry = retry
        super().__init__(f"Telegram API, код {code}")


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **params):
        request = Request(self.base + method, data=json.dumps(params).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=max(20, params.get("timeout", 0) + 10)) as reply:
                result = json.load(reply)
        except HTTPError as err:
            try:
                result = json.loads(err.read())
            except (ValueError, UnicodeError):
                raise ApiError(err.code) from None
        except (URLError, OSError, HTTPException, ValueError):
            raise ApiError(0) from None
        if not result.get("ok"):
            raise ApiError(result.get("error_code", 0), result.get("description", ""),
                           result.get("parameters", {}).get("retry_after", 2))
        return result["result"]

    def lookup_private_chat(self, user_id):
        try:
            return self.call("getChat", chat_id=user_id)
        except ApiError as err:
            if err.code in (401, 404, 409):
                raise
            # The Telegram account picker is available when a private chat
            # can't be queried or the network is temporarily unavailable.
            return None


def acknowledge(api, callback):
    try:
        api.call("answerCallbackQuery", callback_query_id=callback["id"])
    except ApiError as err:
        if err.code == 401:
            raise
        # The persisted response can still be delivered after the button times out.


def deliver(db, api, row):
    stored = json.loads(row["body"])
    params = stored.copy()
    method = row["method"]
    private = method == "personalGrades"
    if row["announcement"]:
        params = grade_notification(db, row["chat"], row["grade_student"], stored["text"])
    elif private:
        params = personal_grades(db, row["chat"], stored["student"], stored["page"])
    if params is None:
        with db:
            db.execute("DELETE FROM outbox WHERE id=? AND chat=? AND method=?",
                       (row["id"], row["chat"], row["method"]))
        return
    if private:
        if "message_id" in stored:
            method = "editMessageText"
            params["message_id"] = stored["message_id"]
        else:
            method = "sendMessage"
            params["protect_content"] = True
    result = None
    try:
        result = api.call(method, chat_id=row["chat"], **params)
    except ApiError as err:
        if err.code == 403:
            with db:
                db.execute("UPDATE viewers SET reachable=0 WHERE id=?", (row["chat"],))
                db.execute("DELETE FROM outbox WHERE chat=?", (row["chat"],))
            return
        if err.code != 400:
            raise
        if method == "editMessageText" and "message is not modified" not in err.description.lower():
            # A menu might have been deleted before the edit arrived.
            stored.pop("message_id", None)
            with db:
                db.execute("UPDATE outbox SET method=?,body=? WHERE id=?",
                           ("personalGrades" if private else "sendMessage", json.dumps(stored), row["id"]))
            return
        if method != "editMessageText":
            print("Telegram отклонил одно сообщение (400). Очередь продолжает работу.", flush=True)
    with db:
        if private and isinstance(result, dict) and result.get("message_id"):
            db.execute("INSERT OR IGNORE INTO grade_views(chat,mid) VALUES(?,?)",
                       (row["chat"], result["message_id"]))
        elif not private and method == "editMessageText":
            db.execute("DELETE FROM grade_views WHERE chat=? AND mid=?", (row["chat"], params["message_id"]))
        db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))


def cycle(journal, api):
    db = journal.db
    # Process short batches so a large group can still use the buttons during a broadcast.
    rows = db.execute("SELECT * FROM outbox ORDER BY announcement,id LIMIT 10").fetchall()
    for row in rows:
        if db.execute("SELECT 1 FROM outbox WHERE id=?", (row["id"],)).fetchone():
            deliver(db, api, row)
            time.sleep(0.05)
    last, active = journal.cursor()
    more = db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone()
    updates = api.call("getUpdates", offset=last + 1 if active else 0,
                       timeout=0 if more else 20, allowed_updates=["message", "callback_query"])
    for update in updates:
        journal.handle(update)
        if update.get("callback_query"):
            acknowledge(api, update["callback_query"])


def with_retry(operation):
    delay = 1
    while True:
        try:
            return operation()
        except ApiError as err:
            if err.code in (401, 404):
                raise SystemExit("Проверь токен у @BotFather и повтори запуск.") from None
            if err.code == 409:
                raise SystemExit("Конфликт подключения. Останови другую копию бота. "
                                 "Если у бота настроен webhook, используй нового бота из @BotFather.") from None
            if err.code and err.code < 500 and err.code != 429:
                raise SystemExit(f"Telegram отклонил запрос (код {err.code}).") from None
            wait = max(1, err.retry) if err.code == 429 else delay
            print(f"Связь с Telegram приостановлена. Повтор через {wait} с.", flush=True)
            time.sleep(wait)
            delay = min(30, 2 * delay)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_journal(db, bot_id):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Файл журнала повреждён.")
    if setting(db, "bot_id") != str(bot_id):
        raise ValueError("Журнал принадлежит другому боту. Проверь токен и папку data.")


def journal_path(bot_id):
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    name = f"journal-{bot_id}.sqlite3"
    if not mount:
        if os.getenv("RAILWAY_SERVICE_ID") or os.getenv("RAILWAY_ENVIRONMENT_ID"):
            raise SystemExit("Подключи постоянный диск Railway к сервису и повтори запуск.")
        local = Path(__file__).resolve().parent / "data"
        local.mkdir(mode=0o700, exist_ok=True)
        return local / name

    volume = Path(mount)
    if not volume.is_absolute() or not volume.is_dir():
        raise SystemExit("Постоянный диск Railway недоступен.")
    target = volume / name
    if target.is_file():
        return target
    digest = os.getenv("GROUPGRADES_IMPORT_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("На диске ещё нет журнала. Выполни перенос через deploy.py на Mac.")
    incoming = volume / f"import-{bot_id}-{digest}.sqlite3"
    if not incoming.is_file():
        raise SystemExit("Файл переноса отсутствует на диске. Проверь выбранный том Railway.")
    return install_snapshot(incoming, target, bot_id, digest)


def install_snapshot(incoming, target, bot_id, digest):
    # Stage a standalone snapshot on the same disk. Publishing it never replaces a live journal.
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        copy = Path(staging) / "journal.sqlite3"
        shutil.copyfile(incoming, copy)
        copy.chmod(0o600)
        if sha256_file(copy) != digest:
            raise SystemExit("Копия журнала загружена не полностью. Повтори перенос.")
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            verify_journal(db, bot_id)
        try:
            os.link(copy, target)
        except FileExistsError:
            pass
    incoming.unlink(missing_ok=True)
    return target


def main(resolve_journal=journal_path):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        if not sys.stdin.isatty():
            raise SystemExit("Задай TELEGRAM_BOT_TOKEN в настройках сервиса.")
        token = getpass.getpass("Вставь токен от @BotFather и нажми Enter (ввод скрыт): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Токен должен быть целиком скопирован из сообщения @BotFather.")
    api = Telegram(token)
    me = with_retry(lambda: api.call("getMe"))
    db_path = resolve_journal(me["id"])
    db = connect(db_path)
    try:
        # Telegram polling uses one running process per bot.
        db.execute("PRAGMA locking_mode=EXCLUSIVE")
        with db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('bot_id', ?)", (str(me['id']),))
        key = secrets.token_urlsafe(24) if not setting(db, "owner") else ""
        username = me["username"]
        journal = Journal(db, username, key, api.lookup_private_chat)
        with_retry(lambda: api.call("setMyCommands", commands=[
            {"command": "start", "description": "Открыть журнал"},
            {"command": "rate", "description": "Оценить одногруппника"},
            {"command": "table", "description": "Рейтинг за всё время и средние баллы за сегодня"},
            {"command": "menu", "description": "Главное меню и уведомления"}
        ]))
        if key:
            print(f"\nЛичная ссылка для владельца (открой лично и нажми «Старт»):\n"
                  f"https://t.me/{username}?start={key}\n", flush=True)
        print(f"Файл журнала: {db_path}\nСсылка для одногруппников: https://t.me/{username}\n"
              "Бот запущен.", flush=True)
        while True:
            with_retry(lambda: cycle(journal, api))
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except sqlite3.OperationalError as err:
        if "locked" in str(err).lower():
            sys.exit("Журнал уже открыт. Останови другую копию бота и повтори запуск.")
        raise
