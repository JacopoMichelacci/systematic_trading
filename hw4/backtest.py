"""HW4 Task 3 — an event-driven backtester on the LOGICAL clock, reading the Parquet tape.

    python backtest.py --warehouse hw4.parquet --week 2 --metrics
    python backtest.py --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed inf
    python backtest.py --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed 20     # same digest
    python backtest.py --week 2 --latency-table            # latency 0 / 1 / 2 ticks
    python backtest.py --week 2 --fill-table               # touch vs depth (sweep) fills
    python backtest.py --week 2 --strategy mine            # your own bot (see MyStrategy / ArenaAdapter below)

The loop, once per scenario tick t (the logical clock):
  1. apply every book snapshot of tick t, in seq order              (data: only tick <= t is ever visible)
  2. fill the orders due at t against the FIRST book of their symbol in tick t   (an order sent at t-1 reaches the
     venue during tick t: --latency-ticks, default 1); every fill is a taker fill and pays --fee
  3. deliver the session events of tick t -> strategy.on_event(ev, now=t)
  4. strategy.on_tick(now=t, market, pf) -> Signals -> orders due at t + latency
  5. mark to the mid, record equity
The strategy is TOLD the time (now=t); it never asks the machine. Pacing (--speed) reads the wall clock only to
decide WHEN a tick is delivered, never WHAT is decided, so the fill digest is identical at every speed.
Sharpe = mean / std(ddof=1) of per-tick equity changes; NOT annualised (one block is not a year).
"""
from __future__ import annotations

import argparse
import hashlib
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

import pyarrow.dataset as ds

TAKER_FEE = 0.003          # the hosted venue's taker rate (0.30 %); pass --fee 0.0015 for the local default


# ------------------------------------------------------------------ data
@dataclass
class Book:
    seq: int
    tick: int
    t: int
    ts: float
    symbol: str
    bids: list            # [(px, qty), ...] best first
    asks: list
    mid: float


def load_tape(warehouse: str, week: int, from_t: int | None, to_t: int | None, need_depth: bool):
    flt = (ds.field("week") == week)
    if from_t is not None:
        flt = flt & (ds.field("t") >= from_t)
    if to_t is not None:
        flt = flt & (ds.field("t") <= to_t)
    book = ds.dataset(f"{warehouse}/book", format="parquet", partitioning="hive") \
             .to_table(filter=flt, columns=["seq", "tick", "t", "ts", "symbol", "best_bid", "best_bid_qty",
                                            "best_ask", "best_ask_qty", "mid"]).to_pylist()
    depth = defaultdict(lambda: ([], []))
    if need_depth and book:
        lo, hi = min(r["tick"] for r in book), max(r["tick"] for r in book)      # book_level has tick, not t
        dflt = (ds.field("week") == week) & (ds.field("tick") >= lo) & (ds.field("tick") <= hi)
        for r in ds.dataset(f"{warehouse}/book_level", format="parquet", partitioning="hive") \
                   .to_table(filter=dflt, columns=["seq", "side", "lvl", "px", "qty"]).to_pylist():
            depth[r["seq"]][0 if r["side"] == "B" else 1].append((r["lvl"], r["px"], r["qty"]))
    events = ds.dataset(f"{warehouse}/session_event", format="parquet", partitioning="hive") \
               .to_table(filter=flt & (ds.field("event") == "CALENDAR_EVENT")).to_pylist()
    books_at, events_at, ts_at = defaultdict(list), defaultdict(list), {}
    for r in sorted(book, key=lambda r: (r["t"], r["seq"])):
        if r["best_bid"] is None or r["best_ask"] is None:
            continue
        if need_depth and depth[r["seq"]][0]:
            bids = [(px, q) for _, px, q in sorted(depth[r["seq"]][0])]
            asks = [(px, q) for _, px, q in sorted(depth[r["seq"]][1])]
        else:
            bids, asks = [(r["best_bid"], r["best_bid_qty"] or 0)], [(r["best_ask"], r["best_ask_qty"] or 0)]
        books_at[r["t"]].append(Book(r["seq"], r["tick"], r["t"], r["ts"], r["symbol"], bids, asks, r["mid"]))
        ts_at.setdefault(r["t"], r["ts"])
    for e in sorted(events, key=lambda e: e["seq"]):
        events_at[e["t"]].append(e)
    ticks = sorted(books_at)
    return ticks, books_at, events_at, ts_at


