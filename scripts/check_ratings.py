"""Exercise voting, role checks and upgrades using a local journal and Telegram updates."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot


class RatingChecks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.sqlite3"
        self.clock = "2026-11-19 20:59:59"
        self.calendar = sqlite3.connect(":memory:")
        self.addCleanup(self.calendar.close)
        self.update = 0
        self.open_journal()

    def open_journal(self):
        self.db = bot.connect(self.path)
        self.addCleanup(self.db.close)

        def date_at_test_time(value, modifier):
            value = self.clock if value == "now" else value
            return self.calendar.execute("SELECT date(?,?)", (value, modifier)).fetchone()[0]

        self.db.create_function("date", 2, date_at_test_time)
        self.journal = bot.Journal(self.db, "rating_test_bot", "test-claim")

    def member(self, telegram, vip=False):
        self.send(telegram, "/start")
        with self.db:
            screen = bot.Screen(self.journal, {"id": telegram}, None)
            sid = screen.create_row(telegram, {"first_name": f"User{telegram}"})
            screen.link_profile(sid, telegram)
            self.db.execute("UPDATE students SET vip=? WHERE id=?", (vip, sid))
        return sid

    def send(self, telegram, action):
        self.update += 1
        sender = {"id": telegram, "username": f"user{telegram}"}
        message = {"chat": {"id": telegram, "type": "private"}, "message_id": self.update}
        if action.startswith("/"):
            update = {"message": {**message, "from": sender, "text": action}}
        else:
            update = {"callback_query": {"from": sender, "message": message, "data": action}}
        self.journal.handle({"update_id": self.update, **update})
        row = self.db.execute("""SELECT body FROM outbox WHERE chat=? AND announcement=0
            ORDER BY id DESC LIMIT 1""", (telegram,)).fetchone()
        return json.loads(row[0])

    def stats(self, sid):
        return self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone()

    @staticmethod
    def buttons(body):
        return [b for row in body["reply_markup"]["inline_keyboard"] for b in row]

    def test_midnight_retries_and_history(self):
        target = self.member(10, vip=True)
        other = self.member(20)
        self.send(31, "/start")
        self.send(32, "/start")
        buttons = self.buttons(self.send(31, f"card:{target}"))
        first = next(b["callback_data"] for b in buttons if b["text"] == "1")
        self.assertEqual(first, f"vote:{target}:2026-11-19:1")
        self.send(31, first)
        def announcements():
            return self.db.execute("SELECT COUNT(*) FROM outbox WHERE announcement=1").fetchone()[0]
        count = announcements()
        for grade in (1, 5):
            entry = self.send(31, f"vote:{target}:2026-11-19:{grade}")
            self.assertFalse(any(b["callback_data"].startswith("vote:") for b in self.buttons(entry)))
            self.assertIn("Твоя оценка сегодня: 1", entry["text"])
        self.assertEqual(announcements(), count)
        self.send(32, f"vote:{target}:2026-11-19:5")
        self.send(31, f"vote:{other}:2026-11-19:4")
        self.send(10, f"vote:{target}:2026-11-19:5")
        self.assertEqual(self.stats(target)["mean"], 3)
        self.assertEqual(self.stats(target)["today_n"], 2)
        self.assertEqual(self.stats(other)["mean"], 4)
        self.clock = "2026-11-19 21:00:00"
        self.assertIsNone(self.stats(target)["today_mean"])
        refreshed = self.send(31, first)
        self.send(31, f"vote:{target}:0:5")  # keyboard from the old bot
        self.assertEqual(self.stats(target)["today_n"], 0)
        five = next(b["callback_data"] for b in self.buttons(refreshed) if b["text"] == "5")
        self.send(31, five)
        self.send(32, five)  # same score as yesterday is also a new vote
        self.db.close()
        self.open_journal()
        row = self.stats(target)
        self.assertEqual((row["n"], row["mean"], row["today_n"], row["today_mean"]), (4, 4, 2, 5))
        self.assertEqual(self.stats(other)["today_n"], 0)
        history = bot.personal_grades(self.db, 10, target, 0)["text"]
        self.assertEqual(history.count("19.11.2026"), 2)
        self.assertEqual(history.count("20.11.2026"), 2)
        self.assertEqual(history.count("@user31"), 2)
        self.assertIsNone(bot.personal_grades(self.db, 31, target, 0))

    def test_ranking_across_pages_and_role_privacy(self):
        self.send(100, "/start test-claim")
        ids = {n: self.member(n, vip=n in (11, 12)) for n in range(10, 20)}
        # Rate the 9 last alphabetical members. Ungraded members belong on the last page.
        for n in range(11, 20):
            self.send(10, f"vote:{ids[n]}:2026-11-19:{5 if n == 19 else 3}")
        self.clock = "2026-11-19 21:00:00"
        self.send(10, f"vote:{ids[19]}:2026-11-20:2")
        self.send(10, f"vote:{ids[11]}:2026-11-20:3")
        page = self.send(10, "/table")["text"]
        self.assertIn("1. @user19", page)
        self.assertIn("2. @user11", page)
        self.assertIn("За всё время: 3,5", page)
        self.assertIn("Сегодня: 2", page)
        self.assertIn("9. @user18", self.send(10, "table:1")["text"])
        for body in (page, self.send(10, f"card:{ids[11]}")["text"]):
            self.assertNotIn("VIP", body)
            self.assertNotIn("💎", body)
        pick = self.buttons(self.send(10, "/rate"))
        self.assertEqual([b["text"] for b in pick[:2]], ["@user19", "@user11"])
        for n in (11, 13):
            controls = self.buttons(self.send(12, f"card:{ids[n]}"))
            self.assertIn("Выдать VIP", [b["text"] for b in controls])
            self.assertNotIn("Снять VIP", [b["text"] for b in controls])
        def role(telegram, target, value):
            sid = ids[target]
            return self.send(telegram, f"vip:{sid}:{value}:{self.stats(sid)['revision']}")
        role(12, 13, 1)
        role(10, 14, 1)
        self.assertEqual(self.stats(ids[13])["vip"], 1)
        self.assertEqual(self.stats(ids[14])["vip"], 0)
        for actor in (10, 12, 13):
            role(actor, 13, 0)
            for cmd in ("remove", "confirm"):
                self.send(actor, f"{cmd}:{ids[13]}:{self.stats(ids[13])['revision']}")
            self.assertEqual(self.stats(ids[13])["vip"], 1)
        role(100, 13, 0)
        self.assertEqual(self.stats(ids[13])["vip"], 0)

    def test_returning_user_and_merge_keep_daily_limit(self):
        admin = self.member(11, vip=True)
        target = self.member(12)
        self.send(11, f"vote:{target}:2026-11-19:4")
        self.send(11, f"confirm:{target}:{self.stats(target)['revision']}")
        self.assertIsNone(self.stats(target))
        self.send(11, "/add")
        screen = bot.Screen(self.journal, {"id": 11}, None)
        with self.db:
            screen.shared({"request_id": screen.picker()["request"],
                           "users": [{"user_id": 12, "username": "user12"}]})
        self.send(11, f"vote:{target}:2026-11-19:2")
        self.assertEqual((self.stats(target)["n"], self.stats(target)["mean"]), (1, 4))
        # A confirmed merge keeps the earliest vote on overlapping days and all other days.
        with self.db:
            older = self.db.execute("INSERT INTO students(name,normalized) VALUES('Old','old')").lastrowid
            self.db.executemany("INSERT INTO votes(student,voter,day,grade,changed) VALUES(?,?,?,?,?)",
                                [(older, 11, "2026-11-19", 2, 123), (older, 11, "2026-11-18", 1, 110)])
            screen.link_profile(older, 12, target)
        self.assertEqual(self.stats(older)["n"], 2)
        self.assertEqual(self.stats(older)["today_mean"], 2)
        self.assertEqual(self.stats(older)["mean"], 1.5)
        self.assertIsNotNone(self.stats(admin))

    def test_pre_daily_database(self):
        # Start before the first migration. One grade has no timestamp, the other
        # was recorded by the version that kept only the latest vote per pair.
        self.db.close()
        self.path = self.path.with_name("pre_daily.sqlite3")
        with sqlite3.connect(self.path) as legacy:
            legacy.executescript("""
                CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    normalized TEXT NOT NULL UNIQUE, grade INTEGER, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE votes (student INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    voter INTEGER NOT NULL,grade INTEGER NOT NULL,changed INTEGER NOT NULL,PRIMARY KEY(student,voter));
                CREATE TABLE accounts (student INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
                    telegram INTEGER NOT NULL UNIQUE, invited INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO students(id,name,normalized,grade) VALUES(7,'Old','old',3);
                INSERT INTO accounts(student,telegram) VALUES(7,10);
                INSERT INTO settings VALUES('owner','10');
            """)
            ts = int(datetime(2026, 11, 18, 21, tzinfo=timezone.utc).timestamp()) * 10**9
            legacy.execute("INSERT INTO votes VALUES(7,99,5,?)", (ts,))
        legacy.close()
        self.open_journal()
        self.send(99, "/start")
        self.send(99, "vote:7:2026-11-19:2")
        self.assertEqual((self.stats(7)["n"], self.stats(7)["today_n"], self.stats(7)["mean"]), (2, 1, 4))
        self.assertEqual(self.stats(7)["vip"], 1)
        self.assertEqual(bot.setting(self.db, "owner"), "10")
        self.assertIn("Дата неизвестна", bot.personal_grades(self.db, 10, 7, 0)["text"])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
