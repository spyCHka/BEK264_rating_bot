"""Wire format shared by the local importer and the Worker."""
import hashlib
import hmac
import json


def encode(data):
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def secret(token, path):
    return hmac.new(token.encode(), ("BEK264/" + path).encode(), hashlib.sha256).hexdigest()


def start_digest(bot_id, counts, high_id):
    data = {"bot_id": str(bot_id), "counts": counts, "high_id": high_id}
    return hashlib.sha256(encode(data)).hexdigest()


def add_digest(previous, table, rows):
    return hashlib.sha256(bytes.fromhex(previous) + encode([table, rows])).hexdigest()
