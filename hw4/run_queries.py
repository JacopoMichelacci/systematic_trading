"""HW4 Task 2 — the same four questions in SQLite and in DuckDB over Parquet, answers and timings.

    python run_queries.py --db hw4.sqlite --warehouse hw4.parquet --team butter_chicken_pasta [--sym AAPL --tick <tick>]

Q1  event window     the END-OF-TICK mid at t-1 and at t+5, t+15, t+60 for every CALENDAR_EVENT, both weeks
Q2  as-of, own fills BACKWARD: the book prevailing just before each of my fills (paid vs touch mid, bp)
                     FORWARD : the end-of-tick book 60 ticks later (markout, bp)
Q3  full scan        spread (bp) and touch depth by symbol over both weeks
Q4  point lookup     the book of one symbol at one tick (last by seq), and my position at that tick

Tie rule (stated once, used everywhere): several snapshots share one tick. "The book AT tick T" means the
last snapshot of that tick by seq (end of tick) — the state a tick-driven backtester sees after applying
every snapshot of T. "The book BEFORE a fill" means the last snapshot with seq < the fill's seq: the exact
state of the book when the fill printed, which is finer than a tick.
Directions: Q2-backward reads the past (safe inside a backtest); Q2-forward reads the future (a markout:
fine in a post-trade report, a look-ahead bug inside a decision).
The team is matched as a PREFIX (taker_id LIKE 'butter_chicken_pasta%'), so all three trader ids count.
"""
from __future__ import annotations

import argparse
import sqlite3
import time

import duckdb

# ------------------------------------------------------------------ SQL, SQLite dialect
# "book at end of tick T for symbol S" = correlated sub-query, ORDER BY tick DESC, seq DESC LIMIT 1
EOT = ("(SELECT b.mid FROM book b WHERE b.week = e.week AND b.symbol = e.symbol AND b.tick <= e.tick + {k} "
       "ORDER BY b.tick DESC, b.seq DESC LIMIT 1)")
SQLITE = {
    "q1_event_window": f"""
        SELECT e.week, e.kind, e.symbol, e.t, e.pct AS announced_pct,
               {EOT.format(k=-1)} AS mid_before,
               round(100.0 * ({EOT.format(k=5)}  / {EOT.format(k=-1)} - 1), 2) AS ret_5,
               round(100.0 * ({EOT.format(k=15)} / {EOT.format(k=-1)} - 1), 2) AS ret_15,
               round(100.0 * ({EOT.format(k=60)} / {EOT.format(k=-1)} - 1), 2) AS ret_60
        FROM session_event e
        WHERE e.event = 'CALENDAR_EVENT' AND e.symbol IS NOT NULL
        ORDER BY e.week, e.t, e.symbol""",
    "q2_asof_backward": """
        WITH f AS (
          SELECT seq, week, tick, t, symbol, price, quantity,
                 CASE WHEN taker_id LIKE :team THEN (CASE WHEN aggressor = 'buy' THEN 1 ELSE -1 END)
                      ELSE (CASE WHEN aggressor = 'buy' THEN -1 ELSE 1 END) END AS side   -- my side of the fill
          FROM trade WHERE taker_id LIKE :team OR maker_id LIKE :team)
        SELECT count(*) AS fills,
               round(avg(1e4 * side * (price - mid_before) / mid_before), 3) AS paid_vs_touch_mid_bp,
               round(100.0 * avg(CASE WHEN side * (price - mid_before) >= 0 THEN 1.0 ELSE 0.0 END), 1) AS pct_at_or_through
        FROM (SELECT f.*,
                     (SELECT b.mid FROM book b WHERE b.week = f.week AND b.symbol = f.symbol AND b.seq < f.seq
                      ORDER BY b.seq DESC LIMIT 1) AS mid_before            -- BACKWARD: last book before the fill
              FROM f)
        WHERE mid_before IS NOT NULL""",
    "q2_asof_forward": """
        WITH f AS (
          SELECT seq, week, tick, t, symbol, price, quantity,
                 CASE WHEN taker_id LIKE :team THEN (CASE WHEN aggressor = 'buy' THEN 1 ELSE -1 END)
                      ELSE (CASE WHEN aggressor = 'buy' THEN -1 ELSE 1 END) END AS side
          FROM trade WHERE taker_id LIKE :team OR maker_id LIKE :team)
        SELECT count(*) AS fills,
               round(avg(1e4 * side * (mid_after - price) / price), 3) AS markout_60_bp,
               round(sum(side * quantity * (mid_after - price)), 2) AS markout_60_usd
        FROM (SELECT f.*,
                     (SELECT b.mid FROM book b WHERE b.week = f.week AND b.symbol = f.symbol AND b.tick >= f.tick + 60
                      ORDER BY b.tick ASC, b.seq DESC LIMIT 1) AS mid_after   -- FORWARD: end of tick t+60
              FROM f)
        WHERE mid_after IS NOT NULL""",
    "q3_scan_spread_depth": """
        SELECT symbol, count(*) AS snapshots,
               round(avg(1e4 * spread / mid), 3) AS spread_bp,
               round(avg(best_bid_qty + best_ask_qty), 1) AS touch_depth
        FROM book WHERE mid > 0
        GROUP BY symbol ORDER BY symbol""",
    "q4_point_book": """
        SELECT tick, seq, best_bid, best_ask, mid FROM book
        WHERE week = :week AND symbol = :sym AND tick <= :tick
        ORDER BY tick DESC, seq DESC LIMIT 1""",
    "q4_point_position": """
        SELECT coalesce(sum(CASE WHEN taker_id LIKE :team THEN (CASE WHEN aggressor = 'buy' THEN quantity ELSE -quantity END)
                                 ELSE (CASE WHEN aggressor = 'buy' THEN -quantity ELSE quantity END) END), 0) AS position
        FROM trade WHERE week = :week AND symbol = :sym AND tick <= :tick AND (taker_id LIKE :team OR maker_id LIKE :team)""",
}

