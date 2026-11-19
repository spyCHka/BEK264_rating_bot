"""Точка входа бота на Altare."""
import os
import re
import sqlite3
from pathlib import Path

import run

# zipapp keeps the journal and token next to the archive on the server.
ROOT = Path(getattr(__loader__, "archive", None) or __file__).resolve().parent


def server_journal(bot_id):
    data = ROOT / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    target = data / f"journal-{bot_id}.sqlite3"
    if target.is_file():
        return target
    seeds = list((ROOT / "transfer").glob(f"{bot_id}-*.sqlite3"))
    if len(seeds) != 1 or not re.fullmatch(fr"{bot_id}-[0-9a-f]{{64}}", seeds[0].stem):
        raise SystemExit("Для этого бота нет однозначной копии журнала. Проверь токен и архив переноса.")
    source = seeds[0]
    digest = source.stem.split("-", 1)[1]
    return run.install_snapshot(source, target, bot_id, digest)


def start():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        token_file = ROOT / "bot-token.txt"
        token = token_file.read_text().strip() if token_file.is_file() else ""
        if not token:
            raise SystemExit("Задай TELEGRAM_BOT_TOKEN или создай файл bot-token.txt с токеном рядом с main.py.")
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    run.main(server_journal)


if __name__ == "__main__":
    try:
        start()
    except KeyboardInterrupt:
        pass
    except (sqlite3.Error, ValueError, OSError) as error:
        raise SystemExit(f"Не удалось открыть журнал: {error}")
