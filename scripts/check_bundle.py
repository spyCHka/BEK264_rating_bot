"""Smoke-test the packaged entry point and journal without contacting Telegram."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# sitecustomize runs in the child interpreter before the zipapp. Supply a fake
# Telegram transport so the full startup path can open a real, temporary journal.
STUB = '''
import sys
sys.path.insert(0, sys.argv[0])
import run
class FakeTelegram:
    def __init__(self, token):
        assert token == "42:test"
    def call(self, method, **kwargs):
        if method == "getMe":
            return {"id": 42, "username": "test_bot"}
        assert method == "setMyCommands"
        return True
    def lookup_private_chat(self, *args):
        raise AssertionError("Unexpected chat lookup")
run.Telegram = FakeTelegram
def finish(journal, api):
    raise SystemExit(0)
run.cycle = finish
'''


def check(source):
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        archive = home / "bot.pyz"
        shutil.copyfile(source, archive)
        (home / "data").mkdir()
        journal = home / "data/journal-42.sqlite3"
        with sqlite3.connect(journal) as db:
            db.execute("CREATE TABLE retained (value TEXT)")
            db.execute("INSERT INTO retained VALUES ('before deploy')")
        (home / "sitecustomize.py").write_text(STUB)
        (home / "bot-token.txt").write_text("42:test")
        env = {**os.environ, "PYTHONPATH": str(home), "TELEGRAM_BOT_TOKEN": ""}
        for _ in range(2):
            result = subprocess.run([sys.executable, str(archive)], cwd=home.parent,
                                    env=env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=15)
            if result.returncode or "Бот запущен." not in result.stdout:
                raise SystemExit("Не прошла проверка запуска собранного бота.")
        with sqlite3.connect(journal) as db:
            assert db.execute("SELECT value FROM retained").fetchone() == ("before deploy",)
            assert db.execute("SELECT value FROM settings WHERE key='bot_id'").fetchone() == ("42",)


if __name__ == "__main__":
    check(Path(sys.argv[1]))
