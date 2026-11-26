"""Prepare and confirm Telegram outbox messages on either host."""
import json

from bot import grade_notification, personal_grades


class ApiError(Exception):
    def __init__(self, code, description="", retry=2):
        self.code = code
        self.description = description
        self.retry = retry
        super().__init__(f"Telegram API, код {code}")


def prepare_delivery(db, row):
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
    return method, params


def finish_delivery(db, row, method, params, result=None, err=None):
    private = row["method"] == "personalGrades"
    if err is not None:
        if err.code == 403:
            with db:
                db.execute("UPDATE viewers SET reachable=0 WHERE id=?", (row["chat"],))
                db.execute("DELETE FROM outbox WHERE chat=?", (row["chat"],))
            return
        if err.code != 400:
            raise err
        if method == "editMessageText" and "message is not modified" not in err.description.lower():
            # A menu might have been deleted before the edit arrived.
            stored = json.loads(row["body"])
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
