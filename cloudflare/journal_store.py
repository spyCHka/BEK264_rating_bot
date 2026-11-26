"""SQLite bridge and resumable journal import for one Telegram bot."""
import json

from pyodide.ffi import jsnull

from bot import setting
from schema import COLUMNS, DDL
from protocol import start_digest, add_digest


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, sql, cursor):
        self.sql = sql
        names = list(cursor.columnNames)
        self.rows = [Row(zip(names, (None if v is jsnull else v for v in values)))
                     for values in cursor.raw().toArray().to_py()]

    def fetchone(self):
        return next(iter(self.rows), None)

    def fetchall(self):
        return self.rows

    @property
    def rowcount(self):
        return self.sql.exec("SELECT changes() AS n").one().n

    @property
    def lastrowid(self):
        return self.sql.exec("SELECT last_insert_rowid() AS id").one().id


class Database:
    def __init__(self, sql):
        self.sql = sql

    def execute(self, statement, args=()):
        # Nanosecond timestamps exceed JS's exact integer range. Bind them as
        # decimal strings and let the SQLite INTEGER column apply its affinity.
        def binding(x):
            if x is None:
                return jsnull
            if isinstance(x, bool):
                return int(x)
            if isinstance(x, int) and abs(x) >= 2**53:
                return str(x)
            return x
        return Cursor(self.sql, self.sql.exec(statement, *(binding(x) for x in args)))

    def __enter__(self):
        # The caller wraps the entire synchronous operation in transactionSync.
        return self

    def __exit__(self, *exc):
        return False


def state(db):
    db.execute("""CREATE TABLE IF NOT EXISTS cf_move (
        id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT, next_chunk INTEGER,
        ready INTEGER, username TEXT, counts TEXT, high_id INTEGER, rolling TEXT, poll_offset INTEGER DEFAULT -1, poll_until INTEGER DEFAULT 0)""")
    return db.execute("SELECT * FROM cf_move WHERE id=1").fetchone()


def import_step(db, bot_id, payload):
    stage = state(db)
    op = payload["op"]
    digest = payload["digest"]
    if stage and stage["digest"] != digest:
        raise ValueError("В хранилище уже есть другой журнал.")
    if op == "begin" and not stage:
        if str(payload["bot_id"]) != bot_id:
            raise ValueError("Копия принадлежит другому боту.")
        counts = payload["counts"]
        if set(counts) != set(COLUMNS) or any(type(n) is not int or n < 0 for n in counts.values()):
            raise ValueError("Неверное описание копии.")
        for sql in DDL:
            db.execute(sql)
        db.execute("INSERT INTO cf_move(id,digest,next_chunk,ready,username,counts,high_id,rolling) VALUES(1,?,0,0,?,?,?,?)",
                   (digest, payload["username"], json.dumps(counts), payload["high_id"],
                    start_digest(bot_id, counts, payload["high_id"])))
        stage = state(db)
    if not stage:
        raise ValueError("Сначала нужно передать описание копии.")
    if op == "chunk" and not stage["ready"]:
        ix = payload["index"]
        if ix > stage["next_chunk"]:
            raise ValueError("Пропущена часть копии.")
        if ix == stage["next_chunk"]:
            table = payload["table"]
            cols = COLUMNS[table]
            insert = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
            for row in payload["rows"]:
                db.execute(insert, row)
            rolling = add_digest(stage["rolling"], table, payload["rows"])
            db.execute("UPDATE cf_move SET next_chunk=next_chunk+1,rolling=? WHERE id=1", (rolling,))
    elif op == "finish" and not stage["ready"]:
        if stage["rolling"] != digest:
            raise ValueError("Контрольная сумма копии не совпала.")
        if payload["chunks"] != stage["next_chunk"] or setting(db, "bot_id") != bot_id or not setting(db, "owner"):
            raise ValueError("Неполный журнал или неверный владелец.")
        for table, expected in json.loads(stage["counts"]).items():
            if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != expected:
                raise ValueError("Количество записей не совпало: " + table)
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Нарушены связи между записями.")
        # Old Telegram buttons must never point at a newly created participant.
        if stage["high_id"] > db.execute("SELECT COALESCE(MAX(id),0) FROM students").fetchone()[0]:
            db.execute("INSERT INTO students(id,name,normalized) VALUES(?,? ,?)",
                       (stage["high_id"], "reserved", "cf-import:" + digest))
            db.execute("DELETE FROM students WHERE id=?", (stage["high_id"],))
        db.execute("CREATE TABLE cf_receipts(id INTEGER PRIMARY KEY, seen INTEGER NOT NULL)")
        db.execute("CREATE INDEX cf_receipts_seen ON cf_receipts(seen)")
        db.execute("UPDATE cf_move SET ready=1,poll_offset=?,poll_until=? WHERE id=1",
                   (int(setting(db, "offset", "-1")), int(setting(db, "last_seen", "0")) + 86400))
    stage = state(db)
    return {"next": stage["next_chunk"], "ready": bool(stage["ready"]), "digest": stage["digest"]}