# ------------------------------------------------------------------ SQL, DuckDB dialect (over Parquet)
DUCK_VIEWS = """
CREATE OR REPLACE VIEW book  AS SELECT * FROM read_parquet('{w}/book/**/*.parquet',  hive_partitioning = true);
CREATE OR REPLACE VIEW trade AS SELECT * FROM read_parquet('{w}/trade/**/*.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW session_event AS SELECT * FROM read_parquet('{w}/session_event/**/*.parquet', hive_partitioning = true);
-- end-of-tick book: the last snapshot of each (week, symbol, tick) by seq = what a tick-driven backtester sees
CREATE OR REPLACE VIEW book_eot AS SELECT * FROM book
  QUALIFY row_number() OVER (PARTITION BY week, symbol, tick ORDER BY seq DESC) = 1;
"""
DUCK = {
    "q1_event_window": """
        WITH ev AS (SELECT week, kind, symbol, t, tick, pct FROM session_event
                    WHERE event = 'CALENDAR_EVENT' AND symbol <> 'ALL'),
             grid AS (SELECT ev.*, k FROM ev CROSS JOIN (VALUES (-1), (5), (15), (60)) AS ks(k)),
             m AS (SELECT g.week, g.kind, g.symbol, g.t, g.pct, g.k, b.mid
                   FROM grid g ASOF JOIN book_eot b
                     ON g.week = b.week AND g.symbol = b.symbol AND g.tick + g.k >= b.tick)   -- book at or before t+k
        SELECT week, kind, symbol, t, pct AS announced_pct,
               max(CASE WHEN k = -1 THEN mid END) AS mid_before,
               round(100 * (max(CASE WHEN k = 5  THEN mid END) / max(CASE WHEN k = -1 THEN mid END) - 1), 2) AS ret_5,
               round(100 * (max(CASE WHEN k = 15 THEN mid END) / max(CASE WHEN k = -1 THEN mid END) - 1), 2) AS ret_15,
               round(100 * (max(CASE WHEN k = 60 THEN mid END) / max(CASE WHEN k = -1 THEN mid END) - 1), 2) AS ret_60
        FROM m GROUP BY ALL ORDER BY week, t, symbol""",
    "q2_asof_backward": """
        WITH f AS (
          SELECT seq, week, tick, t, symbol, price, quantity,
                 CASE WHEN taker_id LIKE $team THEN (CASE WHEN aggressor = 'buy' THEN 1 ELSE -1 END)
                      ELSE (CASE WHEN aggressor = 'buy' THEN -1 ELSE 1 END) END AS side
          FROM trade WHERE taker_id LIKE $team OR maker_id LIKE $team),
             j AS (SELECT f.*, b.mid AS mid_before
                   FROM f ASOF JOIN book b ON f.week = b.week AND f.symbol = b.symbol AND f.seq > b.seq)  -- BACKWARD
        SELECT count(*) AS fills,
               round(avg(1e4 * side * (price - mid_before) / mid_before), 3) AS paid_vs_touch_mid_bp,
               round(100.0 * avg(CASE WHEN side * (price - mid_before) >= 0 THEN 1.0 ELSE 0.0 END), 1) AS pct_at_or_through
        FROM j""",
    "q2_asof_forward": """
        WITH f AS (
          SELECT seq, week, tick, t, symbol, price, quantity,
                 CASE WHEN taker_id LIKE $team THEN (CASE WHEN aggressor = 'buy' THEN 1 ELSE -1 END)
                      ELSE (CASE WHEN aggressor = 'buy' THEN -1 ELSE 1 END) END AS side
          FROM trade WHERE taker_id LIKE $team OR maker_id LIKE $team),
             j AS (SELECT f.*, b.mid AS mid_after
                   FROM f ASOF JOIN book_eot b ON f.week = b.week AND f.symbol = b.symbol AND f.tick + 60 <= b.tick) -- FORWARD
        SELECT count(*) AS fills,
               round(avg(1e4 * side * (mid_after - price) / price), 3) AS markout_60_bp,
               round(sum(side * quantity * (mid_after - price)), 2) AS markout_60_usd
        FROM j""",
    "q3_scan_spread_depth": SQLITE["q3_scan_spread_depth"],
    "q4_point_book": """
        SELECT tick, seq, best_bid, best_ask, mid FROM book
        WHERE week = $week AND symbol = $sym AND tick <= $tick
        ORDER BY tick DESC, seq DESC LIMIT 1""",
    "q4_point_position": """
        SELECT coalesce(sum(CASE WHEN taker_id LIKE $team THEN (CASE WHEN aggressor = 'buy' THEN quantity ELSE -quantity END)
                                 ELSE (CASE WHEN aggressor = 'buy' THEN -quantity ELSE quantity END) END), 0) AS position
        FROM trade WHERE week = $week AND symbol = $sym AND tick <= $tick AND (taker_id LIKE $team OR maker_id LIKE $team)""",
}


