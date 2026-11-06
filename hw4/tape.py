"""Shared tape reader for HW4: one pass over a venue recording -> flat rows per table.

A recording is one JSON object per line: {"ts": <wall clock>, "tick": <venue tick>, "msg": {...}}.
We keep BOTH clocks (tick and ts), the recording order (seq = line number) and add the
scenario tick t = tick - t0, where t0 is derived from the tape itself: the CALENDAR message
announces events in scenario ticks ("AAPL earnings at t400") and the matching CALENDAR_EVENT
fires at an absolute tick, so t0 = fired_tick - announced_t. Falls back to SESSION_OPEN's tick.

Used by load_sqlite.py and tape_to_parquet.py so both stores hold identical rows.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass, field


def week_of(path: str) -> int:
    m = re.search(r"week(\d+)", os.path.basename(path))
    if not m:
        raise SystemExit(f"cannot read the week from the file name: {path} (expected session_weekNN_...)")
    return int(m.group(1))


def open_tape(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


@dataclass
class Tables:
    book: list = field(default_factory=list)
    book_level: list = field(default_factory=list)
    trade: list = field(default_factory=list)
    order_ack: list = field(default_factory=list)
    session_event: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)      # lines per message type, for the report
    n_lines: int = 0
    n_bytes: int = 0                                 # uncompressed JSONL bytes (the .gz on disk is smaller)
    t0: int = 0

    def tables(self) -> dict:
        return {"book": self.book, "book_level": self.book_level, "trade": self.trade,
                "order_ack": self.order_ack, "session_event": self.session_event}


def parse_tape(path: str) -> Tables:
    """Two passes: the first finds t0 (cheap: only session_event lines), the second flattens."""
    week = week_of(path)
    out = Tables()
    announced, fired, open_tick = {}, [], None
    with open_tape(path) as f:
        for line in f:
            if '"session_event"' not in line:
                continue
            rec = json.loads(line); m = rec["msg"]
            if m.get("type") != "session_event":
                continue
            d = m.get("data") or {}
            if m.get("event") == "SESSION_OPEN" and open_tick is None:
                open_tick = d.get("tick", rec["tick"])
            for e in d.get("imminent") or []:
                announced[(e.get("kind"), e.get("symbol"))] = e["tick"]
            if m.get("event") == "CALENDAR_EVENT":
                fired.append(((d.get("kind"), d.get("symbol")), d.get("tick", rec["tick"])))
    offs = [tick - announced[k] for k, tick in fired if k in announced]
    out.t0 = offs[0] if offs else (open_tick or 0)

    with open_tape(path) as f:
        for seq, line in enumerate(f):
            out.n_lines += 1
            out.n_bytes += len(line)                 # ASCII JSON: one char = one byte
            rec = json.loads(line)
            m = rec["msg"]; typ = m.get("type")
            out.counts[typ] = out.counts.get(typ, 0) + 1
            tick, ts = rec["tick"], rec["ts"]
            t = tick - out.t0
            base = (seq, week, tick, t, ts)
            if typ == "book_snapshot":
                bids, asks = m.get("bids") or [], m.get("asks") or []
                bb = bids[0][0] if bids else None; ba = asks[0][0] if asks else None
                mid = m.get("mid_price")
                if mid is None and bb is not None and ba is not None:
                    mid = (bb + ba) / 2
                out.book.append(base + (m["symbol"], bb, bids[0][1] if bids else None, ba,
                                        asks[0][1] if asks else None, mid, m.get("microprice"), m.get("obi"),
                                        (ba - bb) if (bb is not None and ba is not None) else None,
                                        len(bids), len(asks)))
                for lvl, (px, qty) in enumerate(bids):
                    out.book_level.append((seq, week, "B", lvl, px, qty))
                for lvl, (px, qty) in enumerate(asks):
                    out.book_level.append((seq, week, "A", lvl, px, qty))
            elif typ == "trade_execution":
                out.trade.append(base + (m["symbol"], m["price"], m["quantity"], m.get("aggressor"),
                                         m.get("taker_id"), m.get("maker_id"), m.get("fee"),
                                         m.get("buyer_id"), m.get("seller_id")))
            elif typ == "order_ack":
                out.order_ack.append(base + (m.get("order_id"), m.get("team_id"), m.get("symbol"), m.get("side"),
                                             m.get("order_type"), m.get("price"), m.get("quantity")))
            elif typ == "session_event":
                d = m.get("data") or {}
                out.session_event.append(base + (m.get("event"), d.get("kind"), d.get("symbol"), d.get("pct"),
                                                 m.get("message"), json.dumps(d)))
    return out


# Column names, shared by both stores (same order as the tuples above)
COLUMNS = {
    "book": ["seq", "week", "tick", "t", "ts", "symbol", "best_bid", "best_bid_qty", "best_ask", "best_ask_qty",
             "mid", "microprice", "obi", "spread", "n_bids", "n_asks"],
    "book_level": ["seq", "week", "side", "lvl", "px", "qty"],
    "trade": ["seq", "week", "tick", "t", "ts", "symbol", "price", "quantity", "aggressor", "taker_id", "maker_id",
              "fee", "buyer_id", "seller_id"],
    "order_ack": ["seq", "week", "tick", "t", "ts", "order_id", "team_id", "symbol", "side", "order_type", "price",
                  "quantity"],
    "session_event": ["seq", "week", "tick", "t", "ts", "event", "kind", "symbol", "pct", "message", "data"],
}