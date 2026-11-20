"""Взаимные оценки группы в Telegram."""
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from html import escape

PAGE_SIZE = 8
TABLE = "📋 Таблица оценок"
BACK = [("Главное меню", "home")]
ADD = [("＋ Добавить одногруппника", "add")]
MANAGE = [("Управление участниками", "viplist:0")]
MY_GRADES = [("Кто меня оценил", "received:0")]


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS viewers (
            id INTEGER PRIMARY KEY, notify INTEGER NOT NULL DEFAULT 1,
            reachable INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            normalized TEXT NOT NULL UNIQUE, grade INTEGER CHECK(grade BETWEEN 1 AND 5),
            revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, method TEXT NOT NULL,
            body TEXT NOT NULL, announcement INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS outbox_chat ON outbox(chat);
        CREATE TABLE IF NOT EXISTS accounts (
            student INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
            telegram INTEGER NOT NULL UNIQUE, invited INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS votes (
            student INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            voter INTEGER NOT NULL, grade INTEGER NOT NULL CHECK(grade BETWEEN 1 AND 5),
            changed INTEGER NOT NULL, PRIMARY KEY(student,voter)
        );
        CREATE TABLE IF NOT EXISTS invitations (
            student INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE
        );
    """)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not setting(db, "peer_grades"):
            # Voter zero preserves legacy grades if the original owner record is missing.
            db.execute("""INSERT INTO votes(student,voter,grade,changed)
                SELECT id,?,grade,1 FROM students WHERE grade IS NOT NULL""",
                       (int(setting(db, "owner", "0")),))
            db.execute("UPDATE students SET grade=NULL")
            set_value(db, "peer_grades", "1")
        if not setting(db, "member_add_vip"):
            db.execute("ALTER TABLE viewers ADD COLUMN adding INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE students ADD COLUMN added_by INTEGER")
            db.execute("ALTER TABLE students ADD COLUMN vip INTEGER NOT NULL DEFAULT 0 CHECK(vip IN (0,1))")
            if setting(db, "adding"):
                db.execute("UPDATE viewers SET adding=1 WHERE id=?", (setting(db, "owner"),))
            db.execute("DELETE FROM settings WHERE key='adding'")
            db.execute("DROP VIEW IF EXISTS standings")
            db.execute("""CREATE VIEW standings AS
                SELECT s.id,s.name,s.normalized,s.added_by,s.vip,a.telegram,s.revision,
                       COUNT(v.grade) AS n,AVG(v.grade) AS mean
                FROM students s LEFT JOIN accounts a ON a.student=s.id
                LEFT JOIN votes v ON v.student=s.id GROUP BY s.id""")
            set_value(db, "member_add_vip", "1")
        if not setting(db, "vip_management"):
            db.execute("ALTER TABLE invitations ADD COLUMN merge_from INTEGER REFERENCES students(id) ON DELETE SET NULL")
            db.execute("""UPDATE students SET vip=1 WHERE id IN
                (SELECT student FROM accounts WHERE telegram=?)""", (setting(db, "owner"),))
            # These invitations predate VIP moderation and could have been
            # issued to an ordinary member when VIP was only a badge.
            db.execute("DELETE FROM invitations WHERE student IN (SELECT id FROM students WHERE vip=1)")
            set_value(db, "vip_management", "1")
        if not setting(db, "verified_usernames"):
            db.execute("""CREATE TABLE identities (
                telegram INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE)""")
            db.execute("""CREATE TABLE pickers (
                chat INTEGER PRIMARY KEY, request INTEGER NOT NULL, expected TEXT NOT NULL,
                target INTEGER REFERENCES students(id) ON DELETE CASCADE, grant_vip INTEGER NOT NULL)""")
            db.execute("ALTER TABLE outbox ADD COLUMN role_menu INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE invitations ADD COLUMN grant_vip INTEGER NOT NULL DEFAULT 0")
            db.execute("DELETE FROM invitations")
            db.execute("UPDATE viewers SET adding=0")
            db.execute("DROP VIEW standings")
            db.execute("""CREATE VIEW standings AS SELECT s.id,s.name,s.normalized,s.added_by,s.vip,
                a.telegram,i.username,s.revision,COUNT(v.grade) AS n,AVG(v.grade) AS mean
                FROM students s LEFT JOIN accounts a ON a.student=s.id
                LEFT JOIN identities i ON i.telegram=a.telegram
                LEFT JOIN votes v ON v.student=s.id GROUP BY s.id""")
            set_value(db, "verified_usernames", "1")
        if not setting(db, "personal_grades"):
            db.execute("ALTER TABLE identities ADD COLUMN full_name TEXT")
            db.execute("""CREATE TABLE grade_views(
                chat INTEGER NOT NULL, mid INTEGER NOT NULL, PRIMARY KEY(chat,mid))""")
            set_value(db, "personal_grades", "1")
        if not setting(db, "targeted_notifications"):
            db.execute("""ALTER TABLE outbox ADD COLUMN grade_student INTEGER
                REFERENCES students(id) ON DELETE CASCADE""")
            # Older broadcasts have no target ID, so they cannot be safely routed.
            db.execute("DELETE FROM outbox WHERE announcement=1")
            old_notice = r"\nС VIP получатель может увидеть, какой балл ты поставил."
            db.execute("UPDATE outbox SET body=replace(body, ?, '') WHERE instr(body, ?)>0",
                       (old_notice, old_notice))
            db.execute("""CREATE VIEW grade_subscribers AS
                SELECT v.id AS chat, a.student,
                       (COALESCE(s.vip,0)=1 OR v.id=(SELECT CAST(value AS INTEGER)
                           FROM settings WHERE key='owner')) AS vip
                FROM viewers v LEFT JOIN accounts a ON a.telegram=v.id
                LEFT JOIN students s ON s.id=a.student
                WHERE v.notify=1 AND v.reachable=1""")
            set_value(db, "targeted_notifications", "1")
        if not setting(db, "daily_votes"):
            db.execute("ALTER TABLE students ADD COLUMN listed INTEGER NOT NULL DEFAULT 1")
            db.execute("DROP VIEW standings")
            db.execute("""CREATE TABLE daily_votes (
                student INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                voter INTEGER NOT NULL, day TEXT NOT NULL,
                grade INTEGER NOT NULL CHECK(grade BETWEEN 1 AND 5), changed INTEGER NOT NULL,
                PRIMARY KEY(student,voter,day))""")
            # changed=1 is the old import marker, not a real vote timestamp.
            db.execute("""INSERT INTO daily_votes(student,voter,day,grade,changed)
                SELECT student,voter,CASE WHEN changed=1 THEN ''
                    ELSE date(changed/1000000000,'unixepoch','+3 hours') END,grade,changed
                FROM votes""")
            db.execute("DROP TABLE votes")
            db.execute("ALTER TABLE daily_votes RENAME TO votes")
            db.execute("""CREATE VIEW standings AS
                WITH clock(today) AS (SELECT date('now','+3 hours'))
                SELECT s.id,s.name,s.normalized,s.added_by,s.vip,a.telegram,i.username,s.revision,
                    clock.today,COUNT(v.grade) AS n,AVG(v.grade) AS mean,
                    AVG(CASE WHEN v.day=clock.today THEN v.grade END) AS today_mean,
                    COUNT(CASE WHEN v.day=clock.today THEN v.grade END) AS today_n
                FROM students s CROSS JOIN clock LEFT JOIN accounts a ON a.student=s.id
                LEFT JOIN identities i ON i.telegram=a.telegram LEFT JOIN votes v ON v.student=s.id
                WHERE s.listed=1 GROUP BY s.id""")
            # Older messages still waiting for Telegram may contain the old public badge.
            db.execute("UPDATE outbox SET body=replace(body, ' · 💎 VIP', '') WHERE instr(body, ' · 💎 VIP')>0")
            set_value(db, "daily_votes", "1")
    return db


def setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_value(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))


def keyboard(rows):
    return {"inline_keyboard": [
        [{"text": label, "callback_data": action} for label, action in row] for row in rows
    ]}


def payload(text, rows):
    result = {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if rows:
        result["reply_markup"] = keyboard(rows)
    return result


def telegram_name(user):
    name = " ".join(" ".join((user.get("first_name") or "", user.get("last_name") or "")).split())
    return "".join(c for c in name if unicodedata.category(c) not in {"Cc", "Cf"})[:64] or None


def grade_notification(db, chat, student, text):
    recipient = db.execute("""SELECT vip,student FROM grade_subscribers
        WHERE chat=? AND (student=? OR vip=1)""", (chat, student)).fetchone()
    if not recipient:
        return None
    rows = []
    if recipient["vip"] and recipient["student"] == student:
        rows.append(MY_GRADES)
    rows.append([(TABLE, "table:0")])
    return payload(text, rows)


def personal_grades(db, chat, student, page):
    # Resolve both the current VIP role and the recipient at delivery time.
    # The queued request has no voter identities or pre-rendered private text.
    roster = db.execute("SELECT n FROM standings WHERE telegram=? AND id=? AND vip=1",
                        (chat, student)).fetchone()
    if not roster:
        return None
    pages = max(1, (roster["n"] + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    votes = db.execute("""SELECT v.voter,v.grade,v.day,i.username,COALESCE(i.full_name,s.name) AS full_name
        FROM votes v LEFT JOIN identities i ON i.telegram=v.voter
        LEFT JOIN accounts a ON a.telegram=v.voter LEFT JOIN students s ON s.id=a.student
        WHERE v.student=? ORDER BY v.changed DESC,v.voter LIMIT ? OFFSET ?""",
        (student, PAGE_SIZE, page * PAGE_SIZE)).fetchall()
    text = "<b>Кто меня оценил</b>"
    if not votes:
        text += "\n\nТебе пока не поставили ни одной оценки."
    else:
        text += "\nВсе полученные оценки по датам (МСК)"
    for vote in votes:
        if vote["username"]:
            author = "@" + escape(vote["username"])
        elif vote["voter"]:
            name = escape(vote["full_name"] or "Профиль участника")
            author = f'<a href="tg://user?id={vote["voter"]}">{name}</a>'
        else:
            author = "Автор прежней оценки неизвестен"
        date_label = ".".join(reversed(vote["day"].split("-"))) if vote["day"] else "Дата неизвестна"
        text += f"\n\n{date_label} · {author} · <b>{vote['grade']} из 5</b>"
    rows = []
    if pages > 1:
        text += f"\n\nСтраница {page+1} из {pages}"
        nav = []
        if page:
            nav.append(("‹ Назад", f"received:{page-1}"))
        if page+1 < pages:
            nav.append(("Далее ›", f"received:{page+1}"))
        rows.append(nav)
    return payload(text, rows + [[("↻ Обновить", f"received:{page}")], BACK])


class Journal:
    def __init__(self, db, username, claim_key, lookup_chat=None):
        self.db = db
        self.username = username
        self.claim_key = claim_key
        self.lookup_chat = lookup_chat

    def handle(self, update):
        """Keep input progress, grade changes and outgoing messages in one transaction."""
        db = self.db
        with db:
            last, active = self.cursor()
            if active and update["update_id"] <= last:
                return
            callback = update.get("callback_query")
            msg = callback.get("message", {}) if callback else update.get("message", {})
            chat = msg.get("chat", {})
            sender = (callback or msg).get("from", {})
            if chat.get("type") == "private" and chat.get("id") == sender.get("id") and not sender.get("is_bot"):
                screen = Screen(self, sender, msg.get("message_id") if callback else None)
                screen.remember(sender["id"], sender.get("username"), telegram_name(sender))
                if callback:
                    screen.action(callback.get("data", ""))
                elif "users_shared" in msg and not msg.get("forward_origin") and not msg.get("via_bot"):
                    screen.shared(msg["users_shared"])
                else:
                    screen.message(msg.get("text", ""))
            set_value(db, "offset", update["update_id"])
            set_value(db, "last_seen", int(time.time()))

    def cursor(self):
        active = time.time() - int(setting(self.db, "last_seen", "0")) < 6 * 86400
        return int(setting(self.db, "offset", "-1")), active


class Screen:
    def __init__(self, journal, sender, message_id):
        self.journal = journal
        self.db = journal.db
        self.sender = sender
        self.chat = sender["id"]
        self.mid = message_id

    @property
    def owner(self):
        return setting(self.db, "owner") == str(self.chat)

    @property
    def is_vip(self):
        if self.owner:
            return True
        return self.db.execute("""SELECT 1 FROM accounts a JOIN students s ON s.id=a.student
            WHERE a.telegram=? AND s.vip=1""", (self.chat,)).fetchone() is not None

    def vip_required(self):
        self.close_picker()
        self.home("Управлять участниками и выдавать VIP могут участники со статусом VIP.")

    def show(self, text, rows, role_menu=False):
        body = payload(text, rows)
        method = "sendMessage"
        if self.mid:
            method = "editMessageText"
            body["message_id"] = self.mid
        self.db.execute("INSERT INTO outbox (chat,method,body,role_menu) VALUES (?,?,?,?)",
                        (self.chat, method, json.dumps(body, ensure_ascii=False), role_menu))

    def send_keyboard(self, text, markup):
        body = payload(text, [])
        body["reply_markup"] = markup
        self.db.execute("INSERT INTO outbox(chat,method,body) VALUES (?,'sendMessage',?)",
                        (self.chat, json.dumps(body, ensure_ascii=False)))

    def remember(self, telegram, username, full_name=None):
        if username:
            self.db.execute("UPDATE identities SET username=NULL WHERE username=? AND telegram!=?", (username, telegram))
        self.db.execute("""INSERT INTO identities(telegram,username,full_name) VALUES(?,?,?)
            ON CONFLICT(telegram) DO UPDATE SET username=excluded.username,
            full_name=COALESCE(excluded.full_name,identities.full_name)""", (telegram, username, full_name))

    def viewer(self):
        return self.db.execute("SELECT * FROM viewers WHERE id=?", (self.chat,)).fetchone()

    def account(self, telegram=None):
        telegram = self.chat if telegram is None else telegram
        return self.db.execute("""SELECT a.student,s.listed FROM accounts a JOIN students s ON s.id=a.student
            WHERE a.telegram=?""", (telegram,)).fetchone()

    def home(self, intro="", role_menu=False):
        viewer = self.viewer()
        if not viewer:
            self.show("Нажми /start, чтобы присоединиться к журналу группы.", [])
            return
        rows = [[("⭐ Оценить одногруппника", "pick:0")], [(TABLE, "table:0")]]
        vip = self.is_vip
        if vip:
            rows.extend([MY_GRADES, ADD, MANAGE])
        notify = viewer["notify"]
        rows.append([("🔔 Выключить уведомления" if notify else "🔕 Включить уведомления",
                      "mute" if notify else "unmute")])
        text = ("📘 <b>Оценки группы</b>\n\n" + (intro + "\n\n" if intro else "") +
                "Выбери одногруппника и поставь балл от 1 до 5. В таблице видны "
                "общий средний балл и средний за сегодня. Каждого можно оценить один раз в день. "
                "Следующая оценка доступна с 00:00 по Москве.")
        if self.owner:
            text += f"\n\nСсылка для группы: https://t.me/{self.journal.username}"
        status = "включены" if notify else "выключены"
        about = "об оценках всей группы" if vip else "о полученных тобой оценках"
        self.show(text + f"\n\nУведомления {about} {status}.", rows, role_menu=role_menu)

    def notify_role(self, telegram, text):
        recipient = Screen(self.journal, {"id": telegram}, None)
        if not recipient.is_vip:
            self.db.execute("DELETE FROM outbox WHERE chat=? AND method='personalGrades'", (telegram,))
            for view in self.db.execute("SELECT mid FROM grade_views WHERE chat=?", (telegram,)).fetchall():
                Screen(self.journal, {"id": telegram}, view["mid"]).show("Раздел с авторами оценок закрыт.", [BACK])
        viewer = recipient.viewer()
        if viewer and viewer["reachable"]:
            self.db.execute("DELETE FROM outbox WHERE chat=? AND role_menu=1", (telegram,))
            recipient.home(text, role_menu=True)

    def register(self, key=""):
        self.db.execute("""INSERT INTO viewers(id) VALUES (?)
            ON CONFLICT(id) DO UPDATE SET reachable=1""", (self.chat,))
        if (self.journal.claim_key and not setting(self.db, "owner") and key.isascii()
                and secrets.compare_digest(key, self.journal.claim_key)):
            set_value(self.db, "owner", self.chat)
        current = self.account()
        if self.owner and not current and self.sender.get("username"):
            sid = self.create_row(self.chat, self.sender)
            self.db.execute("UPDATE students SET vip=1 WHERE id=?", (sid,))
            self.link_profile(sid, self.chat)
            return "Ты владелец бота. Тебе доступно управление участниками и VIP-статусами."
        if self.owner and current:
            self.db.execute("UPDATE students SET vip=1,listed=1 WHERE id=? AND (vip=0 OR listed=0)", (current["student"],))
            return "Твой постоянный VIP активен. Кнопки управления доступны ниже."
        if self.owner:
            return "Твой VIP активен. Для собственной строки в таблице укажи username в Telegram и нажми /start."
        if current and not current["listed"]:
            current = None
        if key.startswith("join_") and not current:
            return "Попроси VIP открыть твою прежнюю карточку и выбрать твой аккаунт в Telegram."
        if not current:
            return "Ты можешь голосовать. Чтобы другие могли оценить тебя, попроси VIP добавить твой @username."
        if self.is_vip:
            return "Твой статус VIP активен. Кнопки управления уже доступны."
        return ""

    def create_row(self, telegram, user):
        name = (telegram_name(user) or "Участник")[:48]
        # Old text keys could never contain a control character. The new key is
        # independent of aliases, which Telegram users may change or transfer.
        key = chr(0) + str(telegram)
        return self.db.execute("INSERT INTO students(name,normalized,added_by) VALUES(?,?,?)",
                               (name, key, self.chat)).lastrowid

    def link_profile(self, sid, telegram, source=None):
        # VIPs can merge a duplicate from an earlier version without losing votes.
        if source is not None:
            self.db.execute("""UPDATE students SET vip=1 WHERE id=? AND EXISTS
                (SELECT 1 FROM students WHERE id=? AND vip=1)""", (sid, source))
            self.db.execute("""INSERT INTO votes(student,voter,day,grade,changed)
                SELECT ?,voter,day,grade,changed FROM votes WHERE student=?
                ON CONFLICT(student,voter,day) DO UPDATE SET
                    grade=excluded.grade, changed=excluded.changed
                WHERE excluded.changed < votes.changed""", (sid, source))
            self.db.execute("DELETE FROM students WHERE id=?", (source,))
        self.db.execute("DELETE FROM votes WHERE student=? AND voter=?", (sid, telegram))
        self.db.execute("INSERT INTO accounts(student,telegram,invited) VALUES (?,?,1)", (sid, telegram))
        if str(telegram) == setting(self.db, "owner"):
            self.db.execute("UPDATE students SET vip=1 WHERE id=?", (sid,))
        self.db.execute("DELETE FROM invitations WHERE student=?", (sid,))
        self.db.execute("UPDATE students SET revision=revision+1 WHERE id=?", (sid,))
        return self.db.execute("SELECT name FROM students WHERE id=?", (sid,)).fetchone()[0]

    def merge_source(self, sid):
        return self.db.execute("""SELECT s.id,s.name,a.telegram,i.grant_vip,d.username FROM invitations i
            JOIN students s ON s.id=i.merge_from JOIN accounts a ON a.student=s.id
            LEFT JOIN identities d ON d.telegram=a.telegram
            WHERE i.student=?""", (sid,)).fetchone()

    def message(self, text):
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower() if parts else ""
        if cmd == "/start":
            note = self.register(parts[1] if len(parts) == 2 else "")
            self.close_picker()
            self.home(note)
        elif not self.viewer():
            self.home()
        elif cmd == "/mygrades":
            self.close_picker()
            self.received(0)
        elif cmd == "/add":
            expected = parts[1].strip() if len(parts) == 2 else ""
            self.begin_add(expected=expected)
        elif cmd in ("/table", "/rate", "/menu", "/cancel"):
            self.close_picker()
            if cmd == "/table":
                self.page("table", 0)
            elif cmd == "/rate":
                self.page("pick", 0)
            else:
                self.home()
        elif self.picker() and not text.startswith("/"):
            req = self.picker()
            self.begin_add(target=req["target"], grant=req["grant_vip"], expected=text.strip())
        else:
            self.close_picker()
            self.home()

    def picker(self):
        return self.db.execute("SELECT * FROM pickers WHERE chat=?", (self.chat,)).fetchone()

    def close_picker(self, note="Выбор аккаунта завершён."):
        if self.db.execute("DELETE FROM pickers WHERE chat=?", (self.chat,)).rowcount:
            self.send_keyboard(note, {"remove_keyboard": True})

    def begin_add(self, target=None, grant=False, expected="", verify_known=True):
        if not self.is_vip:
            self.vip_required()
            return
        typed = expected.strip()
        valid = re.fullmatch(r"@[A-Za-z0-9_]{1,32}", typed)
        handle = typed[1:] if valid else ""
        request = secrets.randbelow(2**31 - 1) + 1
        self.db.execute("INSERT OR REPLACE INTO pickers(chat,request,expected,target,grant_vip) VALUES(?,?,?,?,?)",
                        (self.chat, request, handle, target, int(grant)))
        if verify_known and handle and self.journal.lookup_chat:
            known = self.db.execute("SELECT telegram FROM identities WHERE username=?", (handle,)).fetchone()
            if known:
                info = self.journal.lookup_chat(known["telegram"])
                if info and info.get("type") == "private" and info.get("id") == known["telegram"]:
                    self.remember(info["id"], info.get("username"), telegram_name(info))
                    if (info.get("username") or "").casefold() == handle.casefold():
                        self.finish_add(self.picker(), info["id"], info)
                        return
        label = "Выбрать аккаунт в Telegram"
        if typed and not valid:
            text = "Введи username в формате @username или выбери аккаунт кнопкой ниже."
        elif handle:
            text = f"Подтверди @{handle}: нажми «{label}» и выбери этого человека."
        else:
            text = f"Нажми «{label}» и выбери человека с @username или пришли его @username в этот чат."
        markup = {"keyboard": [[{"text": label, "request_users": {"request_id": request,
                  "user_is_bot": False, "max_quantity": 1, "request_name": True, "request_username": True}}],
                  [{"text": "/cancel"}]], "resize_keyboard": True, "one_time_keyboard": True}
        self.send_keyboard(text, markup)

    def shared(self, selection):
        if not self.is_vip:
            self.vip_required()
            return
        req = self.picker()
        if not req or selection.get("request_id") != req["request"]:
            self.home("Этот выбор аккаунта устарел. Открой добавление ещё раз.")
            return
        users = selection.get("users", [])
        user = users[0] if len(users) == 1 else {}
        telegram = user.get("user_id")
        handle = user.get("username") or ""
        if (type(telegram) is not int or not 0 < telegram < 2**52 or user.get("is_bot")
                or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", handle)):
            self.show("У выбранного аккаунта нужен публичный @username. Выбери участника ещё раз.", [])
            self.begin_add(req["target"], req["grant_vip"], "@" + req["expected"] if req["expected"] else "",
                           verify_known=False)
            return
        if req["expected"] and req["expected"].casefold() != handle.casefold():
            self.show(f"Выбран @{handle}, а нужен @{req['expected']}. Выбери нужный аккаунт.", [])
            self.begin_add(req["target"], req["grant_vip"], "@" + req["expected"], verify_known=False)
            return
        self.remember(telegram, handle, telegram_name(user))
        self.finish_add(req, telegram, user)

    def finish_add(self, req, telegram, user):
        sid = req["target"]
        current = self.account(telegram)
        self.close_picker()
        if sid is not None:
            target = self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone()
            if not target or target["telegram"] is not None:
                self.home("Карточку уже изменили. Выбери участника заново.")
                return
            if current:
                if str(telegram) == setting(self.db, "owner"):
                    self.card(target, "За владельцем уже закреплена собственная карточка.")
                    return
                self.db.execute("""INSERT INTO invitations(student,token,merge_from,grant_vip) VALUES(?,?,?,?)
                    ON CONFLICT(student) DO UPDATE SET merge_from=excluded.merge_from,grant_vip=excluded.grant_vip""",
                    (sid, secrets.token_urlsafe(24), current["student"], req["grant_vip"]))
                self.card(target, "У аккаунта уже есть карточка. Проверь обе записи перед объединением.")
                return
        elif current:
            sid = current["student"]
            if current["listed"]:
                self.card(self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone(),
                          "Этот аккаунт уже в таблице.")
                return
            self.db.execute("UPDATE students SET listed=1,revision=revision+1 WHERE id=?", (sid,))
        else:
            sid = self.create_row(telegram, user)
        if not current:
            self.link_profile(sid, telegram)
        if req["grant_vip"]:
            self.db.execute("UPDATE students SET vip=1,revision=revision+1 WHERE id=?", (sid,))
        student = self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone()
        if student["vip"]:
            self.notify_role(telegram, "Тебе выдан VIP. Кнопки управления доступны ниже.")
        self.card(student, "Аккаунт подтверждён. VIP выдан." if req["grant_vip"] else "Аккаунт подтверждён и добавлен в таблицу.")

    def action(self, data):
        if not self.viewer():
            self.home()
            return
        parts = data.split(":")
        cmd = parts[0]
        self.close_picker()
        if cmd == "home":
            self.home()
        elif cmd == "received" and len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            self.received(int(parts[1]))
        elif cmd in ("mute", "unmute"):
            self.db.execute("UPDATE viewers SET notify=? WHERE id=?", (cmd == "unmute", self.chat))
            if cmd == "mute":
                self.db.execute("DELETE FROM outbox WHERE chat=? AND announcement=1", (self.chat,))
            self.home()
        elif cmd == "viplist" and not self.is_vip:
            self.vip_required()
        elif cmd in ("table", "pick", "viplist") and len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            self.page(cmd, int(parts[1]))
        elif cmd == "add":
            self.begin_add()
        elif cmd in ("card", "vote", "invite", "bind", "remove", "confirm", "grade", "vip", "merge", "keep") and len(parts) >= 2:
            self.student_action(cmd, parts)
        else:
            self.home()

    def page(self, mode, page):
        where = " WHERE telegram IS NOT ?" if mode == "pick" else ""
        args = (self.chat,) if mode == "pick" else ()
        total = self.db.execute("SELECT COUNT(*) FROM standings" + where, args).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages-1)
        students = self.db.execute("SELECT * FROM standings" + where + " ORDER BY mean DESC, lower(COALESCE(username,name)),id LIMIT ? OFFSET ?",
                                   (*args, PAGE_SIZE, page * PAGE_SIZE)).fetchall()
        rows = []
        if mode != "table":
            text = "<b>Кому поставить оценку?</b>" if mode == "pick" else "<b>Управление участниками</b>\nВыбери участника."
            rows.extend([[(display_name(s), f"card:{s['id']}")] for s in students])
            if not students:
                text += "\n\nДругих участников пока нет. VIP могут добавить одногруппников в таблицу."
        else:
            text = "📋 <b>Рейтинг группы</b>\nПо среднему за всё время · оценки от 1 до 5 · день по Москве"
            for rank, s in enumerate(students, start=page * PAGE_SIZE + 1):
                text += f"\n\n<b>{rank}. {escape(display_name(s))}</b>\n{summary(s)}"
            if not students:
                text += "\n\nВ журнале пока пусто."
        if pages > 1:
            text += f"\n\nСтраница {page + 1} из {pages}"
        nav = []
        if page:
            nav.append(("‹ Назад", f"{mode}:{page-1}"))
        if page + 1 < pages:
            nav.append(("Далее ›", f"{mode}:{page+1}"))
        if nav:
            rows.append(nav)
        if mode == "table":
            rows.extend([[("↻ Обновить", f"table:{page}")], [("⭐ Оценить одногруппника", "pick:0")]])
        if mode in ("pick", "viplist") and self.is_vip:
            rows.append(ADD)
        self.show(text, rows + [BACK])

    def own_vote(self, sid, day):
        return self.db.execute("SELECT grade FROM votes WHERE student=? AND voter=? AND day=?",
                               (sid, self.chat, day)).fetchone()

    def received(self, page):
        if not self.is_vip:
            self.home("Список авторов полученных оценок доступен с VIP-статусом.")
            return
        account = self.account()
        if not account:
            self.home("Для просмотра полученных оценок нужна твоя карточка в таблице.")
            return
        request = {"student": account["student"], "page": page}
        if self.mid and self.db.execute("SELECT 1 FROM grade_views WHERE chat=? AND mid=?",
                                        (self.chat, self.mid)).fetchone():
            request["message_id"] = self.mid
        self.db.execute("INSERT INTO outbox(chat,method,body) VALUES(?,'personalGrades',?)",
                        (self.chat, json.dumps(request)))

    def card(self, student, note=""):
        sid = student["id"]
        name = escape(display_name(student))
        text = f"<b>{name}</b>\n{summary(student)}"
        rows = []
        if student["telegram"] != self.chat:
            today = student["today"]
            mine = self.own_vote(sid, today)
            if mine:
                text += (f"\nТвоя сегодняшняя оценка {name} — {mine['grade']} из 5. "
                         f"Следующая возможность поставить новую оценку {name} — с 00:00 МСК.")
                rows.append([("↻ Обновить карточку", f"card:{sid}")])
            else:
                text += "\nТвоя оценка за сегодня:"
                rows.append([(str(i), f"vote:{sid}:{today}:{i}") for i in range(1, 6)])
        if self.is_vip and student["telegram"] is None:
            source = self.merge_source(sid)
            if source:
                label = "@" + source["username"] if source["username"] else source["name"]
                text += f"\n\nЕсть запрос на объединение с карточкой <b>{escape(label)}</b>."
                if source["grant_vip"]:
                    text += " После объединения участник получит VIP."
                rows.append([("Объединить карточки", f"merge:{sid}:{source['id']}"),
                             ("Оставить отдельно", f"keep:{sid}:{source['id']}")])
            else:
                rows.append([("Привязать @username", f"bind:{sid}")])
        if self.is_vip:
            if not self.is_owner_card(student):
                revoke = self.owner and student["vip"]
                rows.append([("Снять VIP" if revoke else "Выдать VIP",
                              f"vip:{sid}:{0 if revoke else 1}:{student['revision']}")])
                rows.append([("Убрать из таблицы", f"remove:{sid}:{student['revision']}")])
            rows.append(MANAGE)
        rows.extend([[("Выбрать другого", "pick:0")], [(TABLE, "table:0")], BACK])
        self.show((note + "\n\n" if note else "") + text, rows)

    def is_owner_card(self, student):
        return str(student["telegram"]) == setting(self.db, "owner")

    def student_action(self, cmd, parts):
        student = self.db.execute("SELECT * FROM standings WHERE id=?", (parts[1],)).fetchone()
        if not student:
            self.show("Этой записи уже нет в таблице.", [[("К списку", "pick:0")], BACK])
            return
        sid = student["id"]
        if cmd == "vote" and len(parts) == 4 and parts[3] in ("1", "2", "3", "4", "5"):
            self.vote(student, parts[2], int(parts[3]))
        elif cmd in ("vip", "remove", "confirm", "invite", "bind", "merge", "keep") and not self.is_vip:
            self.vip_required()
        elif cmd in ("vip", "remove", "confirm") and self.is_owner_card(student):
            self.card(student, "За владельцем закреплены карточка и статус VIP.")
        elif cmd == "vip" and len(parts) == 4 and parts[2] in ("1", "0") and parts[3] == str(student["revision"]):
            value = int(parts[2])
            if not value and not self.owner:
                self.card(student, "Снять статус с действующего участника может владелец бота.")
                return
            if value and student["telegram"] is None:
                self.begin_add(target=sid, grant=True)
                return
            changed = self.db.execute("UPDATE students SET vip=?, revision=revision+1 WHERE id=? AND vip!=?",
                                      (value, sid, value)).rowcount
            note = "Статус VIP выдан." if value else "Статус VIP снят."
            if changed and student["telegram"] is not None:
                self.notify_role(student["telegram"], "Тебе выдан VIP. Кнопки управления доступны ниже."
                                 if value else "Твой VIP снят. Ниже твоё текущее меню.")
            updated = self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone()
            self.card(updated, note)
        elif cmd in ("merge", "keep") and student["telegram"] is None:
            source = self.merge_source(sid)
            if source and parts[2:] == [str(source["id"])] and not self.is_owner_card(source):
                if cmd == "merge":
                    if source["grant_vip"]:
                        self.db.execute("UPDATE students SET vip=1 WHERE id=?", (sid,))
                    self.link_profile(sid, source["telegram"], source["id"])
                    self.notify_role(source["telegram"], "Твои карточки объединены. Ниже твоё текущее меню.")
                else:
                    self.db.execute("DELETE FROM invitations WHERE student=?", (sid,))
            self.card(self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone())
        elif cmd in ("bind", "invite") and student["telegram"] is None:
            self.begin_add(target=sid)
        elif cmd in ("remove", "confirm") and parts[2:] == [str(student["revision"])]:
            if student["vip"] and not self.owner:
                self.card(student, "Убрать эту карточку может только администратор.")
                return
            if cmd == "remove":
                question = f"Убрать <b>{escape(display_name(student))}</b> из таблицы? История оценок сохранится."
                if student["vip"]:
                    question += " Участник также потеряет VIP-права."
                self.show(question,
                          [[("Убрать", f"confirm:{sid}:{student['revision']}"), ("Оставить", f"card:{sid}")]])
            else:
                self.db.execute("UPDATE students SET listed=0,vip=0,revision=revision+1 WHERE id=?", (sid,))
                self.db.execute("DELETE FROM invitations WHERE student=? OR merge_from=?", (sid, sid))
                self.db.execute("DELETE FROM pickers WHERE target=?", (sid,))
                self.db.execute("DELETE FROM outbox WHERE grade_student=?", (sid,))
                if student["vip"] and student["telegram"] is not None:
                    self.notify_role(student["telegram"], "Твою карточку убрали из таблицы. Ниже твоё текущее меню.")
                self.show("Запись убрана из таблицы.", [[(TABLE, "table:0")], BACK])
        else:
            # Old single-owner keyboards open the current card on upgrade.
            self.card(student)

    def vote(self, student, day, grade):
        sid = student["id"]
        if student["telegram"] == self.chat:
            self.show("Это твой профиль. Выбери другого участника.", [[("К списку", "pick:0")], BACK])
            return
        if day != student["today"]:
            self.card(student, "Карточка обновлена на сегодня.")
            return
        inserted = self.db.execute("""INSERT INTO votes(student,voter,day,grade,changed)
            VALUES (?,?,?,?,?) ON CONFLICT(student,voter,day) DO NOTHING""",
            (sid, self.chat, day, grade, time.time_ns())).rowcount
        if not inserted:
            self.card(student, "Сегодня ты уже оценил этого участника.")
            return
        self.db.execute("UPDATE students SET revision=revision+1 WHERE id=?", (sid,))
        updated = self.db.execute("SELECT * FROM standings WHERE id=?", (sid,)).fetchone()
        text = (f"📘 <b>Новая оценка</b>\n\n{escape(display_name(updated))}  ·  {grade} из 5\n"
                f"Теперь в таблице: {summary(updated)}")
        self.card(updated, "✓ Твоя оценка учтена.")
        self.db.execute("""INSERT INTO outbox(chat,method,body,announcement,grade_student)
            SELECT chat,'sendMessage',?,1,? FROM grade_subscribers
            WHERE student=? OR vip=1""", (json.dumps({"text": text}, ensure_ascii=False), sid, sid))


def display_name(student):
    return "@" + student["username"] if student["username"] else student["name"]


def summary(student):
    def average(mean, count):
        if mean is None:
            return "—"
        score = f"{mean:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{score} из 5 · оценок: {count}"

    overall = average(student["mean"], student["n"])
    today = average(student["today_mean"], student["today_n"])
    return f"За всё время: {overall}\nСегодня: {today}"
