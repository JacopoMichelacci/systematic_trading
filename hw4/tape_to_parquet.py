"""HW4 Task 1b — the same recordings as Parquet: one table per message type, hive-partitioned by week and symbol.

    python tape_to_parquet.py sessions/session_week02_*.jsonl.gz sessions/session_week04_*.jsonl.gz --out hw4.parquet

Layout (kdb+'s date folders, in a file format):
    hw4.parquet/book/week=2/symbol=AAPL/week2-0.parquet
    hw4.parquet/book/week=4/symbol=AAPL/week4-0.parquet      <- a new week is a NEW FOLDER, never an edit
    hw4.parquet/trade/..., order_ack/..., session_event/..., book_level/...

Every table keeps both clocks (tick, t, ts) and the recording order (seq), has an explicit pyarrow schema,
is sorted by (tick, seq) so the row-group min/max statistics make time filters skip row groups (pushdown),
and is zstd-compressed. Prints bytes per table and the conversion time for the storage table.
"""
from __future__ import annotations

import argparse
import os
import time

import pyarrow as pa
import pyarrow.dataset as ds

from tape import COLUMNS, parse_tape

CLOCKS = [("seq", pa.int64()), ("week", pa.int32()), ("tick", pa.int64()), ("t", pa.int32()), ("ts", pa.float64())]
SCHEMAS = {
    "book": pa.schema(CLOCKS + [("symbol", pa.string()), ("best_bid", pa.float64()), ("best_bid_qty", pa.float64()),
                                ("best_ask", pa.float64()), ("best_ask_qty", pa.float64()), ("mid", pa.float64()),
                                ("microprice", pa.float64()), ("obi", pa.float64()), ("spread", pa.float64()),
                                ("n_bids", pa.int32()), ("n_asks", pa.int32())]),
    "book_level": pa.schema([("seq", pa.int64()), ("week", pa.int32()), ("side", pa.string()), ("lvl", pa.int32()),
                             ("px", pa.float64()), ("qty", pa.float64()), ("symbol", pa.string()),
                             ("tick", pa.int64())]),
    "trade": pa.schema(CLOCKS + [("symbol", pa.string()), ("price", pa.float64()), ("quantity", pa.float64()),
                                 ("aggressor", pa.string()), ("taker_id", pa.string()), ("maker_id", pa.string()),
                                 ("fee", pa.float64()), ("buyer_id", pa.string()), ("seller_id", pa.string())]),
    "order_ack": pa.schema(CLOCKS + [("order_id", pa.string()), ("team_id", pa.string()), ("symbol", pa.string()),
                                     ("side", pa.string()), ("order_type", pa.string()), ("price", pa.float64()),
                                     ("quantity", pa.float64())]),
    "session_event": pa.schema(CLOCKS + [("event", pa.string()), ("kind", pa.string()), ("symbol", pa.string()),
                                         ("pct", pa.float64()), ("message", pa.string()), ("data", pa.string())]),
}


def to_table(name: str, rows: list, sym_of_seq: dict | None = None) -> pa.Table:
    cols = COLUMNS[name]
    data = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
    if name == "book_level":                               # depth rows carry the symbol + tick for partitioning/pushdown
        data["symbol"] = [sym_of_seq[r[0]][0] for r in rows]
        data["tick"] = [sym_of_seq[r[0]][1] for r in rows]
    if name == "session_event":                            # market-wide events have no symbol: partition them as ALL
        data["symbol"] = [s if s is not None else "ALL" for s in data["symbol"]]
    tbl = pa.Table.from_pydict(data, schema=SCHEMAS[name])
    return tbl.sort_by([("tick", "ascending"), ("seq", "ascending")])


def dir_bytes(path: str) -> int:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def convert(tapes: list[str], out: str) -> list[dict]:
    report = []
    for path in tapes:
        t0 = time.perf_counter()
        tb = parse_tape(path)
        t_parse = time.perf_counter() - t0
        week = tb.book[0][1]
        sym_of_seq = {r[0]: (r[5], r[2]) for r in tb.book}
        sizes = {}
        t1 = time.perf_counter()
        for name, rows in tb.tables().items():
            if not rows:
                continue
            tbl = to_table(name, rows, sym_of_seq)
            before = dir_bytes(f"{out}/{name}") if os.path.isdir(f"{out}/{name}") else 0
            ds.write_dataset(tbl, f"{out}/{name}", format="parquet",
                             partitioning=ds.partitioning(pa.schema([("week", pa.int32()), ("symbol", pa.string())]),
                                                          flavor="hive"),
                             basename_template=f"week{week}-{{i}}.parquet",
                             existing_data_behavior="overwrite_or_ignore",     # other weeks' folders are untouched
                             file_options=ds.ParquetFileFormat().make_write_options(compression="zstd"),
                             max_rows_per_group=32_768)
            sizes[name] = dir_bytes(f"{out}/{name}") - before
        t_write = time.perf_counter() - t1
        report.append({"week": week, "tape": os.path.basename(path), "jsonl_bytes": tb.n_bytes, "parse_s": t_parse,
                       "write_s": t_write, "bytes": sizes, "rows": {n: len(r) for n, r in tb.tables().items()}})
        print(f"week {week}: parse {t_parse:.1f}s, write {t_write:.2f}s, parquet {sum(sizes.values()):,} bytes "
              f"({tb.n_bytes / max(sum(sizes.values()), 1):.1f}x smaller than the JSONL)")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tapes", nargs="+")
    ap.add_argument("--out", default="hw4.parquet")
    a = ap.parse_args()
    rep = convert(a.tapes, a.out)
    tables = ["book", "book_level", "trade", "order_ack", "session_event"]
    print("\n| week | JSONL bytes | Parquet bytes | ratio | parse s | write s | " + " | ".join(f"{t} bytes" for t in tables) + " |")
    print("|---|---:|---:|---:|---:|---:|" + "---:|" * len(tables))
    for r in rep:
        tot = sum(r["bytes"].values())
        print(f"| {r['week']} | {r['jsonl_bytes']:,} | {tot:,} | {r['jsonl_bytes'] / tot:.1f}x | {r['parse_s']:.1f} | "
              f"{r['write_s']:.2f} | " + " | ".join(f"{r['bytes'].get(t, 0):,}" for t in tables) + " |")
    print(f"\nwarehouse {a.out}: {dir_bytes(a.out):,} bytes in total")
    for name in tables:
        d = f"{a.out}/{name}"
        if os.path.isdir(d):
            parts = sorted(p for p in os.listdir(d) if p.startswith("week="))
            print(f"  {name}/: {', '.join(parts)}")