# ------------------------------------------------------------------ market / portfolio the strategy sees
class Market:
    """Read-only view: only snapshots with tick <= now have been applied."""
    def __init__(self):
        self.books: dict[str, Book] = {}
        self.hist: dict[str, list[float]] = defaultdict(list)       # mids, one per tick per symbol

    def symbols(self): return sorted(self.books)
    def best_bid(self, s): b = self.books.get(s); return b.bids[0][0] if b and b.bids else None
    def best_ask(self, s): b = self.books.get(s); return b.asks[0][0] if b and b.asks else None
    def mid_price(self, s): b = self.books.get(s); return b.mid if b else None
    def spread(self, s):
        bb, ba = self.best_bid(s), self.best_ask(s)
        return (ba - bb) if bb is not None and ba is not None else None
    def prices(self, s): return self.hist[s]
    def order_book_imbalance(self, s):
        b = self.books.get(s)
        if not b or not b.bids or not b.asks: return 0.0
        bq, aq = b.bids[0][1], b.asks[0][1]
        return (bq - aq) / (bq + aq) if bq + aq else 0.0


@dataclass
class Portfolio:
    cash: float
    positions: dict = field(default_factory=lambda: defaultdict(int))
    fills: list = field(default_factory=list)
    fees: float = 0.0

    def net_worth(self, market: Market) -> float:
        return self.cash + sum(q * (market.mid_price(s) or 0.0) for s, q in self.positions.items())
    def can_buy(self, s, qty, px): return self.cash >= qty * px * (1 + TAKER_FEE)
    def can_sell(self, s, qty): return self.positions[s] >= qty


@dataclass
class Signal:
    symbol: str
    side: str                 # 'buy' | 'sell'
    quantity: int
    price: float | None = None   # a limit cap: a buy fills only at asks <= price; None = marketable


# ------------------------------------------------------------------ strategies
class Strategy:
    name = "base"
    def on_event(self, ev: dict, now: int) -> None: ...
    def on_tick(self, now: int, market: Market, pf: Portfolio) -> list[Signal]: return []


class DoNothing(Strategy):
    name = "do_nothing"


