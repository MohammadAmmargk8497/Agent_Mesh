"""The Worker's SQLite model, serialized by one condition/mutex."""

from collections import deque
import hashlib
import hmac
import os
import sqlite3
import threading
import time

from .config import TOKEN_KEYS, secure_directory

AGENTS = ("agent-a", "agent-b")
DAY = 86_400_000
MAX_WAIT_S = 25
MAX_WAITERS_PER_AGENT = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
  assignee TEXT NOT NULL, created_by TEXT NOT NULL, status TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL, result TEXT, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
  text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL, acked_at INTEGER
);
CREATE TABLE IF NOT EXISTS patches (
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, title TEXT NOT NULL,
  text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, type TEXT NOT NULL,
  ref TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
  ref TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_recipient ON events(recipient, id);
"""


def now_ms():
    return time.time_ns() // 1_000_000


class RateLimiter:
    """A sliding 120 requests/60 seconds window, shared by each identity/IP."""

    def __init__(self):
        self.buckets = {}
        self.lock = threading.Lock()
        self.last_cleanup = 0.0

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            if now - self.last_cleanup >= 60:
                self.buckets = {k: q for k, q in self.buckets.items()
                                if q and q[-1] > now - 60}
                self.last_cleanup = now
            bucket = self.buckets.setdefault(key, deque())
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= 120:
                return False
            bucket.append(now)
            return True


class State:
    def __init__(self, config_dir, config):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.waiters = {agent: 0 for agent in AGENTS}
        self.limiter = RateLimiter()
        self.token_hashes = [
            (role, hashlib.sha256(token.strip().encode()).digest())
            for role, key in zip((*AGENTS, "viewer"), TOKEN_KEYS)
            for token in config[key].split(",") if len(token.strip()) >= 32
        ]
        path = secure_directory(config_dir) / "data.db"
        if path.is_symlink():
            raise ValueError("database must not be a symbolic link")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.stop = threading.Event()
        self.sweep()
        self.retention_thread = threading.Thread(target=self._retention, daemon=True,
                                                 name="agentmesh-retention")
        self.retention_thread.start()

    def identify(self, token):
        presented = hashlib.sha256(token.encode()).digest()
        for role, candidate in self.token_hashes:
            if hmac.compare_digest(presented, candidate):
                return role
        return None

    def rows(self, query, *args):
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def last_event_id(self, agent):
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE recipient=?",
                               (agent,)).fetchone()[0]

    def emit(self, recipient, actor, kind, ref):
        if recipient == actor:
            return
        self.db.execute("INSERT INTO events (recipient,type,ref,actor,created_at) VALUES (?,?,?,?,?)",
                        (recipient, kind, ref, actor, now_ms()))
        self.condition.notify_all()

    def sweep(self, now=None):
        now = now_ms() if now is None else now
        with self.lock:
            removed = {}
            for table, predicate, cutoff in (
                ("events", "created_at < ?", now - 7 * DAY),
                ("audit", "created_at < ?", now - 7 * DAY),
                ("messages", "acked_at IS NOT NULL AND acked_at < ?", now - 30 * DAY),
                ("patches", "created_at < ?", now - 30 * DAY),
                ("tasks", "status IN ('accepted','cancelled') AND updated_at < ?", now - 90 * DAY),
            ):
                removed[table] = self.db.execute(
                    f"DELETE FROM {table} WHERE {predicate}", (cutoff,)).rowcount
            return removed

    def _retention(self):
        while not self.stop.wait(DAY / 1000):
            self.sweep()

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        self.retention_thread.join()
        with self.lock:
            self.db.close()
