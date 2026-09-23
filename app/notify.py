"""Telegram 推播。沒設 token 就靜默略過。背景執行緒送出，不阻塞行情。"""
import json
import os
import queue
import threading
import time
import urllib.request

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_SIM = os.getenv("SIMULATION", "true").lower() != "false"
PREFIX = os.getenv("TELEGRAM_PREFIX", "[模擬]" if _SIM else "[🟡正式]")

_q = queue.Queue()
_last_sent = {}


def enabled():
    return bool(TOKEN and CHAT_ID)


def notify(text, key=None, cooldown=0):
    """key + cooldown：同一 key 在 cooldown 秒內只送一次（避免斷線訊息洗版）。"""
    if not enabled():
        return
    if key and cooldown:
        t = _last_sent.get(key, 0)
        if time.time() - t < cooldown:
            return
        _last_sent[key] = time.time()
    _q.put(f"{PREFIX} {text}")


def _worker():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    while True:
        text = _q.get()
        try:
            data = json.dumps({"chat_id": CHAT_ID, "text": text[:3900]}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"[TG] send failed: {e!r}", flush=True)
        time.sleep(0.5)


if enabled():
    threading.Thread(target=_worker, daemon=True, name="telegram").start()
