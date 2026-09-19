# -*- coding: utf-8 -*-
"""存储层：SQLite 连接管理 + 建表 DDL + 事务上下文。

对应 Step 2 的 DDL 与 Step 1 的 A2(SQLite)/A5(事务) 选型。
设计要点：
  - 单连接 + check_same_thread=False + 线程锁，保证单进程串行写入；
  - PRAGMA foreign_keys=ON 强制外键；
  - 写事务统一用 BEGIN IMMEDIATE，避免写-写升级死锁；
  - 提供 transaction() 上下文：正常 COMMIT，异常 ROLLBACK。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    holder_name     TEXT    NOT NULL,
    account_no      TEXT    NOT NULL UNIQUE,
    account_type    TEXT    NOT NULL
                    CHECK (account_type IN ('ASSET','LIABILITY','EQUITY','INCOME','EXPENSE')),
    currency        TEXT    NOT NULL DEFAULT 'CNY' CHECK (length(currency) = 3),
    balance         INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','FROZEN','CLOSED')),
    allow_overdraft INTEGER NOT NULL DEFAULT 0 CHECK (allow_overdraft IN (0,1)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (balance >= 0 OR allow_overdraft = 1)
);

CREATE INDEX IF NOT EXISTS idx_accounts_type_status ON accounts(account_type, status);
CREATE INDEX IF NOT EXISTS idx_accounts_currency    ON accounts(currency);

CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_ref           TEXT    NOT NULL UNIQUE,
    idempotency_key   TEXT    NOT NULL UNIQUE,
    txn_type          TEXT    NOT NULL
                      CHECK (txn_type IN ('TRANSFER','DEPOSIT','WITHDRAWAL')),
    amount            INTEGER NOT NULL CHECK (amount > 0),
    currency          TEXT    NOT NULL CHECK (length(currency) = 3),
    from_account_id   INTEGER REFERENCES accounts(id),
    to_account_id     INTEGER REFERENCES accounts(id),
    status            TEXT    NOT NULL DEFAULT 'POSTED'
                      CHECK (status IN ('PENDING','POSTED','REVERSED')),
    memo              TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_txn_created_at   ON transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_txn_from_account ON transactions(from_account_id);
CREATE INDEX IF NOT EXISTS idx_txn_to_account   ON transactions(to_account_id);
CREATE INDEX IF NOT EXISTS idx_txn_type_status  ON transactions(txn_type, status);

CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    direction      TEXT    NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
    amount         INTEGER NOT NULL CHECK (amount > 0),
    currency       TEXT    NOT NULL CHECK (length(currency) = 3),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    memo           TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_transaction ON entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_entries_account     ON entries(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_entries_dir         ON entries(transaction_id, direction);
"""

#: 系统内部账户号（用于存款/取款的对方分录，保持复式记账完整）
SYSTEM_CASH_ACCOUNT_NO = "SYS-CASH-000000"


class Database:
    """SQLite 数据库封装（单连接 + 线程锁 + 事务上下文）。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(DDL)
            self._ensure_system_account()

    def _ensure_system_account(self) -> None:
        """确保系统现金账户存在（存款/取款的对手方，属 ASSET 类）。"""
        cur = self.conn.execute(
            "SELECT id FROM accounts WHERE account_no = ?", (SYSTEM_CASH_ACCOUNT_NO,)
        )
        if cur.fetchone() is None:
            self.conn.execute(
                """INSERT INTO accounts
                   (holder_name, account_no, account_type, currency, balance, allow_overdraft)
                   VALUES (?, ?, 'ASSET', 'CNY', 0, 1)""",
                ("SYSTEM CASH", SYSTEM_CASH_ACCOUNT_NO),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文：BEGIN IMMEDIATE -> yield -> COMMIT / 异常 ROLLBACK。

        使用 IMMEDIATE 立即取写锁，避免并发写升级死锁；
        任一步异常整体回滚，库状态不变（满足"失败整体回滚"）。
        """
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
