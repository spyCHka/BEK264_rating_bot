"""Authenticated Telegram webhook and a private, SQLite-backed group journal."""
import asyncio
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from workers import DurableObject, Response, WorkerEntrypoint, fetch

from bot import Journal
from delivery import ApiError, finish_delivery, prepare_delivery
from journal_store import Database, import_step, state
from protocol import secret


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlsplit(request.url).path
        if request.method == "GET" and path == "/health":
            return Response("BEK264 webhook")
        if request.method != "POST" or path not in ("/telegram", "/migration"):
            return Response("Not found", status=404)
        token = getattr(self.env, "BOT_TOKEN", None)
        if not token:
            return Response("Bot is not configured", status=503)
        header = "X-Telegram-Bot-Api-Secret-Token" if path == "/telegram" else "X-Migration-Key"
        supplied = request.headers.get(header) or ""
        if not supplied.isascii() or not hmac.compare_digest(supplied, secret(token, path)):
            return Response("Forbidden", status=403)
        stub = self.env.JOURNAL.getByName(token.split(":", 1)[0])
        raw = await request.text()
        if path == "/migration":
            result = json.loads(await stub.migrate(raw))
            return Response.json(result, status=409 if "error" in result else 200)
        result = await stub.receive(raw)
        if result is None or result == "pending":
            return Response("Waiting for journal", status=503)
        return Response(result, headers={"Content-Type": "application/json"})


class RatingJournal(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.db = Database(ctx.storage.sql)
        self.lock = asyncio.Lock()

    def atomic(self, fn):
        # Callback returns no Python proxy to JS; keep its result in Python.
        results = []
        self.ctx.storage.transactionSync(lambda: results.append(fn()))
        return results[0]

    async def migrate(self, raw):
        async with self.lock:
            data = json.loads(raw)
            try:
                result = self.atomic(lambda: import_step(self.db, self.env.BOT_TOKEN.split(":")[0], data))
            except (ValueError, KeyError) as err:
                return json.dumps({"error": str(err)})
            return json.dumps(result)

    async def telegram(self, method, **params):
        res = await fetch("https://api.telegram.org/bot" + self.env.BOT_TOKEN + "/" + method,
                          method="POST", headers={"Content-Type": "application/json"}, body=json.dumps(params))
        body = await res.json()
        if not body.get("ok"):
            raise ApiError(body.get("error_code", res.status), body.get("description", ""),
                           body.get("parameters", {}).get("retry_after", 2))
        return body["result"]

    async def receive(self, raw):
        async with self.lock:
            stage = state(self.db)
            if not stage or not stage["ready"]:
                return "pending"
            update = json.loads(raw)
            # Telegram getChat is asynchronous. Resolve a cached @username
            # before the synchronous transaction; the picker remains the fallback.
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            cached = {}
            handle = re.fullmatch(r"(?:(?:/add(?:@\w+)?)\s+)?@([A-Za-z0-9_]{1,32})", text)
            if handle and msg.get("chat", {}).get("type") == "private":
                actor = msg.get("from", {}).get("id")
                known = self.db.execute("""SELECT i.telegram FROM identities i WHERE i.username=? AND
                    (EXISTS(SELECT 1 FROM standings WHERE telegram=? AND vip=1)
                     OR EXISTS(SELECT 1 FROM settings WHERE key='owner' AND value=?))""",
                    (handle.group(1), actor, str(actor))).fetchone()
                if known:
                    try:
                        cached[known[0]] = await self.telegram("getChat", chat_id=known[0])
                    except ApiError:
                        pass
            journal = Journal(self.db, stage["username"], "", cached.get)
            # Arm first: a lost response or evicted isolate must not strand the outbox.
            if not await self.ctx.storage.getAlarm():
                await self.ctx.storage.setAlarm(int(time.time()*1000) + 100)
            def apply_update():
                now = int(time.time())
                uid = update["update_id"]
                if now <= stage["poll_until"] and uid <= stage["poll_offset"]:
                    return
                self.db.execute("DELETE FROM cf_receipts WHERE seen<?", (now - 172800,))
                fresh = self.db.execute("INSERT OR IGNORE INTO cf_receipts VALUES(?,?)", (uid, now))
                if fresh.rowcount:
                    journal.dispatch(update)

            self.atomic(apply_update)
            callback = update.get("callback_query")
            return json.dumps({"method": "answerCallbackQuery", "callback_query_id": callback["id"]}
                              if callback else {"ok": True})

    async def alarm(self, *args):
        async with self.lock:
            # A watchdog also covers transient errors beyond the platform retry window.
            await self.ctx.storage.setAlarm(int(time.time()*1000) + 60000)
            for _ in range(6):
                row = self.db.execute("SELECT * FROM outbox ORDER BY announcement,id LIMIT 1").fetchone()
                if not row:
                    await self.ctx.storage.deleteAlarm()
                    return
                pending = self.atomic(lambda: prepare_delivery(self.db, row))
                if pending is None:
                    continue
                method, params = pending
                try:
                    result = await self.telegram(method, chat_id=row["chat"], **params)
                except ApiError as err:
                    if err.code not in (400, 403):
                        delay = max(err.retry, 60 if err.code == 401 else 3)
                        await self.ctx.storage.setAlarm(int((time.time()+delay)*1000))
                        return
                    self.atomic(lambda: finish_delivery(self.db, row, method, params, err=err))
                else:
                    self.atomic(lambda: finish_delivery(self.db, row, method, params, result=result))
            await self.ctx.storage.setAlarm(int(time.time()*1000) + 1000)
