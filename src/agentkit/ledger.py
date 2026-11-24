"""账本 + 防篡改审计日志（Phase 0 最小实现，可直接用）

设计要点
1. 每笔收支一行，append-only，不允许 UPDATE/DELETE。
2. hash-chain：每行含前一行 hash，任何篡改都会导致链校验失败。
3. 单位经济：按 project 聚合收入/成本，算毛利率，直接喂给 kill criteria。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DB_PATH = Path(os.environ.get("ROBOT_LEDGER", Path(__file__).with_name("robot.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    kind       TEXT    NOT NULL,          -- expense | revenue | action | decision
    project    TEXT    NOT NULL DEFAULT '',
    category   TEXT    NOT NULL DEFAULT '',
    amount_usd REAL    NOT NULL DEFAULT 0,
    meta       TEXT    NOT NULL DEFAULT '{}',
    prev_hash  TEXT    NOT NULL,
    hash       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_project ON journal(project, kind);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _append(conn: sqlite3.Connection, kind: str, *, project: str = "", category: str = "",
            amount_usd: float = 0.0, meta: dict | None = None) -> dict:
    row = conn.execute("SELECT hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else "GENESIS"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = json.dumps(
        {"ts": ts, "kind": kind, "project": project, "category": category,
         "amount_usd": round(amount_usd, 6), "meta": meta or {}},
        sort_keys=True, ensure_ascii=False,
    )
    digest = hashlib.sha256((prev + payload).encode()).hexdigest()
    conn.execute(
        "INSERT INTO journal(ts,kind,project,category,amount_usd,meta,prev_hash,hash) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (ts, kind, project, category, round(amount_usd, 6), json.dumps(meta or {}, ensure_ascii=False), prev, digest),
    )
    conn.commit()
    return {"hash": digest, "prev": prev}


# ---------------------------------------------------------------- public API

def record_expense(amount_usd: float, *, project: str = "", category: str = "api",
                   meta: dict | None = None) -> dict:
    """记一笔成本。所有 agent 的每一次花钱都必须走这里。"""
    if amount_usd < 0:
        raise ValueError("expense 必须为正数")
    with _connect() as conn:
        return _append(conn, "expense", project=project, category=category,
                       amount_usd=amount_usd, meta=meta)


def record_revenue(amount_usd: float, *, project: str = "", category: str = "service",
                   meta: dict | None = None) -> dict:
    if amount_usd <= 0:
        raise ValueError("revenue 必须为正数")
    with _connect() as conn:
        return _append(conn, "revenue", project=project, category=category,
                       amount_usd=amount_usd, meta=meta)


def record_action(action: str, *, project: str = "", meta: dict | None = None) -> dict:
    """记录一个不可逆/对外动作（发邮件、发布内容、下单）。审计用。"""
    with _connect() as conn:
        return _append(conn, "action", project=project, category=action, meta=meta)


def totals(*, project: str | None = None, since: str | None = None) -> dict:
    sql = ("SELECT kind, COALESCE(SUM(amount_usd),0) AS amt, COUNT(*) AS n "
           "FROM journal WHERE kind IN ('expense','revenue')")
    args: list = []
    if project:
        sql += " AND project = ?"
        args.append(project)
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " GROUP BY kind"
    with _connect() as conn:
        rows = {r["kind"]: {"amount": r["amt"], "count": r["n"]} for r in conn.execute(sql, args)}
    rev = rows.get("revenue", {"amount": 0.0, "count": 0})
    exp = rows.get("expense", {"amount": 0.0, "count": 0})
    margin = rev["amount"] - exp["amount"]
    return {
        "revenue": round(rev["amount"], 4),
        "expense": round(exp["amount"], 4),
        "margin": round(margin, 4),
        "margin_pct": round(margin / rev["amount"] * 100, 1) if rev["amount"] else None,
        "orders": rev["count"],
    }


def recent(limit: int = 20, *, project: str | None = None) -> list[dict]:
    """最近 N 条流水，供规划器理解"做过什么、结果如何"。"""
    sql = "SELECT ts, kind, project, category, amount_usd, meta FROM journal"
    args: list = []
    if project:
        sql += " WHERE project = ?"
        args.append(project)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["meta"] = json.loads(r["meta"])
    return rows


def verify_chain() -> tuple[bool, str]:
    """校验 hash-chain 完整性。任何人改了库里的历史行都会被抓出来。"""
    with _connect() as conn:
        prev = "GENESIS"
        for r in conn.execute("SELECT * FROM journal ORDER BY seq"):
            payload = json.dumps(
                {"ts": r["ts"], "kind": r["kind"], "project": r["project"],
                 "category": r["category"], "amount_usd": r["amount_usd"],
                 "meta": json.loads(r["meta"])},
                sort_keys=True, ensure_ascii=False,
            )
            expect = hashlib.sha256((prev + payload).encode()).hexdigest()
            if expect != r["hash"] or r["prev_hash"] != prev:
                return False, f"seq={r['seq']} 校验失败（账本被篡改）"
            prev = r["hash"]
    return True, "OK"


if __name__ == "__main__":
    record_expense(0.42, project="demo", category="llm")
    record_revenue(200.0, project="demo", category="service")
    print(totals(project="demo"))
    print(verify_chain())
