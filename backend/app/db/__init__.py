"""SQLite access: connection handling, migrations and small query helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import get_config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def db_path() -> Path:
    cfg = get_config()
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    return cfg.data_path / "app.db"


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_version")}
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(f.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(f.read_text())
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))


def get_conn() -> sqlite3.Connection:
    """One connection per thread, keyed by db path (tests swap data dirs)."""
    path = db_path()
    key = str(path)
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    _local.conns = conns
    conn = conns.get(key)
    if conn is None:
        conn = _connect(path)
        with _init_lock:
            if key not in _initialized:
                migrate(conn)
                _initialized.add(key)
        conns[key] = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    if conn.in_transaction:
        # nested: just reuse the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return get_conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    return get_conn().execute(sql, params).fetchone()


def ex(sql: str, params: tuple | dict = ()) -> int:
    cur = get_conn().execute(sql, params)
    return cur.lastrowid or cur.rowcount


def insert(table: str, row: dict[str, Any]) -> int:
    cols = ", ".join(row)
    ph = ", ".join(f":{k}" for k in row)
    cur = get_conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", row)
    return int(cur.lastrowid)


def update(table: str, row_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    sets = ", ".join(f"{k} = :{k}" for k in values)
    get_conn().execute(f"UPDATE {table} SET {sets} WHERE id = :_id", {**values, "_id": row_id})


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def loads(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    return json.loads(v)


def close_all() -> None:
    conns = getattr(_local, "conns", None) or {}
    for c in conns.values():
        c.close()
    _local.conns = {}
