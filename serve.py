#!/usr/bin/env python3
"""Дашборд радара медиа на этом компьютере.

python3 serve.py            → http://127.0.0.1:8997/
Сбор раз в RADAR_EVERY_MIN минут (по умолчанию 60) и по кнопке «обновить» на странице.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import radar

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("RADAR_PORT", "8997"))
EVERY = int(os.environ.get("RADAR_EVERY_MIN", "60"))
LOCK = threading.Lock()
WAKE = threading.Event()
STATE = {"next": None}


def collect() -> None:
    if not LOCK.acquire(blocking=False):
        return
    try:
        radar.run()
    except BaseException as e:
        radar.write_status(running=False, error=f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        LOCK.release()


def scheduler() -> None:
    while True:
        collect()
        nxt = datetime.now(timezone.utc) + timedelta(minutes=EVERY)
        STATE["next"] = nxt.isoformat()
        radar.write_status(next=STATE["next"], every_min=EVERY)
        WAKE.wait(timeout=EVERY * 60)
        WAKE.clear()


def payload() -> dict:
    status = json.loads(radar.STATUS.read_text(encoding="utf-8")) if radar.STATUS.exists() else {}
    status["running"] = LOCK.locked()
    store = json.loads(radar.STORE.read_text(encoding="utf-8")) if radar.STORE.exists() else {}
    items = [v for v in store.values() if v.get("score", 0) >= 1]
    for v in items:
        v.pop("text", None) if v.get("title") else None
    srcs = [s for s in radar.read_sources(HERE / "sources.txt")]
    return {"status": status, "items": items,
            "sources": [{"name": s["name"], "lang": s["lang"], "tier": s["tier"]} for s in srcs]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/data":
            self.send(200, json.dumps(payload(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/run":
            return self.send(404, b"not found", "text/plain")
        busy = LOCK.locked()
        if not busy:
            WAKE.set()  # будит планировщик: сбор сейчас, следующий через EVERY минут
        self.send(202, json.dumps({"started": not busy, "running": True}).encode(), "application/json")


if __name__ == "__main__":
    radar.DATA.mkdir(exist_ok=True)
    radar.write_status(running=False)
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"радар медиа: http://127.0.0.1:{PORT}/ · сбор раз в {EVERY} мин")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
