"""Week 4 long-only trader for butter_chicken_pasta.

What the week-4 block grades (Phase 1, Part A):
  1. a bid in the ORCA bookbuild            -> on_ipo()
  2. orders after the opening cross,
     not only during the pre-open           -> in_preopen gate + post-cross buy
  3. at most 10 rejected orders             -> _NoFlattenBot (no close-time
                                               market orders), tiny marketable
                                               buys only, position cap

On top of that it keeps the week-3 behaviour: a 1-share marketable BUY when
a scheduled earnings print fires on EVENT_SYMBOLS, and a buy-only MSFT 5/20
moving-average cross. Shorts are unlocked in week 4 but this bot never
submits a sell: every extra order type is another way to collect a rejection,
and the rejection count is what is graded. Positions carry over between
sessions (the season never resets), so there is nothing to flatten.

Run: TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

NOTE: ``CALENDAR`` announces *future* events. ``CALENDAR_EVENT`` is emitted
when scheduled earnings actually fire; ``SHOCK`` is for injected shocks.
``IPO_OPEN`` starts the bookbuild (t150, after the opening cross).
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
# Internal per-symbol cap, far below the venue's position limit: a runaway
# loop can never build a position worth a POSITION_LIMIT rejection.
MAX_ABS_POSITION = 20
# Week 4 calendar: earnings AMZN @ t350, GOOGL @ t800.
EVENT_SYMBOLS = frozenset(("AMZN", "GOOGL"))
# A fired event we could not act on (no ask, no cash) is dropped after this
# many on_tick calls (~0.5 s each) instead of being retried forever.
MAX_EVENT_WAIT_TICKS = 60
# ORCA bookbuild: 200 shares at the top of the range is ~$5.6k on a $250k
# seat — far below both exchange caps (deal size, and cash at max_price).
IPO_SHARES = 200
# on_tick calls (~0.5 s each) to let the brokers rebuild the book after the
# cross before sending the guaranteed post-cross order. Measured on our own
# tick counter, NOT on len(market.prices()): the SDK's price history is a
# deque capped at 200, so on a long-lived process its length stops growing
# and a snapshot-based wait never completes (live bug, week 4).
POST_CROSS_WAIT_TICKS = 10
# Backstop: the opening auction is 12 ticks (~12 s). If we are somehow still
# flagged "pre-open" this many on_tick calls later, assume AUCTION_RESULT
# was missed.
PREOPEN_MAX_TICKS = 80


class MyTrader(Trader):
    """Week 4: BUY-only; IPO bid, post-cross order, earnings and momentum."""

    def __init__(self) -> None:
        self.pending_events: list[dict] = []
        self.seen_events: set[tuple] = set()
        self.last_momentum_signal: tuple | None = None
        self.momentum_orders: int = 0
        self.tick_count: int = 0
        # Opening-auction state (driven by on_event, read by on_tick).
        self.in_preopen: bool = False
        self.preopen_tick: int | None = None
        self.post_cross_done: bool = False
        self.cross_tick: int | None = None

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _valid_price(price) -> bool:
        """Reject missing, boolean, non-finite or non-positive prices."""
        return (isinstance(price, (int, float)) and not isinstance(price, bool)
                and math.isfinite(price) and price > 0)

    @classmethod
    def _safe_buy(cls, market, portfolio, symbol: str) -> Signal | None:
        """Return a 1-share marketable limit BUY only when it is safe to send.

        Checks: a real ask, the internal position cap, cash with a fee
        buffer, and the SDK's own can_buy. No sell can be constructed here.
        """
        ask = market.best_ask(symbol)
        if not cls._valid_price(ask):
            return None
        if portfolio.positions.get(symbol, 0) + QUANTITY > MAX_ABS_POSITION:
            return None
        if (not isinstance(portfolio.cash, (int, float))
                or not math.isfinite(portfolio.cash)
                or portfolio.cash < ask * QUANTITY * BUY_CASH_BUFFER):
            return None
        if not portfolio.can_buy(symbol, QUANTITY, ask):
            return None
        return Signal(symbol=symbol, side="buy", quantity=QUANTITY, price=ask)

    # ── strategies ──────────────────────────────────────────────────────

    def handle_shock(self, market, portfolio) -> Signal | None:
        """Act on the earliest *fired* event; drop it once it is stale."""
        if not self.pending_events:
            return None
        shock = self.pending_events[0]
        if self.tick_count - shock["created_tick"] > MAX_EVENT_WAIT_TICKS:
            self.pending_events.pop(0)
            logger.info("SHOCK EXPIRED: %s (no fillable ask for %d ticks)",
                        shock["symbol"], MAX_EVENT_WAIT_TICKS)
            return None
        signal = self._safe_buy(market, portfolio, shock["symbol"])
        if signal is None:
            # No ask, capped, or insufficient cash: don't send a doomed order.
            logger.debug("Shock %s pending: no safe buy yet", shock["symbol"])
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
        if self.cross_tick is None:
            self.cross_tick = self.tick_count  # first tick after the cross
            return None
        if self.tick_count - self.cross_tick < POST_CROSS_WAIT_TICKS:
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
        if len(prices) < 21 or not all(self._valid_price(p) for p in prices[-21:]):
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
        self.tick_count += 1

        # Opening auction: send nothing at all. Limit orders are legal in the
        # pre-open, but the graded requirement is orders AFTER the cross and
        # the auction is the one place a marketable buy can misbehave.
        if self.in_preopen:
            if self.preopen_tick is None:
                self.preopen_tick = self.tick_count
            if self.tick_count - self.preopen_tick <= PREOPEN_MAX_TICKS:
                return None
            logger.warning("Still pre-open after %d ticks — assuming the "
                           "cross happened and resuming", PREOPEN_MAX_TICKS)
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
        if signal is not None and (signal.side != "buy" or signal.quantity <= 0
                                   or not self._valid_price(signal.price)):
            logger.error("Blocked unsafe signal: %s", signal)
            return None
        return signal

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)

    def on_ipo(self, symbol: str, lo: float, hi: float, shares: int,
               data: dict) -> tuple[int, float] | None:
        """Bid the TOP of the range for a sure allocation; small size."""
        if (not self._valid_price(lo) or not self._valid_price(hi)
                or hi < lo or shares <= 0):
            logger.warning("IPO %s: invalid terms lo=%s hi=%s shares=%s — passing",
                           symbol, lo, hi, shares)
            return None
        qty = min(IPO_SHARES, int(shares))
        logger.info("IPO %s: bidding %d @ %.2f (range %.2f–%.2f)",
                    symbol, qty, hi, lo, hi)
        return (qty, hi)

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Track the opening auction and the IPO; queue fired earnings."""
        data = data or {}
        logger.info("EVENT %s — %s %s", event, message, data)

        if event == "SESSION_PREOPEN":
            self.in_preopen = True
            self.preopen_tick = None
            return
        if event == "AUCTION_RESULT":
            if data.get("phase", "open") == "open":
                self.in_preopen = False
                self.cross_tick = None          # restart the post-cross clock
                self.post_cross_done = False
            return

        # IPO lifecycle — log loudly so the live session is easy to follow.
        if event == "IPO_SUB_RESULT":
            if data.get("ok"):
                logger.info("IPO SUBSCRIPTION ACCEPTED: %s", data.get("symbol"))
            else:
                logger.warning("IPO SUBSCRIPTION REJECTED: %s", data)
            return
        if event in ("IPO_PRICED", "IPO_LISTED"):
            logger.info("%s: %s", event, data)
            return
        if event in ("TRADING_HALT", "MARKET_HALT"):
            logger.warning("%s: %s", event, data)
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
                "symbol": symbol, "event": event, "pct": data.get("pct"),
                "created_tick": self.tick_count,
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