def timed(fn, n: int = 5):
    """Best of n runs (the number to report) and the first run (cold)."""
    first = None; best = float("inf"); out = None
    for i in range(n):
        t0 = time.perf_counter(); out = fn(); dt = time.perf_counter() - t0
        if i == 0:
            first = dt
        best = min(best, dt)
    return best, first, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hw4.sqlite")
    ap.add_argument("--warehouse", default="hw4.parquet")
    ap.add_argument("--team", required=True, help="your team id; matched as a PREFIX, so butter_chicken_pasta matches _trader_1/2/3")
    ap.add_argument("--sym", default="AAPL")
    ap.add_argument("--week", type=int, default=None, help="week for the point lookup (default: the first week)")
    ap.add_argument("--tick", type=int, default=None, help="tick for the point lookup (default: the week's event tick)")
    ap.add_argument("--repeat", type=int, default=5)
    a = ap.parse_args()

    lite = sqlite3.connect(a.db)
    duck = duckdb.connect()
    duck.execute(DUCK_VIEWS.format(w=a.warehouse))

    # point-lookup target: the first CALENDAR_EVENT of the first week unless given
    week, tick = a.week, a.tick
    if week is None or tick is None:
        row = lite.execute("SELECT week, tick FROM session_event WHERE event='CALENDAR_EVENT' "
                           "ORDER BY week, tick LIMIT 1").fetchone()
        week = week or row[0]; tick = tick or row[1]
    params_l = {"team": a.team + "%", "sym": a.sym, "week": week, "tick": tick}
    params_d = {"team": a.team + "%", "sym": a.sym, "week": week, "tick": tick}

    ids = [r[0] for r in lite.execute("SELECT DISTINCT taker_id FROM trade WHERE taker_id LIKE ? UNION "
                                      "SELECT DISTINCT maker_id FROM trade WHERE maker_id LIKE ?", (a.team + "%", a.team + "%"))]
    print(f"team ids matched by '{a.team}%': {ids}")
    results = []
    for name in SQLITE:
        bl, fl, out_l = timed(lambda: lite.execute(SQLITE[name], params_l).fetchall(), a.repeat)
        pd_ = {k: v for k, v in params_d.items() if f"${k}" in DUCK[name]}   # DuckDB wants exactly the used params
        bd, fd, out_d = timed(lambda: duck.execute(DUCK[name], pd_).fetchall(), a.repeat)
        same = [tuple(round(v, 4) if isinstance(v, float) else v for v in r) for r in out_l] == \
               [tuple(round(v, 4) if isinstance(v, float) else v for v in r) for r in out_d]
        results.append((name, len(out_l), bl, fl, bd, fd, same))
        print(f"\n== {name}  (SQLite {1e3*bl:.2f} ms · DuckDB/Parquet {1e3*bd:.2f} ms · best of {a.repeat} · same answer: {same})")
        cols = [d[0] for d in duck.execute(DUCK[name], pd_).description]
        print("   " + " | ".join(cols))
        for r in out_d[:12]:
            print("   " + " | ".join(str(v) for v in r))
        if len(out_d) > 12:
            print(f"   ... {len(out_d) - 12} more rows")
        if not same:
            print("   SQLite rows:", out_l[:5])

    print("\n### Query x store timings (best of %d runs; first run in brackets)\n" % a.repeat)
    print("| query | rows | SQLite ms | DuckDB/Parquet ms | winner | same answer |")
    print("|---|---:|---:|---:|---|---|")
    for name, n, bl, fl, bd, fd, same in results:
        win = f"DuckDB {bl / bd:.1f}x" if bd < bl else f"SQLite {bd / bl:.1f}x"
        print(f"| {name} | {n} | {1e3*bl:.2f} ({1e3*fl:.1f}) | {1e3*bd:.2f} ({1e3*fd:.1f}) | {win} | {same} |")
    print(f"\npoint lookup target: week {week}, {a.sym}, tick {tick} · team {a.team}")


if __name__ == "__main__":
    main()