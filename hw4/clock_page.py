"""HW4 Task 4 — the numbers for the "logical vs real clock" page, from the tape of the live block.

    python clock_page.py --db hw4.sqlite --week 4 --team butter_chicken_pasta --code ../team

Prints, for the week's block:
  A. the wall-clock length of a tick (SESSION_OPEN -> SESSION_CLOSED, p1 / median / p99 / max), lines per tick,
     snapshots per tick for the busiest symbol, and the "1 tick = 1 s" drift at each scheduled event
  B. your LIVE fills from the recording: count, shares, average price, fees, realised + marked P&L
  C. your reaction to each CALENDAR_EVENT: ticks (and wall seconds) from the event line to your first fill in
     that symbol
  D. every time.time / time.monotonic / datetime.now / perf_counter in your code, with a first guess at
     "decision path" vs "edge" — you confirm each one
Then run `python backtest.py --week <w> --strategy mine` for the replayed numbers (section B's twin).
The team is matched as a prefix (LIKE 'butter_chicken_pasta%'), so all three trader ids count.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import statistics


def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hw4.sqlite")
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--team", required=True, help="team id prefix, e.g. butter_chicken_pasta")
    ap.add_argument("--code", default=None, help="folder of your bot's source, for the clock-read grep")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    w = a.week

    # ---- A. the clocks ------------------------------------------------------------------------------------
    print(f"## A. Two clocks on the week-{w} tape\n")
    ev = con.execute("SELECT event, tick, t, ts FROM session_event WHERE week=? AND event IN ('SESSION_OPEN','SESSION_CLOSED') "
                     "ORDER BY seq", (w,)).fetchall()
    for e in ev:
        print(f"- {e[0]:15s} tick {e[1]:,}  t {e[2]}  ts {e[3]:.3f}")
    per_tick = con.execute("SELECT tick, MIN(ts), COUNT(*) FROM book WHERE week=? GROUP BY tick ORDER BY tick", (w,)).fetchall()
    if len(per_tick) > 2:
        deltas = [b[1] - a_[1] for a_, b in zip(per_tick, per_tick[1:]) if b[1] > a_[1]]
        span = per_tick[-1][1] - per_tick[0][1]
        print(f"- {len(per_tick):,} ticks in {span:,.1f} s of wall clock: **{span / len(per_tick):.3f} s per tick** on average")
        print(f"- one tick of wall clock: p1 {pct(deltas, .01):.3f} s · median {statistics.median(deltas):.3f} s · "
              f"p99 {pct(deltas, .99):.3f} s · max {max(deltas):.3f} s")
        lines = [c for _, _, c in per_tick]
        print(f"- book snapshots per tick: median {statistics.median(lines):.0f}, max {max(lines)}")
    busiest = con.execute("SELECT symbol, tick, COUNT(*) c FROM book WHERE week=? GROUP BY symbol, tick ORDER BY c DESC LIMIT 1", (w,)).fetchone()
    if busiest:
        print(f"- most snapshots of one symbol in one tick: {busiest[0]} at tick {busiest[1]:,}: {busiest[2]} "
              f"(order inside a tick = seq)")
    t0 = con.execute("SELECT MIN(ts) FROM book WHERE week=? AND t>=1", (w,)).fetchone()[0]
    print("\n| event | symbol | t | tape ts - open (s) | '1 tick = 1 s' says | drift (s) |")
    print("|---|---|---:|---:|---:|---:|")
    for kind, sym, t, ts in con.execute("SELECT kind, symbol, t, ts FROM session_event WHERE week=? AND event='CALENDAR_EVENT' "
                                        "ORDER BY seq", (w,)).fetchall():
        print(f"| {kind} | {sym} | {t} | {ts - t0:,.1f} | {t:,.1f} | {ts - t0 - t:+.1f} |")

    # ---- B. live fills from the recording ---------------------------------------------------------------
    print(f"\n## B. {a.team}'s LIVE fills on the week-{w} tape\n")
    fills = con.execute("""
        SELECT seq, tick, t, ts, symbol, price, quantity, fee,
               CASE WHEN taker_id LIKE :team THEN aggressor ELSE (CASE WHEN aggressor='buy' THEN 'sell' ELSE 'buy' END) END AS my_side,
               CASE WHEN taker_id LIKE :team THEN 'taker' ELSE 'maker' END AS role
        FROM trade WHERE week=:w AND (taker_id LIKE :team OR maker_id LIKE :team) ORDER BY seq""", {"team": a.team + "%", "w": w}).fetchall()
    if not fills:
        print(f"no fills for team_id '{a.team}' in week {w}: check the id (taker_id/maker_id values present: "
              f"{[r[0] for r in con.execute('SELECT DISTINCT taker_id FROM trade WHERE week=? LIMIT 12', (w,))]})")
    else:
        cash, pos, fees, shares = 0.0, {}, 0.0, 0
        for _, _, _, _, sym, px, q, fee, side, role in fills:
            sgn = 1 if side == "buy" else -1
            cash -= sgn * q * px
            pos[sym] = pos.get(sym, 0) + sgn * q
            shares += q
            if role == "taker" and fee: fees += fee          # the tape records the taker's fee; maker rebates are not on it
        last_mid = {s: con.execute("SELECT mid FROM book WHERE week=? AND symbol=? ORDER BY tick DESC, seq DESC LIMIT 1", (w, s)).fetchone()[0]
                    for s in pos}
        marked = sum(q * last_mid[s] for s, q in pos.items())
        n_tk = sum(1 for f in fills if f[9] == "taker")
        print(f"- fills: **{len(fills)}** ({n_tk} as taker, {len(fills) - n_tk} as maker) · shares {shares:,.0f} · "
              f"average price {sum(f[5] * f[6] for f in fills) / shares:,.4f}")
        print(f"- taker fees on the tape: {fees:,.2f} · net cash from trading {cash:+,.2f} · open positions marked at the "
              f"last mid {marked:+,.2f}")
        print(f"- **trading P&L (cash + mark - taker fees): {cash + marked - fees:+,.2f}**  "
              f"(compare with `python backtest.py --week {w} --strategy mine`)")
        print(f"- end positions: {dict(sorted(pos.items()))}")

        # ---- C. reaction time per event --------------------------------------------------------------------
        print(f"\n## C. Ticks from each CALENDAR_EVENT to {a.team}'s first fill in that symbol\n")
        print("| event | symbol | pct | event t | first fill t | reaction (ticks) | reaction (wall s) |")
        print("|---|---|---:|---:|---:|---:|---:|")
        for seq_e, t_e, ts_e, kind, sym, pct_ in con.execute(
                "SELECT seq, t, ts, kind, symbol, pct FROM session_event WHERE week=? AND event='CALENDAR_EVENT' ORDER BY seq", (w,)):
            f = next((f for f in fills if f[0] > seq_e and f[4] == sym), None)
            if f is None:
                print(f"| {kind} | {sym} | {pct_} | {t_e} | — | no fill | — |")
            else:
                print(f"| {kind} | {sym} | {pct_} | {t_e} | {f[2]} | {f[2] - t_e} | {f[3] - ts_e:.1f} |")

    # ---- D. where the code reads the wall clock ----------------------------------------------------------
    if a.code:
        print(f"\n## D. Wall-clock reads in {a.code}\n")
        pat = re.compile(r"time\.time\(|time\.monotonic\(|datetime\.now\(|perf_counter\(|utcnow\(")
        edge = re.compile(r"timeout|reconnect|backoff|heartbeat|ping|log|print|sleep|pace|retry|elapsed|latency|perf", re.I)
        print("| file:line | code | first guess | you confirm |")
        print("|---|---|---|---|")
        n = 0
        for root, _, files in os.walk(a.code):
            for fn in files:
                if not fn.endswith(".py"): continue
                path = os.path.join(root, fn)
                for i, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
                    if pat.search(line):
                        n += 1
                        guess = "edge (timeout/log/pacing)" if edge.search(line) else "**DECISION PATH? check**"
                        print(f"| {path}:{i} | `{line.strip()[:70]}` | {guess} | |")
        if n == 0:
            print("| — | no wall-clock reads found | | |")
        print("\nRule: a read is fine at an edge (socket timeout, reconnect backoff, heartbeat, log line, replay pacing); "
              "a read inside entry/exit/cooldown/'once a minute' logic makes the backtest depend on the CPU.")


if __name__ == "__main__":
    main()