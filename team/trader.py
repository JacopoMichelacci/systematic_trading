"""Week 4 long-only trader for butter_chicken_pasta.

What the week-4 block grades (Phase 1, Part A):
  1. a bid in the ORCA bookbuild            -> on_ipo()
  2. orders after the opening cross,
     not only during the pre-open           -> in_preopen gate + post-cross buy
  3. at most 10 rejected orders             -> _NoFlattenBot (no close-time
                                               market orders), tiny marketable
                                               buys only

On top of that it keeps the week-3 behaviour: a 1-share marketable BUY when
a scheduled earnings print fires on EVENT_SYMBOLS, and a buy-only MSFT 5/20
moving-average cross. It never submits a sell.

Run: TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

NOTE: ``CALENDAR`` announces *future* events. ``CALENDAR_EVENT`` is emitted
when scheduled earnings actually fire; ``SHOCK`` is for injected shocks.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import pathlib

from arena import Signal, Trader
from arena.trader import _AdaptedTraderBot

logger = logging.getLogger("team.trader")

PREFERRED_SYMBOL = "MSFT"
QUANTITY = 1
MAX_MOMENTUM_ORDERS = 3
# Account for venue taker fees and stale best-ask prices, rather than relying
# on Portfolio.can_buy's smaller 0.1% buffer alone.
BUY_CASH_BUFFER = 1.01
# Week 4 calendar: earnings AMZN @ t350, GOOGL @ t800.
EVENT_SYMBOLS = frozenset(("AMZN", "GOOGL"))
# ORCA bookbuild: 200 shares at the top of the range is ~$5.6k on a $250k
# seat — far below both exchange caps (deal size, and cash at max_price).
IPO_SHARES = 200
# Book snapshots to let the brokers rebuild the book after the cross before
# sending the guaranteed post-cross order.
POST_CROSS_WAIT_SNAPSHOTS = 5
# Backstop: the opening auction is 12 ticks. If we are somehow still flagged
# "pre-open" this many snapshots later, assume the AUCTION_RESULT was missed.
PREOPEN_MAX_SNAPSHOTS = 40


class MyTrader(Trader):
    """Week 4: BUY-only; IPO bid, post-cross order, earnings and momentum."""

    def __init__(self) -> None:
        self.pending_events: list[dict] = []
        self.seen_events: set[tuple] = set()
        self.last_momentum_signal: tuple | None = None
        self.momentum_orders: int = 0
        # Opening-auction state (driven by on_event, read by on_tick).
        self.in_preopen: bool = False
        self.preopen_snapshots: int | None = None
        self.post_cross_done: bool = False
        self.cross_snapshots: int | None = None

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _safe_buy(market, portfolio, symbol: str) -> Signal | None:
        """Return a 1-share marketable limit BUY only when it is affordable.

        No sell signal can be constructed here, even for a negative shock.
        """
        ask = market.best_ask(symbol)
        if (not isinstance(ask, (int, float)) or isinstance(ask, bool)
                or not math.isfinite(ask) or ask <= 0):
            return None
        if (not isinstance(portfolio.cash, (int, float))
                or not math.isfinite(portfolio.cash)
                or portfolio.cash < ask * QUANTITY * BUY_CASH_BUFFER):
            return None
        if not portfolio.can_buy(symbol, QUANTITY, ask):
            return None
        return Signal(symbol=symbol, side="buy", quantity=QUANTITY, price=ask)

    @staticmethod
    def _snapshots(market) -> int:
        """How many MSFT book snapshots we have seen (our clock, ~1/s)."""
        return len(market.prices(PREFERRED_SYMBOL))

    # ── strategies ──────────────────────────────────────────────────────

    def handle_shock(self, market, portfolio) -> Signal | None:
        """Act on the earliest *fired* event. Retain it until an order is sent."""
        if not self.pending_events:
            return None
        shock = self.pending_events[0]
        signal = self._safe_buy(market, portfolio, shock["symbol"])
        if signal is None:
            # No bid/ask or insufficient cash: don't attempt a doomed order.
            logger.debug("Shock %s pending: no affordable ask yet", shock["symbol"])
            return None
        self.pending_events.pop(0)
        logger.info("SHOCK RESPONSE: BUY %d %s @ %.2f (event=%s, pct=%s)",
                    signal.quantity, signal.symbol, signal.price,
                    shock["event"], shock["pct"])
        return signal

    def post_cross_buy(self, market, portfolio) -> Signal | None:
        """One guaranteed 1-share buy shortly after the opening cross."""
        if self.post_cross_done:
            return None
        n = self._snapshots(market)
        if self.cross_snapshots is None:
            self.cross_snapshots = n           # first tick after the cross
            return None
        if n - self.cross_snapshots < POST_CROSS_WAIT_SNAPSHOTS:
            return None
        candidates = [PREFERRED_SYMBOL] + [
            s for s in market.symbols() if s != PREFERRED_SYMBOL]
        for symbol in candidates:
            signal = self._safe_buy(market, portfolio, symbol)
            if signal is not None:
                self.post_cross_done = True
                logger.info("POST-CROSS BUY %d %s @ %.2f",
                            signal.quantity, signal.symbol, signal.price)
                return signal
        return None                            # no ask anywhere yet — retry

    def momentum_strategy(self, market, portfolio) -> Signal | None:
        """Optional buy-only MSFT 5/20 cross; bearish crosses simply do nothing."""
        if self.momentum_orders >= MAX_MOMENTUM_ORDERS:
            return None
        symbol = PREFERRED_SYMBOL
        prices = market.prices(symbol)
        if len(prices) < 21 or not all(
            isinstance(p, (int, float)) and math.isfinite(p) and p > 0
            for p in prices[-21:]
        ):
            return None
        prev_ma5 = sum(prices[-6:-1]) / 5
        prev_ma20 = sum(prices[-21:-1]) / 20
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20
        # NEVER place a sell, even if the crossover is bearish.
        if not (prev_ma5 <= prev_ma20 and ma5 > ma20):
            return None
        signature = ("buy", tuple(prices[-21:]))
        if signature == self.last_momentum_signal:
            return None
        signal = self._safe_buy(market, portfolio, symbol)
        if signal is None:
            return None
        self.last_momentum_signal = signature
        self.momentum_orders += 1
        logger.info("MOMENTUM BUY %d %s @ %.2f", QUANTITY, symbol, signal.price)
        return signal

    # ── hooks ───────────────────────────────────────────────────────────

    def on_tick(self, market, portfolio) -> Signal | None:
        # Opening auction: send nothing at all. Limit orders are legal in the
        # pre-open, but the graded requirement is orders AFTER the cross and
        # the auction is the one place a marketable buy can misbehave.
        if self.in_preopen:
            n = self._snapshots(market)
            if self.preopen_snapshots is None:
                self.preopen_snapshots = n
            if n - self.preopen_snapshots <= PREOPEN_MAX_SNAPSHOTS:
                return None
            logger.warning("Still pre-open after %d snapshots — assuming the "
                           "cross happened and resuming", PREOPEN_MAX_SNAPSHOTS)
            self.in_preopen = False

        # Fired earnings get priority: do not spend the shock's response
        # window executing the unrelated strategies.
        if self.pending_events:
            signal = self.handle_shock(market, portfolio)
        elif not self.post_cross_done:
            signal = self.post_cross_buy(market, portfolio)
        else:
            signal = self.momentum_strategy(market, portfolio)
        # Final fail-closed guard for any future strategy modifications.
        if signal is not None and signal.side != "buy":
            logger.error("Blocked non-buy signal: %s", signal)
            return None
        return signal

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)

    def on_ipo(self, symbol: str, lo: float, hi: float, shares: int,
               data: dict) -> tuple[int, float] | None:
        """Bid the TOP of the range for a sure allocation; small size."""
        if hi <= 0 or shares <= 0:
            return None
        qty = min(IPO_SHARES, int(shares))
        logger.info("IPO %s: bidding %d @ %.2f (range %.2f–%.2f)",
                    symbol, qty, hi, lo, hi)
        return (qty, hi)

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Track the opening auction; queue fired earnings on EVENT_SYMBOLS."""
        data = data or {}
        logger.info("EVENT %s — %s %s", event, message, data)

        if event == "SESSION_PREOPEN":
            self.in_preopen = True
            self.preopen_snapshots = None
            return
        if event == "AUCTION_RESULT":
            if data.get("phase", "open") == "open":
                self.in_preopen = False
                self.cross_snapshots = None     # restart the post-cross clock
                self.post_cross_done = False
            return

        if event == "CALENDAR_EVENT":
            if data.get("kind") != "earnings":
                return
            symbols = data.get("affected") or [data.get("symbol")]
            event_key = ("CALENDAR_EVENT", data.get("tick"))
        elif event == "SHOCK":
            symbols = data.get("affected") or [data.get("symbol")]
            event_key = ("SHOCK", data.get("shock_id"))
        else:
            return
        if isinstance(symbols, str):
            symbols = [symbols]
        for symbol in symbols:
            if symbol not in EVENT_SYMBOLS:
                continue
            key = (*event_key, symbol)
            if key in self.seen_events:
                continue
            self.seen_events.add(key)
            self.pending_events.append({
                "symbol": symbol, "event": event, "pct": data.get("pct")
            })
            logger.info("SHOCK DETECTED: %s (event=%s pct=%s)",
                        symbol, event, data.get("pct"))

    # ── entry point ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Same as arena.Trader.run(), but on the no-flatten bot below."""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        asyncio.run(_NoFlattenBot(self).run())


class _NoFlattenBot(_AdaptedTraderBot):
    """The SDK bot, minus the close-time flatten.

    On SESSION_CLOSED the engine sends one market order per held symbol. The
    venue is already closed by then, so every one of them is rejected
    (10 × ``SESSION_CLOSED`` per seat in Session 2) — and week 4 caps us at
    10 rejections. Positions persist across sessions anyway, so skipping the
    flatten loses nothing.

    NOTE: this subclasses an underscore-prefixed SDK class. If a template
    update renames ``arena.trader._AdaptedTraderBot`` this import breaks
    loudly at startup — fall back to ``Trader.run()`` and stop the bots by
    hand before the close.
    """

    async def flatten_all_positions(self) -> None:
        logger.info("Session closed — keeping positions (no close-time orders)")


def _setup_logging() -> None:
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    team_id = os.environ.get("TEAM_ID", "trader")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / f"{team_id}.log")],
        force=True,
    )
    logging.getLogger("trader.trader").setLevel(logging.DEBUG)


if __name__ == "__main__":
    _setup_logging()
    MyTrader().run()
