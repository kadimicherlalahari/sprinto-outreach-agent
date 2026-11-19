"""Persistence. Real data -> Neon Postgres (NEON_DATABASE_URL). Mock data -> a separate SQLite file
under data/mock/ whose rows are all flagged is_mock. The two are never mixed."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import DATA_DIR, ROOT, Settings
from .util import new_id, now_iso

MIGRATIONS_DIR = ROOT / "migrations"
MOCK_DB_PATH = DATA_DIR / "mock" / "MOCK_FAKE_DATA.sqlite3"


class StorageNotConfigured(RuntimeError):
    pass


class Store:
    """Thin SQL wrapper. `dialect` is 'postgres' or 'sqlite'. SQL is written with %s placeholders."""

    def __init__(self, conn: Any, dialect: str, is_mock: bool, label: str, reconnect: Any = None):
        self.conn = conn
        self._reconnect = reconnect
        self.dialect = dialect
        self.is_mock = is_mock
        self.label = label
        self._lock = threading.RLock()
        self._writes = 0            # bumped on every write; invalidates the display snapshot cache
        self._snap: Any = None      # (writes, monotonic time, data) for read-only dashboard views
        self._tx = 0                # >0 inside transaction(): statements are not committed one by one
        self._local = threading.local()   # per-request read memo for page rendering (see page_reads)
        self._plans: Dict[str, List[Any]] = {}

    # ---------- low level ----------
    def _sql(self, sql: str) -> str:
        return sql.replace("%s", "?") if self.dialect == "sqlite" else sql

    def _run(self, sql: str, params: Iterable[Any], fetch: bool) -> Any:
        """Execute once; on a dropped Postgres connection (e.g. Neon idle timeout) reconnect and retry once."""
        for attempt in (0, 1):
            try:
                cur = self.conn.cursor()
                cur.execute(self._sql(sql), tuple(params))
                if fetch:
                    cols = [d[0] for d in cur.description] if cur.description else []
                    out = [dict(zip(cols, r)) for r in cur.fetchall()]
                else:
                    out = None
                if not self._tx:
                    self.conn.commit()
                return out
            except Exception as e:
                if self.dialect == "sqlite" and not self._tx:
                    try:
                        self.conn.rollback()   # never leave a failed statement's implicit transaction open
                    except Exception:
                        pass
                if self._tx or attempt or self._reconnect is None or not _is_connection_error(e):
                    raise
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = self._reconnect()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        memo = getattr(self._local, "memo", None)
        if memo is not None:
            memo.clear()            # any write inside a page request drops what was read before it
        with self._lock:
            self._writes += 1
            self._run(sql, params, fetch=False)

    @contextmanager
    def page_reads(self, key: str):
        """Rendering a GET page: identical SELECTs are answered once, and on Postgres the SELECTs this page made
        last time are prefetched in ONE pipelined round trip (Neon latency otherwise adds ~0.3 s per query).
        Read freshness: everything is read at the start of this request. Not used by send/cost/lock decisions,
        which run in POST handlers outside this scope."""
        memo: Dict[Any, Any] = {}
        log: List[Any] = []
        self._local.memo, self._local.log = memo, log
        try:
            plan = self._plans.get(key)
            if plan and self.dialect == "postgres":
                try:
                    for (sql, params), rows in zip(plan, self.query_many(plan)):
                        memo[(sql, tuple(params))] = rows
                except Exception:
                    memo.clear()
            yield self
        finally:
            self._local.memo = self._local.log = None
            if log:
                seen, uniq = set(), []
                for item in log:
                    k = (item[0], tuple(item[1]))
                    if k not in seen:
                        seen.add(k)
                        uniq.append(item)
                self._plans[key] = uniq[:80]

    @contextmanager
    def transaction(self):
        """All statements inside run in one database transaction, holding this store's lock (in-process
        callers are serialised) and, on Postgres, row locks taken with SELECT ... FOR UPDATE (other processes)."""
        with self._lock:
            if self._tx:
                self._tx += 1
                try:
                    yield self
                finally:
                    self._tx -= 1
                return
            if self.dialect == "sqlite":
                self.conn.execute("BEGIN IMMEDIATE")
                self._tx = 1
                try:
                    yield self
                except BaseException:
                    self._tx = 0
                    self.conn.rollback()
                    raise
                self._tx = 0
                self.conn.commit()
            else:
                with self.conn.transaction():
                    self._tx = 1
                    try:
                        yield self
                    finally:
                        self._tx = 0

    def query_many(self, items: List[Any]) -> List[List[Dict[str, Any]]]:
        """Run several SELECTs. On Postgres they are pipelined into one network round trip (Neon latency
        dominates page load time otherwise). Read-only: used for dashboard views."""
        if self.dialect != "postgres":
            return [self.query(sql, params) for sql, params in items]
        for attempt in (0, 1):
            try:
                with self._lock:
                    with self.conn.pipeline():
                        curs = [self.conn.execute(sql, tuple(params)) for sql, params in items]
                    out = []
                    for cur in curs:
                        cols = [d[0] for d in cur.description] if cur.description else []
                        out.append([{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in zip(cols, r)}
                                    for r in cur.fetchall()])
                return out
            except Exception as e:
                if attempt or self._reconnect is None or not _is_connection_error(e):
                    raise
                with self._lock:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = self._reconnect()
        return []

    def display_snapshot(self, loader: Any, ttl: float = 5.0) -> Any:
        """Cache a read-only view for `ttl` seconds, dropped on any write through this store.
        Never used for safety decisions (send gate, locks, caps), only for rendering pages."""
        import time
        snap = self._snap
        if snap and snap[0] == self._writes and time.monotonic() - snap[1] < ttl:
            return snap[2]
        data = loader(self)
        self._snap = (self._writes, time.monotonic(), data)
        return data

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        memo = getattr(self._local, "memo", None)
        if memo is not None and sql.lstrip()[:6].upper() == "SELECT" and "FOR UPDATE" not in sql.upper():
            k = (sql, tuple(params))
            if k in memo:
                return [dict(r) for r in memo[k]]
            self._local.log.append((sql, tuple(params)))
            rows = self._query(sql, params)
            memo[k] = rows
            return [dict(r) for r in rows]
        return self._query(sql, params)

    def _query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._run(sql, params, fetch=True)
        for r in rows:
            for k, v in list(r.items()):
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        return rows

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: Dict[str, Any]) -> str:
        row = dict(row)
        row.setdefault("id", new_id())
        cols = list(row.keys())
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join(["%s"] * len(cols)))
        self.execute(sql, [row[c] for c in cols])
        return row["id"]

    def update(self, table: str, row_id: str, fields: Dict[str, Any]) -> None:
        cols = list(fields.keys())
        sql = "UPDATE %s SET %s WHERE id = %s" % (table, ", ".join("%s = %%s" % c for c in cols), "%s")
        self.execute(sql, [fields[c] for c in cols] + [row_id])

    # ---------- migrations ----------
    def migrate(self) -> List[str]:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            self.conn.commit()
        applied = {r["name"] for r in self.query("SELECT name FROM schema_migrations")}
        done = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with self._lock:
                cur = self.conn.cursor()
                if self.dialect == "sqlite":
                    self.conn.executescript(sql)
                else:
                    cur.execute(sql)
                cur.execute(self._sql("INSERT INTO schema_migrations (name, applied_at) VALUES (%s, %s)"),
                            (path.name, now_iso()))
                self.conn.commit()
            done.append(path.name)
        return done

    # ---------- domain helpers ----------
    def log(self, run_id: str, message: str, level: str = "info", company_id: Optional[str] = None) -> None:
        from .util import redact
        self.insert("run_log", {"run_id": run_id, "company_id": company_id, "level": level,
                                "message": redact(message)[:2000], "created_at": now_iso()})

    def runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM research_runs ORDER BY started_at DESC LIMIT %s", (limit,))

    def companies_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM companies WHERE run_id = %s ORDER BY (score IS NULL), score DESC, created_at", (run_id,))

    def evidence_for(self, company_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM evidence WHERE company_id = %s ORDER BY criterion, retrieved_at", (company_id,))

    def pocs_for(self, company_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM pocs WHERE company_id = %s", (company_id,))

    def drafts_for(self, company_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM drafts WHERE company_id = %s ORDER BY created_at", (company_id,))

    def draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM drafts WHERE id = %s", (draft_id,))

    def reviews_for(self, draft_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM review_decisions WHERE draft_id = %s ORDER BY decided_at, version", (draft_id,))

    def recent_reviews(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.query("SELECT r.*, d.original_body, d.original_subject FROM review_decisions r "
                          "JOIN drafts d ON d.id = r.draft_id ORDER BY r.decided_at DESC LIMIT %s", (limit,))

    def sends_for(self, draft_id: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM sends WHERE draft_id = %s ORDER BY sent_at", (draft_id,))

    def logs_for(self, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM run_log WHERE run_id = %s ORDER BY created_at LIMIT %s", (run_id, limit))


def _is_connection_error(e: Exception) -> bool:
    try:
        import psycopg
        return isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))
    except ImportError:
        return False


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def open_mock_store(path: Optional[Path] = None) -> Store:
    path = path or MOCK_DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    store = Store(conn, "sqlite", is_mock=True, label="MOCK (fake data, %s)" % path)
    store.migrate()
    return store


def open_real_store(settings: Settings) -> Store:
    if not settings.neon_url:
        raise StorageNotConfigured(
            "NEON_DATABASE_URL is not set. Real runs need Neon Postgres; nothing was saved. "
            "Set it in .env, then run: python -m sprinto_agent migrate")
    import psycopg  # imported lazily so mock mode never needs a DB driver
    url = settings.neon_url

    def connect():
        # autocommit: no transaction is left open between requests (Neon ends idle-in-transaction sessions)
        return psycopg.connect(url, connect_timeout=15, autocommit=True)
    return Store(connect(), "postgres", is_mock=False, label="Neon Postgres", reconnect=connect)


def open_store(settings: Settings) -> Store:
    return open_mock_store() if settings.mock else open_real_store(settings)


# ---------------------------------------------------------------- reliability helpers
class StorageError(RuntimeError):
    """Raised when a write that must succeed (lock, audit, send record) fails."""


def is_unique_violation(e: Exception) -> bool:
    if isinstance(e, sqlite3.IntegrityError):
        return True
    try:
        import psycopg
        return isinstance(e, psycopg.errors.UniqueViolation)
    except ImportError:  # pragma: no cover
        return False


def get_flag(store: "Store", name: str, default: str = "") -> str:
    row = store.one("SELECT value FROM app_flags WHERE name = %s", (name,))
    return row["value"] if row else default


def set_flag(store: "Store", name: str, value: str, by: str = "operator") -> None:
    """Race-safe upsert: several workers may set the same flag at once (e.g. all hitting a cost cap)."""
    if not store.one("SELECT name FROM app_flags WHERE name = %s", (name,)):
        try:
            store.execute("INSERT INTO app_flags (name, value, updated_at, updated_by) VALUES (%s, %s, %s, %s)",
                          (name, value, now_iso(), by))
            return
        except Exception as e:
            if not is_unique_violation(e):
                raise
    store.execute("UPDATE app_flags SET value = %s, updated_at = %s, updated_by = %s WHERE name = %s",
                  (value, now_iso(), by, name))


def audit_fetch(store: "Store", run_id: str, company_id: Optional[str], kind: str, target: str, outcome: str,
                detail: str = "") -> None:
    from .util import redact
    store.insert("fetch_log", {"run_id": run_id, "company_id": company_id, "kind": kind, "target": redact(target)[:500],
                               "outcome": outcome, "detail": redact(detail or "")[:500], "created_at": now_iso()})
