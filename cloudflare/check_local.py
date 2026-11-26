"""Exercise the Python Worker, its persistent SQL and Telegram delivery on loopback."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from build import HERE, build, connect
from transfer import transfer
from protocol import secret


TOKEN = "12345:offline-test"
BASE = "http://127.0.0.1:8877"
CLIENT = build_opener(ProxyHandler({}))
sent = []


class Telegram(BaseHTTPRequestHandler):
    def do_POST(self):
        params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sent.append((self.path.rsplit("/", 1)[-1], params))
        body = json.dumps({"ok": True, "result": {"message_id": len(sent)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def call(path, data, key=None):
    header = "X-Migration-Key" if path == "/migration" else "X-Telegram-Bot-Api-Secret-Token"
    req = Request(BASE + path, data=json.dumps(data).encode(),
                  headers={header: key if key is not None else secret(TOKEN, path), "Content-Type": "application/json"})
    with CLIENT.open(req, timeout=15) as result:
        return json.load(result)


def require_status(code, action):
    try:
        action()
    except HTTPError as exc:
        assert exc.code == code, exc.code
    else:
        raise AssertionError(f"Expected HTTP {code}")


def send_update(actor, update_id, data, button=False):
    sender = {"id": actor, "username": "student" + str(actor)}
    message = {"chat": {"id": actor, "type": "private"}, "message_id": 40}
    event = ({"callback_query": {"from": sender, "message": message, "id": "callback", "data": data}}
             if button else {"message": {**message, "from": sender, "text": data}})
    return call("/telegram", {"update_id": update_id, **event})


def wait_for(test, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for the local Worker")


def check(directory):
    owner, member = 5000000001, 5000000002
    today = datetime.now(timezone(timedelta(hours=3))).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    db = connect(":memory:")
    try:
        with db:
            db.executemany("INSERT INTO settings VALUES(?,?)", [("owner", str(owner)), ("bot_id", "12345")])
            for i, person in enumerate((owner, member), start=1):
                db.execute("INSERT INTO viewers(id) VALUES(?)", (person,))
                db.execute("INSERT INTO students(id,name,normalized,vip) VALUES(?,?,?,?)",
                           (i, f"User {person}", chr(0)+str(person), int(i == 1)))
                db.execute("INSERT INTO accounts(student,telegram) VALUES(?,?)", (i, person))
            db.execute("INSERT INTO votes VALUES(2,?,?,4,1794632100883123456)", (owner, yesterday))
        require_status(403, lambda: call("/telegram", {}, "bad"))
        require_status(503, lambda: call("/telegram", {}))
        for _ in range(2):
            transfer(db, 12345, "test_bot", lambda data: call("/migration", data))
        assert send_update(owner, 10, "/table") == {"ok": True}
        assert send_update(owner, 12, f"vote:2:{today}:5", True)["method"] == "answerCallbackQuery"
        send_update(owner, 11, f"revote:2:{today}:3", True)  # distinct out-of-order update
        send_update(owner, 12, f"vote:2:{today}:5", True)    # replay
        send_update(owner, 13, f"revote:2:{yesterday}:1", True)
        send_update(owner, 14, "/mygrades")

        def personal_delivery():
            return any(params.get("protect_content") and "Кто меня оценил" in params.get("text","")
                       for method, params in sent)
        wait_for(personal_delivery, 25)
        assert any("Рейтинг группы" in p.get("text","") for _, p in sent)
        def corrections():
            return [p for _, p in sent if "<b>Оценка исправлена</b>" in p.get("text", "")]
        wait_for(lambda: len(corrections()) >= 2, 25)
        notices = corrections()
        assert {p["chat_id"] for p in notices} == {owner, member}
        assert len(notices) == 2
        for file in directory.rglob("*.sqlite"):
            with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as state:
                if state.execute("SELECT 1 FROM sqlite_schema WHERE name='votes'").fetchone():
                    assert state.execute("SELECT grade,changed FROM votes WHERE day=?", (yesterday,)).fetchone() == (4,1794632100883123456)
                    assert state.execute("SELECT grade FROM votes WHERE day=?", (str(today),)).fetchone() == (3,)
                    assert state.execute("SELECT count(*) FROM votes").fetchone() == (2,)
                    assert state.execute("SELECT normalized FROM students WHERE id=1").fetchone() == (chr(0)+str(owner),)
                    break
        else:
            raise AssertionError("No local journal")
    finally:
        db.close()


def main():
    build()
    with tempfile.TemporaryDirectory() as temp, ThreadingHTTPServer(("127.0.0.1", 0), Telegram) as fake:
        root = Path(temp)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        shutil.copytree(HERE / "dist", root / "dist")
        shutil.copy(HERE / "wrangler.toml", root)
        (root / ".dev.vars").write_text("BOT_TOKEN=" + TOKEN)
        entry = root / "dist" / "entry.py"
        entry.write_text(entry.read_text().replace("https://api.telegram.org/bot",
                               f"http://127.0.0.1:{fake.server_port}/bot"))
        command = os.environ.get("WRANGLER_BIN", "wrangler")
        with (root / "runtime.log").open("w+") as log:
            proc = subprocess.Popen([command, "dev", "--local", "--ip", "127.0.0.1", "--port", "8877"],
                                    cwd=root, stdout=log, stderr=subprocess.STDOUT)
            def ready():
                if proc.poll() is not None:
                    raise RuntimeError("Wrangler exited before starting")
                try:
                    with CLIENT.open(BASE + "/health", timeout=1) as response:
                        return response.status == 200
                except (OSError, URLError):
                    return False
            try:
                wait_for(ready, 140)
                check(root)
            except Exception:
                log.seek(0)
                print(log.read()[-9000:], file=sys.stderr)
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                fake.shutdown()
    print("Local Worker: migration, roles, rating edits, delivery and replay passed.")


if __name__ == "__main__":
    main()