class BuyHold(Strategy):
    """Baseline 2: on the first tick with books, put 90 % of cash into equal $ per symbol, then hold."""
    name = "buy_hold"
    def __init__(self): self.done = False
    def on_tick(self, now, market, pf):
        if self.done or not market.symbols(): return []
        self.done = True
        per = 0.9 * pf.cash / len(market.symbols())
        return [Signal(s, "buy", int(per // market.best_ask(s))) for s in market.symbols()
                if market.best_ask(s) and int(per // market.best_ask(s)) > 0]


class TickMomentum(Strategy):
    """Baseline 3, the naive rule: long 100 when the mid is above its L-tick mean, flat otherwise (long-only)."""
    name = "momentum"
    def __init__(self, lookback=20, qty=100): self.L, self.qty = lookback, qty
    def on_tick(self, now, market, pf):
        out = []
        for s in market.symbols():
            h = market.prices(s)
            if len(h) < self.L: continue
            up = h[-1] > sum(h[-self.L:]) / self.L
            pos = pf.positions[s]
            if up and pos <= 0 and pf.can_buy(s, self.qty, market.best_ask(s)):
                out.append(Signal(s, "buy", self.qty))
            elif not up and pos > 0:
                out.append(Signal(s, "sell", pos))
        return out


class EventRide(Strategy):
    """The event strategy: trade the CALENDAR_EVENT message. Good news -> buy 100 up to pre_mid*(1+pct/2), exit after
    `hold` ticks. Bad news -> sell what we hold, buy back after `hold`. Skip events whose edge < two taker fees."""
    name = "event_ride"
    def __init__(self, qty=100, hold=15, fee=TAKER_FEE, realclock=False):
        self.qty, self.hold, self.fee, self.realclock = qty, hold, fee, realclock
        self.pending_events, self.open = [], []          # open: dicts with symbol, side, qty, t_in, wall
    def on_event(self, ev, now):
        pct = ev.get("pct")
        if pct is None or ev.get("symbol") in (None, "ALL"): return   # market-wide events have no symbol
        if abs(pct) > 1: pct = pct / 100                  # the tape records a FRACTION (0.062 = +6.2 %); accept either
        if abs(pct) < 2 * self.fee: return                # designed edge must beat the round trip (2 x 0.30 %)
        self.pending_events.append({**ev, "pct": pct})
    def due(self, p, now):
        if self.realclock:                                # the planted bug (lab step 7): reads the WALL clock
            return time.time() - p["wall"] >= self.hold * 1.06
        return now - p["t_in"] >= self.hold               # the logical clock
    def on_tick(self, now, market, pf):
        out = []
        for ev in self.pending_events:
            s, pct = ev["symbol"], ev["pct"]                # already a fraction (see on_event)
            mid = market.mid_price(s)
            if mid is None: continue
            if pct > 0 and pf.can_buy(s, self.qty, market.best_ask(s) or mid):
                out.append(Signal(s, "buy", self.qty, price=mid * (1 + pct / 2)))
                self.open.append({"symbol": s, "side": "buy", "qty": self.qty, "t_in": now, "wall": time.time()})
            elif pct < 0 and pf.positions[s] > 0:
                q = pf.positions[s]
                out.append(Signal(s, "sell", q))
                self.open.append({"symbol": s, "side": "sell", "qty": q, "t_in": now, "wall": time.time()})
        self.pending_events = []
        still = []
        for p in self.open:
            if self.due(p, now):
                if p["side"] == "buy" and pf.positions[p["symbol"]] > 0:
                    out.append(Signal(p["symbol"], "sell", min(p["qty"], pf.positions[p["symbol"]])))
                elif p["side"] == "sell" and market.best_ask(p["symbol"]) and pf.can_buy(p["symbol"], p["qty"], market.best_ask(p["symbol"])):
                    out.append(Signal(p["symbol"], "buy", p["qty"]))
            else:
                still.append(p)
        self.open = still
        return out


class ArenaAdapter(Strategy):
    """Run YOUR arena.Trader subclass unchanged: it gets a Market/Portfolio with the same read methods the SDK
    exposes (symbols, prices, best_bid, best_ask, mid_price, spread, order_book_imbalance; cash, positions,
    can_buy, can_sell) and its on_tick returns Signal | None.  Example:
        from team.trader import MyTrader
        strategy = ArenaAdapter(MyTrader())
    """
    name = "mine"
    def __init__(self, trader): self.trader = trader
    def on_event(self, ev, now):
        if hasattr(self.trader, "on_event"):                 # same shape the SDK delivers: (event, message, data)
            self.trader.on_event("CALENDAR_EVENT", ev.get("message", ""),
                                 {"kind": ev.get("kind"), "symbol": ev.get("symbol"), "pct": ev.get("pct"),
                                  "tick": ev.get("tick"), "t": now})
    def on_tick(self, now, market, pf):
        sig = self.trader.on_tick(market, pf)
        if sig is None: return []
        return [Signal(sig.symbol, sig.side, int(sig.quantity), getattr(sig, "price", None))]


class MyStrategy(Strategy):
    """<<< PASTE YOUR BOT'S DECISION LOGIC HERE (or use ArenaAdapter above with your real class). >>>
    It must only read `market` and `pf` and the `now` it is given. No time.time(), no sockets."""
    name = "mine"
    def on_tick(self, now, market, pf):
        return []


def make_mine() -> Strategy:
    """Our real bot, team/trader.py, run unchanged through the adapter (needs PYTHONPATH=../engine:..)."""
    try:
        from team.trader import MyTrader                     # the class we run live
        return ArenaAdapter(MyTrader())
    except Exception as e:                                   # noqa: BLE001 - say why, then fall back
        print(f"  (could not import team.trader.MyTrader: {e!r}; using the MyStrategy placeholder)")
        return MyStrategy()


def make_strategy(name: str, fee: float, realclock: bool, lookback: int) -> Strategy:
    return {"do_nothing": DoNothing, "buy_hold": BuyHold,
            "momentum": lambda: TickMomentum(lookback=lookback),
            "event_ride": lambda: EventRide(fee=fee, realclock=realclock),
            "mine": make_mine}[name]()


# ------------------------------------------------------------------ execution
def fill_order(sig: Signal, book: Book, mode: str, fee_rate: float, pf: Portfolio, long_only: bool, max_pos: int, now: int):
    """A taker fill against `book`: 'touch' takes the best level only, 'sweep' walks the recorded depth."""
    levels = book.asks if sig.side == "buy" else book.bids
    if not levels: return
    want = sig.quantity
    if sig.side == "buy":
        room = max_pos - pf.positions[sig.symbol]
        want = min(want, room)
    elif long_only:
        want = min(want, pf.positions[sig.symbol])
    if want <= 0: return
    filled, notional = 0, 0.0
    for px, qty in (levels[:1] if mode == "touch" else levels):
        if sig.price is not None and ((sig.side == "buy" and px > sig.price) or (sig.side == "sell" and px < sig.price)):
            break
        take = int(min(want - filled, qty))
        if sig.side == "buy" and pf.cash - notional < take * px * (1 + fee_rate):
            take = int((pf.cash - notional) // (px * (1 + fee_rate)))
        if take <= 0: break
        filled += take; notional += take * px
        if filled >= want: break
    if filled == 0: return
    fee = notional * fee_rate
    signed = filled if sig.side == "buy" else -filled
    if sig.side == "buy":
        pf.cash -= notional + fee                                   # pay the price and the taker fee
    else:
        pf.cash += notional - fee                                   # receive the price, pay the fee
    pf.positions[sig.symbol] += signed
    pf.fees += fee
    pf.fills.append((now, sig.symbol, sig.side, filled, round(notional / filled, 4), round(fee, 4)))


def run(ticks, books_at, events_at, ts_at, strategy: Strategy, *, cash=250_000.0, granted=20, fee=TAKER_FEE,
        latency=1, fill="sweep", long_only=True, max_pos=400, speed=math.inf, split_t=None):
    market, pf = Market(), Portfolio(cash)
    pending: dict[int, list[Signal]] = defaultdict(list)
    equity, eq_t = [], []
    t_wall0, ts0 = time.perf_counter(), ts_at[ticks[0]]
    first_tick = True
    for t in ticks:
        if speed != math.inf:                                           # pacing: the only wall-clock read
            wait = (ts_at[t] - ts0) / speed - (time.perf_counter() - t_wall0)
            if wait > 0: time.sleep(wait)
        first_book_of_tick: dict[str, Book] = {}
        for b in books_at[t]:                                           # 1. snapshots of tick t, in seq order
            market.books[b.symbol] = b
            first_book_of_tick.setdefault(b.symbol, b)
        for s, b in market.books.items():
            market.hist[s].append(b.mid)
        if first_tick and granted:                                      # the venue grants a few shares per name
            for s in market.books: pf.positions[s] += granted
            first_tick = False
        carry = []
        for sig in pending.pop(t, []):                                  # 2. orders due at t: fill on the FIRST book of t
            b = first_book_of_tick.get(sig.symbol)
            if b is None: carry.append(sig); continue
            fill_order(sig, b, fill, fee, pf, long_only, max_pos, t)
        if carry: pending[t + 1].extend(carry)
        for ev in events_at.get(t, []):                                 # 3. events of tick t
            strategy.on_event(ev, now=t)
        for sig in strategy.on_tick(now=t, market=market, pf=pf):       # 4. decisions see only tick <= t
            if latency == 0:                                            # latency 0: fill on the book you decided on
                fill_order(sig, market.books[sig.symbol], fill, fee, pf, long_only, max_pos, t)
            else:
                pending[t + latency].append(sig)                        # otherwise: due at t + L
        equity.append(pf.net_worth(market)); eq_t.append(t)             # 5. mark to mid
    digest = hashlib.sha1(repr(pf.fills).encode()).hexdigest()[:12]
    return {"equity": equity, "eq_t": eq_t, "fills": pf.fills, "fees": pf.fees, "digest": digest,
            "wall": time.perf_counter() - t_wall0, "split_t": split_t}


def metrics(res: dict) -> dict:
    eq, et = res["equity"], res["eq_t"]
    split = res["split_t"]
    pnl = eq[-1] - eq[0]
    i_split = next((i for i, t in enumerate(et) if t >= split), len(eq)) if split is not None else len(eq)
    is_pnl = (eq[i_split - 1] - eq[0]) if i_split > 0 else 0.0
    oos_pnl = pnl - is_pnl
    peak, mdd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x); mdd = max(mdd, peak - x)
    d = [b - a for a, b in zip(eq, eq[1:])]
    sd1 = statistics.stdev(d) if len(d) > 1 else 0.0
    sd0 = statistics.pstdev(d) if d else 0.0
    mean = statistics.fmean(d) if d else 0.0
    sharpe1 = mean / sd1 if sd1 else 0.0
    return {"pnl": pnl, "is": is_pnl, "oos": oos_pnl, "mdd": mdd, "fills": len(res["fills"]), "fees": res["fees"],
            "sharpe_tick": sharpe1, "sharpe_sqrtN": sharpe1 * math.sqrt(len(d)) if d else 0.0,
            "sd1": sd1, "sd0": sd0, "mean": mean, "digest": res["digest"], "wall": res["wall"]}


def table(rows: list[tuple[str, dict]], show_metrics: bool) -> None:
    print("\n| strategy | P&L | IS | OOS | max DD | fills | fees | digest | wall s |")
    print("|---|---:|---:|---:|---:|---:|---:|---|---:|")
    for name, m in rows:
        print(f"| {name} | {m['pnl']:+,.0f} | {m['is']:+,.0f} | {m['oos']:+,.0f} | {m['mdd']:,.0f} | {m['fills']} | "
              f"{m['fees']:,.0f} | {m['digest']} | {m['wall']:.1f} |")
    if show_metrics:
        print("\n| strategy | mean $/tick | std ddof=1 | std ddof=0 | Sharpe/tick (ddof=1) | x sqrt(N) |")
        print("|---|---:|---:|---:|---:|---:|")
        for name, m in rows:
            print(f"| {name} | {m['mean']:+.4f} | {m['sd1']:.3f} | {m['sd0']:.3f} | {m['sharpe_tick']:+.4f} | {m['sharpe_sqrtN']:+.2f} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default="hw4.parquet")
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--strategy", default="all", help="all | do_nothing | buy_hold | momentum | event_ride | mine")
    ap.add_argument("--fee", type=float, default=TAKER_FEE)
    ap.add_argument("--latency-ticks", type=int, default=1)
    ap.add_argument("--fill", default="sweep", choices=["touch", "sweep"])
    ap.add_argument("--cash", type=float, default=250_000.0)
    ap.add_argument("--granted", type=int, default=0, help="shares granted per name at the open (0: the seat starts flat, as ours did on Oct 23)")
    ap.add_argument("--max-pos", type=int, default=400)
    ap.add_argument("--shorts", action="store_true", help="allow short positions (default long-only)")
    ap.add_argument("--split-t", type=int, default=None, help="IS/OOS split tick, chosen BEFORE looking (default: midpoint)")
    ap.add_argument("--from-t", type=int, default=None)
    ap.add_argument("--to-t", type=int, default=None)
    ap.add_argument("--speed", type=float, default=math.inf, help="replay pacing on the tape's ts: inf = no sleeping")
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--metrics", action="store_true")
    ap.add_argument("--latency-table", action="store_true")
    ap.add_argument("--fill-table", action="store_true")
    ap.add_argument("--realclock", action="store_true", help="plant the wall-clock exit timer in event_ride (the bug)")
    ap.add_argument("--show-fills", action="store_true", help="print every fill (t, symbol, side, qty, price, fee)")
    a = ap.parse_args()

    t0 = time.perf_counter()
    ticks, books_at, events_at, ts_at = load_tape(a.warehouse, a.week, a.from_t, a.to_t, need_depth=(a.fill == "sweep" or a.fill_table))
    if not ticks: raise SystemExit("no ticks in that range")
    split = a.split_t if a.split_t is not None else ticks[len(ticks) // 2]
    n_snaps = sum(len(v) for v in books_at.values())
    print(f"tape: week {a.week}, t {ticks[0]}..{ticks[-1]} ({len(ticks):,} ticks, {n_snaps:,} book snapshots, "
          f"{sum(len(v) for v in events_at.values())} events) loaded from Parquet in {1e3*(time.perf_counter()-t0):.0f} ms · "
          f"fee {100*a.fee:.2f}% · latency {a.latency_ticks} tick · fill {a.fill} · IS t<{split} / OOS t>={split}")
    common = dict(cash=a.cash, granted=a.granted, fee=a.fee, long_only=not a.shorts, max_pos=a.max_pos, split_t=split)
    names = ["mine", "event_ride", "do_nothing", "buy_hold", "momentum"] if a.strategy == "all" else [a.strategy]

    def go(name, **kw):
        kw2 = {**common, "latency": a.latency_ticks, "fill": a.fill, "speed": a.speed, **kw}
        res = run(ticks, books_at, events_at, ts_at, make_strategy(name, a.fee, a.realclock, a.lookback), **kw2)
        if a.show_fills:                                          # (t, symbol, side, qty, avg price, fee)
            print(f"  fills of {name} {kw or ''}: {len(res['fills'])}")
            for f in res["fills"][:12]:
                print(f"    t={f[0]:<5} {f[1]:<6} {f[2]:<4} {f[3]:>4} @ {f[4]:.4f}  fee {f[5]:.4f}")
            if len(res["fills"]) > 12:
                print(f"    ... {len(res['fills']) - 12} more")
        return metrics(res)

    rows = [(n, go(n)) for n in names]
    table(rows, a.metrics)
    if a.speed != math.inf:
        for n, m in rows:
            print(f"speed {a.speed:g}{' REALCLOCK' if a.realclock else ''}: {n} P&L {m['pnl']:+.2f} · {m['fills']} fills · digest {m['digest']} · wall {m['wall']:.1f} s")
    if a.latency_table:
        print("\nlatency sensitivity (ticks on the logical clock):")
        table([(f"{n} latency={L}", go(n, latency=L)) for n in names for L in (0, 1, 2)], False)
    if a.fill_table:
        print("\nfill-model sensitivity:")
        table([(f"{n} fill={f}", go(n, fill=f)) for n in names for f in ("touch", "sweep")], False)


if __name__ == "__main__":
    main()
