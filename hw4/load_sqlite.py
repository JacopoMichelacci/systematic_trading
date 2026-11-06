"""HW4 Task 1a — the recordings in SQLite, a relational schema with keys and indexes.

    python load_sqlite.py sessions/session_week02_*.jsonl.gz sessions/session_week04_*.jsonl.gz --db hw4.sqlite

One table per message type we use (book + its normalised depth, trade, order_ack, session_event),
typed columns, primary keys on (week, seq) so a second week is a NEW LOAD that can never collide
with or edit an old one, both clocks (tick, t, ts) and the recording order (seq) kept.

Measures, per week: JSONL bytes in, load time, rows per table, and the file size without and
with indexes. Prints the storage table in Markdown for the report.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

from tape import COLUMNS, parse_tape

DDL = """
CREATE TABLE IF NOT EXISTS book (            -- top of book, one row per snapshot
  seq INTEGER NOT NULL,                      -- recording order (line number in the tape)
  week INTEGER NOT NULL,
  tick INTEGER NOT NULL,                     -- venue tick: the logical clock
  t INTEGER NOT NULL,                        -- scenario tick (t400 = the AAPL print)
  ts REAL NOT NULL,                          -- wall clock when the line was written
  symbol TEXT NOT NULL,
  best_bid REAL, best_bid_qty REAL, best_ask REAL, best_ask_qty REAL,
  mid REAL, microprice REAL, obi REAL, spread REAL,
  n_bids INTEGER, n_asks INTEGER,
  PRIMARY KEY (week, seq));
CREATE TABLE IF NOT EXISTS book_level (      -- the depth, normalised: one row per price level
  seq INTEGER NOT NULL, week INTEGER NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('B','A')),
  lvl INTEGER NOT NULL, px REAL NOT NULL, qty REAL NOT NULL,
  PRIMARY KEY (week, seq, side, lvl),
  FOREIGN KEY (week, seq) REFERENCES book(week, seq));
CREATE TABLE IF NOT EXISTS trade (
  seq INTEGER NOT NULL, week INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL, ts REAL NOT NULL,
  symbol TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, aggressor TEXT,
  taker_id TEXT, maker_id TEXT, fee REAL, buyer_id TEXT, seller_id TEXT,
  PRIMARY KEY (week, seq));
CREATE TABLE IF NOT EXISTS order_ack (
  seq INTEGER NOT NULL, week INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL, ts REAL NOT NULL,
  order_id TEXT, team_id TEXT, symbol TEXT, side TEXT, order_type TEXT, price REAL, quantity REAL,
  PRIMARY KEY (week, seq));
CREATE TABLE IF NOT EXISTS session_event (
  seq INTEGER NOT NULL, week INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL, ts REAL NOT NULL,
  event TEXT NOT NULL, kind TEXT, symbol TEXT, pct REAL, message TEXT, data TEXT,
  PRIMARY KEY (week, seq));
