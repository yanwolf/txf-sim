"""持久化：DATABASE_URL 有設就用 PostgreSQL，否則用 SQLite 檔（DATA_DIR）。

介面刻意很薄，SQL 用 ? 佔位符，Postgres 時自動換成 %s。
所有寫入都經過同一把鎖、失敗自動重連一次；DB 掛了不影響交易主流程，只會 log。
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "/data")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bars (
        ts TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume INTEGER, src TEXT)""",
    """CREATE TABLE IF NOT EXISTS signals (
        id {SERIAL}, t TEXT, side TEXT, price REAL, reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (
        id {SERIAL}, t TEXT, level TEXT, msg TEXT)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, t TEXT, code TEXT, action TEXT, qty INTEGER, price REAL,
        status TEXT, filled_qty INTEGER, avg_price REAL, msg TEXT, reason TEXT, mode TEXT, broker_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS fills (
        id {SERIAL}, t TEXT, order_id TEXT, code TEXT, action TEXT, price REAL, qty INTEGER)""",
    """CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)""",
]


class DB:
    def __init__(self):
        self.lock = threading.RLock()
        self.kind = "postgres" if DATABASE_URL else "sqlite"
        self.conn = None
        self.ok = False
        self.error = None
        self.path = None

    # ---------------------------------------------------------------- 連線
    def connect(self):
        with self.lock:
            try:
                if self.kind == "postgres":
                    import psycopg
                    self.conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)
                else:
                    d = DATA_DIR if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK) else "/tmp"
                    self.path = os.path.join(d, "txf-sim.sqlite3")
                    self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                    self.conn.execute("PRAGMA journal_mode=WAL")
                serial = "SERIAL PRIMARY KEY" if self.kind == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
                for stmt in SCHEMA:
                    self._exec(stmt.replace("{SERIAL}", serial))
                try:   # 舊表補欄位
                    self._exec("ALTER TABLE orders ADD COLUMN broker_id TEXT")
                except Exception:
                    pass
                self.ok, self.error = True, None
            except Exception as e:
                self.ok, self.error = False, repr(e)
            return self.ok

    def _q(self, sql):
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def _exec(self, sql, params=(), fetch=False):
        cur = self.conn.execute(self._q(sql), params)
        return cur.fetchall() if fetch else None

    def run(self, sql, params=(), fetch=False):
        """執行一句 SQL；失敗就重連再試一次，還是失敗回 None。"""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None and not self.connect():
                        return None
                    return self._exec(sql, params, fetch)
                except Exception as e:
                    self.error = repr(e)
                    self.ok = False
                    self.conn = None
                    if attempt == 2:
                        print(f"[DB] {sql[:40]}... failed: {e!r}", flush=True)
                        return None
            return None

    # ---------------------------------------------------------------- 寫入
    def upsert_bar(self, b):
        if self.kind == "postgres":
            self.run("""INSERT INTO bars (ts,open,high,low,close,volume,src) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT (ts) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume,src=EXCLUDED.src""",
                     (b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], b["src"]))
        else:
            self.run("INSERT OR REPLACE INTO bars (ts,open,high,low,close,volume,src) VALUES (?,?,?,?,?,?,?)",
                     (b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], b["src"]))

    def upsert_bars(self, bars):
        for b in bars:
            self.upsert_bar(b)

    def add_signal(self, s):
        self.run("INSERT INTO signals (t,side,price,reason) VALUES (?,?,?,?)",
                 (s["t"], s["side"], s["price"], s["reason"]))

    def add_event(self, e):
        self.run("INSERT INTO events (t,level,msg) VALUES (?,?,?)", (e["t"], e["level"], e["msg"]))

    def upsert_order(self, o):
        cols = ["id", "t", "code", "action", "qty", "price", "status", "filled_qty", "avg_price", "msg", "reason", "mode", "broker_id"]
        vals = tuple(o.get(c) for c in cols)
        if self.kind == "postgres":
            sets = ",".join(f"{c}=EXCLUDED.{c}" for c in cols[1:])
            self.run(f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?'*len(cols))}) "
                     f"ON CONFLICT (id) DO UPDATE SET {sets}", vals)
        else:
            self.run(f"INSERT OR REPLACE INTO orders ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", vals)

    def add_fill(self, f):
        self.run("INSERT INTO fills (t,order_id,code,action,price,qty) VALUES (?,?,?,?,?,?)",
                 (f["t"], f["order_id"], f["code"], f["action"], f["price"], f["qty"]))

    def set_kv(self, k, v):
        s = json.dumps(v, ensure_ascii=False)
        if self.kind == "postgres":
            self.run("INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, s))
        else:
            self.run("INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)", (k, s))

    # ---------------------------------------------------------------- 讀取
    def get_kv(self, k, default=None):
        rows = self.run("SELECT v FROM kv WHERE k=?", (k,), fetch=True)
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except Exception:
            return default

    def load_bars(self, n=600):
        rows = self.run("SELECT ts,open,high,low,close,volume,src FROM bars ORDER BY ts DESC LIMIT ?", (n,), fetch=True) or []
        return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5], "src": r[6]}
                for r in reversed(rows)]

    def load_signals(self, n=200):
        rows = self.run("SELECT t,side,price,reason FROM signals ORDER BY id DESC LIMIT ?", (n,), fetch=True) or []
        return [{"t": r[0], "side": r[1], "price": r[2], "reason": r[3]} for r in rows]

    def load_orders(self, n=100):
        rows = self.run("SELECT id,t,code,action,qty,price,status,filled_qty,avg_price,msg,reason,mode,broker_id "
                        "FROM orders ORDER BY t DESC LIMIT ?", (n,), fetch=True) or []
        keys = ["id", "t", "code", "action", "qty", "price", "status", "filled_qty", "avg_price", "msg", "reason", "mode", "broker_id"]
        return [dict(zip(keys, r)) for r in rows]

    def load_fills_today(self, day):
        rows = self.run("SELECT t,order_id,code,action,price,qty FROM fills WHERE t LIKE ? ORDER BY id", (f"{day}%",), fetch=True) or []
        return [{"t": r[0], "order_id": r[1], "code": r[2], "action": r[3], "price": r[4], "qty": r[5]} for r in rows]

    def trim(self, days=14):
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        self.run("DELETE FROM events WHERE t < ?", (cutoff,))
        self.run("DELETE FROM bars WHERE ts < ?", (cutoff,))

    def status(self):
        return {"kind": self.kind, "ok": self.ok, "error": self.error, "path": self.path}


DB_ = DB()
