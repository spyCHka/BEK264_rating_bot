"""Altare startup file. Run beside the existing main.py and data/ directory."""
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
RELEASES = "https://github.com/spyCHka/BEK264_rating_bot/releases"
CHECK_INTERVAL = 120
SHA256 = re.compile(r"[0-9a-f]{64}")
stopping = False


def shutdown(signum, frame):
    global stopping
    stopping = True


def download(url, max_bytes):
    request = Request(url, headers={"User-Agent": "BEK264-rating-bot updater"})
    with urlopen(request, timeout=15) as response:
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Превышен допустимый размер загрузки")
    return content


def save(path, data):
    with tempfile.NamedTemporaryFile(dir=ROOT, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bundle(digest):
    return ROOT / ("bek264-" + digest + ".pyz")


def active_version():
    marker = ROOT / ".bek264-active"
    if marker.is_file():
        digest = marker.read_text().strip()
        if SHA256.fullmatch(digest):
            path = bundle(digest)
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                return path
    return ROOT / "main.py"


def latest():
    manifest = download(RELEASES + "/latest/download/version.txt", 256).decode("ascii")
    tag, digest = manifest.split()
    if not re.fullmatch(r"bot-[0-9]+-[0-9]+", tag) or not SHA256.fullmatch(digest):
        raise ValueError("Некорректный файл версии")
    path = bundle(digest)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        data = download(f"{RELEASES}/download/{tag}/bot.pyz", 8 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Контрольная сумма не совпала")
        save(path, data)
    return path, digest


def start(path):
    return subprocess.Popen([sys.executable, "-u", str(path)], cwd=ROOT)


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def supervise():
    current = active_version()
    child = start(current)
    check_at = 0.0
    rejected = None
    print("Автообновление запущено.", flush=True)
    try:
        while not stopping:
            if time.monotonic() >= check_at:
                check_at = time.monotonic() + CHECK_INTERVAL
                try:
                    candidate, digest = latest()
                except (OSError, ValueError) as error:
                    print(f"Проверка GitHub отложена ({type(error).__name__}).", flush=True)
                else:
                    if not stopping and candidate != current and digest != rejected:
                        stop(child)
                        child = start(candidate)
                        until = time.monotonic() + 12
                        while child.poll() is None and time.monotonic() < until and not stopping:
                            time.sleep(0.25)
                        if stopping:
                            break
                        if child.poll() is None:
                            save(ROOT / ".bek264-active", digest.encode("ascii"))
                            current = candidate
                            print(f"Запущена сборка {digest[:12]}.", flush=True)
                        else:
                            rejected = digest
                            child = start(current)
                            print("Новая сборка завершилась при запуске. Возвращена предыдущая.", flush=True)
            if not stopping and child.poll() is not None:
                return child.returncode or 1
            time.sleep(0.5)
    finally:
        stop(child)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    sys.exit(supervise())