"""

# Every index exists for a query in run_queries.py. The leading columns are the ones the WHERE
# clauses filter on first (a composite B-tree is used left to right).
INDEXES = {
    # as-of join & point lookup: "AAPL's book at or before tick T, last by seq" -> one B-tree seek
    "ix_book_sym_tick": "CREATE INDEX IF NOT EXISTS ix_book_sym_tick ON book (week, symbol, tick, seq)",
    # event windows: "every book of tick T" across symbols
    "ix_book_tick": "CREATE INDEX IF NOT EXISTS ix_book_tick ON book (week, tick)",
    # own fills: "every trade where I was taker/maker"
    "ix_trade_taker": "CREATE INDEX IF NOT EXISTS ix_trade_taker ON trade (taker_id, week, tick)",
    "ix_trade_maker": "CREATE INDEX IF NOT EXISTS ix_trade_maker ON trade (maker_id, week, tick)",
    # events by kind: "every CALENDAR_EVENT"
    "ix_event_kind": "CREATE INDEX IF NOT EXISTS ix_event_kind ON session_event (event, week, tick)",
}


def load(db_path: str, tapes: list[str], drop_indexes_first: bool = True) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.executescript(DDL)
    if drop_indexes_first:                                  # measure size without indexes first
        for name in INDEXES:
            con.execute(f"DROP INDEX IF EXISTS {name}")
    con.commit()
    report = []
    for path in tapes:
        t0 = time.perf_counter()
        tb = parse_tape(path)
        t_parse = time.perf_counter() - t0
        week = tb.book[0][1] if tb.book else None
        con.execute("DELETE FROM book_level WHERE week=?", (week,))    # re-running the same week is a reload,
        for name in ("book", "trade", "order_ack", "session_event"):   # never a partial edit
            con.execute(f"DELETE FROM {name} WHERE week=?", (week,))
        t1 = time.perf_counter()
        with con:                                                     # ONE transaction per tape
            for name, rows in tb.tables().items():
                cols = COLUMNS[name]
                con.executemany(f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})", rows)
        t_insert = time.perf_counter() - t1
        rows_per = {name: len(rows) for name, rows in tb.tables().items()}
        report.append({"week": week, "tape": os.path.basename(path), "jsonl_bytes": tb.n_bytes, "file_bytes": os.path.getsize(path),
                       "lines": tb.n_lines, "t0": tb.t0, "parse_s": t_parse, "insert_s": t_insert,
                       "rows": rows_per, "total_rows": sum(rows_per.values())})
        print(f"week {week}: {tb.n_lines:,} lines, t0={tb.t0}, parse {t_parse:.1f}s, insert {t_insert:.2f}s "
              f"({sum(rows_per.values()) / max(t_insert, 1e-9):,.0f} rows/s)")
    con.commit(); con.execute("VACUUM"); con.close()
    return report


def add_indexes(db_path: str) -> float:
    con = sqlite3.connect(db_path)
    t0 = time.perf_counter()
    for sql in INDEXES.values():
        con.execute(sql)
    con.commit(); con.close()
    return time.perf_counter() - t0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tapes", nargs="+")
    ap.add_argument("--db", default="hw4.sqlite")
    a = ap.parse_args()
    if os.path.exists(a.db):
        os.remove(a.db)                                     # rebuild from scratch: one command
    rep = load(a.db, a.tapes)
    size_no_ix = os.path.getsize(a.db)
    t_ix = add_indexes(a.db)
    size_ix = os.path.getsize(a.db)
    print(f"\nindexes built in {t_ix:.2f}s · file {size_no_ix:,} -> {size_ix:,} bytes "
          f"(+{100 * (size_ix / size_no_ix - 1):.0f}%)\n")
    # ---- storage table (Markdown) --------------------------------------------------------------
    tables = ["book", "book_level", "trade", "order_ack", "session_event"]
    print("| week | tape | JSONL bytes (uncompressed) | file on disk | lines | parse s | insert s | " + " | ".join(tables) + " | total rows |")
    print("|---|---|---:|---:|---:|---:|---:|" + "---:|" * len(tables) + "---:|")
    for r in rep:
        print(f"| {r['week']} | {r['tape']} | {r['jsonl_bytes']:,} | {r['file_bytes']:,} | {r['lines']:,} | {r['parse_s']:.1f} | "
              f"{r['insert_s']:.2f} | " + " | ".join(f"{r['rows'][t]:,}" for t in tables) + f" | {r['total_rows']:,} |")
    print(f"\nSQLite file: {size_no_ix:,} bytes without indexes, {size_ix:,} bytes with {len(INDEXES)} indexes "
          f"(indexes built in {t_ix:.2f} s)")
    jsonl_total = sum(r["jsonl_bytes"] for r in rep)
    print(f"JSONL in (all weeks, uncompressed): {jsonl_total:,} bytes")