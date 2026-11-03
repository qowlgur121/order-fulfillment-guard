"""주문·구매 시도 상태를 SQLite에 저장한다.

중복 방지는 애플리케이션 if문이 아니라 DB 제약(PRIMARY KEY)과
조건부 UPDATE(WHERE status=?)로 막는다. 워커가 둘 떠도 같은 주문을 두 번 잡지 못한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,          -- 판매 채널 주문번호: 같은 주문은 한 줄뿐
    sku           TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    status        TEXT NOT NULL,             -- NEW / PROCESSING / BOUGHT / SENT / RETRY_WAIT / NEEDS_REVIEW
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_attempts (
    order_id          TEXT PRIMARY KEY REFERENCES orders(order_id),
    status            TEXT NOT NULL,         -- POSTING / CONFIRMED / REJECTED / AMBIGUOUS / CLEARED_NO_ORDER
    started_at        REAL NOT NULL,         -- 공급처 이력 대조 시 시간 창의 시작
    supplier_order_id TEXT,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # 트랜잭션은 직접 연다
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def add_order(conn: sqlite3.Connection, order_id: str, sku: str, quantity: int) -> bool:
    """새 주문을 넣는다. 이미 있는 주문번호면 False (폴링이 같은 주문을 다시 봐도 안전)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO orders(order_id, sku, quantity, status, updated_at) VALUES(?,?,?,'NEW',?)",
        (order_id, sku, quantity, _now()),
    )
    return cur.rowcount == 1


def claim(conn: sqlite3.Connection, order_id: str, now: float) -> bool:
    """처리할 차례인 주문을 PROCESSING으로 잡는다. 다른 워커가 먼저 잡았으면 False."""
    cur = conn.execute(
        """UPDATE orders SET status='PROCESSING', updated_at=?
           WHERE order_id=? AND (status='NEW' OR (status='RETRY_WAIT' AND next_retry_at<=?))""",
        (_now(), order_id, now),
    )
    return cur.rowcount == 1


def transition(conn: sqlite3.Connection, order_id: str, expected: str, new: str, **fields) -> bool:
    """expected 상태일 때만 new로 바꾼다. 상태가 이미 바뀌었으면 아무것도 하지 않는다."""
    sets = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE orders SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE order_id=? AND status=?"
    cur = conn.execute(sql, (new, _now(), *fields.values(), order_id, expected))
    return cur.rowcount == 1


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


def get_attempt(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM purchase_attempts WHERE order_id=?", (order_id,)).fetchone()


def save_attempt(conn: sqlite3.Connection, order_id: str, status: str, *, started_at: float | None = None,
                 supplier_order_id: str | None = None) -> None:
    if started_at is not None:
        conn.execute(
            """INSERT INTO purchase_attempts(order_id, status, started_at, supplier_order_id, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, started_at=excluded.started_at,
                 supplier_order_id=excluded.supplier_order_id, updated_at=excluded.updated_at""",
            (order_id, status, started_at, supplier_order_id, _now()),
        )
    else:
        conn.execute(
            "UPDATE purchase_attempts SET status=?, supplier_order_id=COALESCE(?, supplier_order_id), updated_at=? "
            "WHERE order_id=?",
            (status, supplier_order_id, _now(), order_id),
        )


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def confirmed_purchases_since(conn: sqlite3.Connection, since: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM purchase_attempts WHERE status='CONFIRMED' AND started_at>=?", (since,)
    ).fetchone()
    return int(row[0])
