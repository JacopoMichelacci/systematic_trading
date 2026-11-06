"""Risk-first, long-only trader for butter_chicken_pasta (weeks 5–6).

What the live block grades, and where it is handled:

  no forced liquidation        long-only, cash-funded 1-share buys under a
                               per-session notional budget; never short,
                               never margin, never futures  -> _check_buy()
  position <= scenario limit   hard caps in code, at SEAT and at TEAM scope
                               (the venue enforces the limit per team), fed by
                               the scenario's position_limit    -> _caps()
  survive a halt, then resume  no orders to a halted symbol; a buy in that
                               symbol as soon as TRADING_RESUMED arrives
                               (or the halt times out)  -> halted / _resume_step()
  a bid in the IPO bookbuild   on_ipo(): top of the range, small size
  connected >= 80%             nothing here may raise into the SDK's listen
                               loop (that drops the socket); run the seats
                               under `python -m team.run_block`

So that a halt always lands on "a symbol the bot trades", the bot buys one
share of every equity shortly after the opening cross (the seed basket) and
then keeps a slow heartbeat of 1-share buys going for the whole block.

Run one seat:   TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader
Run them all:   python -m team.run_block
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import pathlib
import time

# `arena` must be imported first: it puts engine/ on sys.path, which is where
# the `trader` package below lives.
from arena import Signal, Trader  # isort: skip
from arena.trader import _AdaptedTraderBot  # isort: skip
import trader.config as _engine_config  # isort: skip
from trader.trader import Portfolio  # isort: skip

from team.config import BROKER_IDS, TRADER_IDS

logger = logging.getLogger("team.trader")

# ── universe ────────────────────────────────────────────────────────────
# The ten cash equities, and nothing else: no index future (margin, daily
# settlement), no freshly listed IPO names (thin books).
TRADABLE = ("MSFT", "INTC", "AAPL", "NVDA", "TSLA",
            "AMZN", "GOOGL", "META", "NFLX", "AMD")
PREFERRED_SYMBOL = "MSFT"
QUANTITY = 1
TEAM_BOT_IDS = frozenset(BROKER_IDS) | frozenset(TRADER_IDS)

# ── hard risk limits ────────────────────────────────────────────────────
# Used until the scenario tells us its position limit (WEEK_CHANGED, or the
# SESSION_OPEN we get when joining mid-session). Deliberately low.
DEFAULT_POSITION_LIMIT = 400
# One seat never holds more than min(SEAT_HARD_CAP, limit // 5) of a symbol,
# and the whole team (4 bots) never more than limit // 2: the venue checks
# the limit at TEAM scope, so we stay at half of it.
SEAT_HARD_CAP = 100
# Per process and per session: shares added per symbol, orders, and dollars.
SESSION_SHARES_PER_SYMBOL = 10
MAX_ORDERS_PER_SESSION = 80
MAX_SESSION_NOTIONAL = 25_000.0     # ~10% of a $250k seat
# Cash needed on top of the order's cost (venue fees, a stale ask).
BUY_CASH_BUFFER = 1.01
# The venue rejects limit prices more than 10% from its mark (PRICE_BAND).
# Never lift an ask further than this above the book mid.
MAX_ASK_OVER_MID = 1.05

# ── IPO ─────────────────────────────────────────────────────────────────
IPO_SHARES = 100                    # per seat; three seats bid
IPO_MAX_NOTIONAL = 10_000.0

# ── timing, in on_tick calls (~0.5 s each) ──────────────────────────────
# Counted on our own tick counter, never on len(market.prices()): that
# history is a deque capped at 200 and stops growing (live bug, week 4).
CONNECT_SETTLE_TICKS = 6            # let the books and leaderboard arrive
POST_CROSS_WAIT_TICKS = 10          # let the brokers rebuild after the cross
PREOPEN_MAX_TICKS = 240             # backstop if AUCTION_RESULT is missed
SEED_SPACING_TICKS = 4              # one seed order every ~2 s
SEED_MAX_TICKS = 1200               # give up on unbuyable seeds after 10 min
HEARTBEAT_TICKS = 240               # an order at least every ~2 min
RESUME_SETTLE_TICKS = 6             # after a resume, let quotes come back
RESUME_FALLBACK_TICKS = 60          # then trade another symbol instead
MAX_EVENT_WAIT_TICKS = 600          # drop an unfillable earnings event
MAX_MOMENTUM_ORDERS = 3

# ── halts, in seconds ───────────────────────────────────────────────────
DEFAULT_HALT_SEC = 300.0            # the venue's per-symbol halt
HALT_GRACE_SEC = 20.0               # only if TRADING_RESUMED never arrives


def _valid_price(price) -> bool:
    """Reject missing, boolean, non-finite or non-positive prices."""
    return (isinstance(price, (int, float)) and not isinstance(price, bool)
            and math.isfinite(price) and price > 0)


class MyTrader(Trader):
    """BUY-only, 1 share at a time, every order through _check_buy()."""

    def __init__(self) -> None:
        self.team_id: str = os.environ.get("TEAM_ID", "")
        self.tick_count: int = 0
        self._now = time.monotonic          # injectable clock (tests)

        # Scenario (from WEEK_CHANGED / the mid-session SESSION_OPEN).
        self.position_limit: int | None = None

        # Connection / session phase.
        self.connect_tick: int = 0
        self.active_tick: int | None = None
        self.in_preopen: bool = False
        self.preopen_tick: int | None = None
        self.in_preclose: bool = False
        self.liquidated: bool = False
        self.risk_off: bool = False

        # Halts: symbol -> local time after which we stop believing it.
        self.halted: dict[str, float] = {}
        self.market_halt_until: float | None = None

        self._reset_session()

    def _reset_session(self) -> None:
        """Everything that belongs to one trading session."""
        self.bought: dict[str, int] = {}
        self.orders_sent: int = 0
        self.spent: float = 0.0
        self.last_order_tick: int = self.tick_count
        self.rotation: int = 0
        self.seed_queue: list[str] = list(TRADABLE)
        self.seed_start_tick: int | None = None
        self.last_seed_tick: int = -10**9
        self.resume_queue: list[dict] = []
        self.pending_events: list[dict] = []
        self.seen_events: set[tuple] = set()
        self.last_momentum_signal: tuple | None = None
        self.momentum_orders: int = 0

    # ── the one gate every order goes through ───────────────────────────

    def _caps(self) -> tuple[int, int]:
        """(seat cap, team cap) in shares per symbol."""
        limit = self.position_limit or DEFAULT_POSITION_LIMIT
        return min(SEAT_HARD_CAP, limit // 5), limit // 2

    def _check_buy(self, market, portfolio, symbol: str
                   ) -> tuple[Signal | None, str]:
        """A 1-share marketable limit BUY, or None and the reason why not."""
        if symbol not in TRADABLE:
            return None, "not in the tradable universe"
        if symbol in self.halted:
            return None, "halted"

        ask = market.best_ask(symbol)
        if not _valid_price(ask):
            return None, "no ask in the book"
        mid = market.mid_price(symbol)
        if _valid_price(mid) and ask > mid * MAX_ASK_OVER_MID:
            return None, "ask too far above the mid"

        positions = portfolio.positions
        own = int(positions.get(symbol, 0))
        seat_cap, team_cap = self._caps()
        if own + QUANTITY > seat_cap:
            return None, f"seat cap ({own} held, cap {seat_cap})"
        teammates = getattr(portfolio, "teammates", None)
        if isinstance(teammates, dict):
            team = own + int(teammates.get(symbol, 0))
        else:                       # unknown: assume every seat holds as much
            team = own * max(1, len(TEAM_BOT_IDS))
        if team + QUANTITY > team_cap:
            return None, f"team cap ({team} held, cap {team_cap})"

        if self.bought.get(symbol, 0) + QUANTITY > SESSION_SHARES_PER_SYMBOL:
            return None, "session share budget for this symbol"
        if self.orders_sent >= MAX_ORDERS_PER_SESSION:
            return None, "session order budget"
        cost = ask * QUANTITY
        if self.spent + cost > MAX_SESSION_NOTIONAL:
            return None, "session notional budget"

        cash = portfolio.cash
        if (not isinstance(cash, (int, float)) or not math.isfinite(cash)
                or cash < cost * BUY_CASH_BUFFER):
            return None, "not enough cash"
        if not portfolio.can_buy(symbol, QUANTITY, ask):
            return None, "not enough cash (SDK check)"
        return Signal(symbol=symbol, side="buy", quantity=QUANTITY,
                      price=ask), "ok"

    def _any_buy(self, market, portfolio) -> Signal | None:
        """The next buyable symbol in rotation, if there is one."""
        n = len(TRADABLE)
        for i in range(n):
            symbol = TRADABLE[(self.rotation + i) % n]
            signal, _ = self._check_buy(market, portfolio, symbol)
            if signal is not None:
                self.rotation = (self.rotation + i + 1) % n
                return signal
        return None

    def _record(self, signal: Signal) -> None:
        """Charge an order we are about to send against the budgets."""
        self.bought[signal.symbol] = (
            self.bought.get(signal.symbol, 0) + signal.quantity)
        self.orders_sent += 1
        self.spent += signal.price * signal.quantity
        self.last_order_tick = self.tick_count

    # ── halts ───────────────────────────────────────────────────────────

    def _queue_resume(self, symbol: str, why: str) -> None:
        if symbol in TRADABLE:
            self.resume_queue = [r for r in self.resume_queue
                                 if r["symbol"] != symbol]
            self.resume_queue.append({"symbol": symbol,
                                      "tick": self.tick_count})
        else:                       # not ours to trade: resume elsewhere
            self.last_order_tick = -10**9
        logger.info("RESUMED: %s (%s) — activity resumes", symbol, why)

    def _expire_halts(self) -> None:
        """Fallback for a TRADING_RESUMED we never received."""
        now = self._now()
        for symbol, until in list(self.halted.items()):
            if now >= until:
                del self.halted[symbol]
                self._queue_resume(symbol, "halt timed out locally")
        if self.market_halt_until is not None and now >= self.market_halt_until:
            self.market_halt_until = None
            self.last_order_tick = -10**9
            logger.info("RESUMED: market-wide halt timed out locally")

    # ── strategies, highest priority first ──────────────────────────────

    def _resume_step(self, market, portfolio) -> Signal | None:
        """Trade a symbol again as soon as its halt is lifted."""
        while self.resume_queue:
            item = self.resume_queue[0]
            symbol = item["symbol"]
            age = self.tick_count - item["tick"]
            if symbol in self.halted:               # halted again
                self.resume_queue.pop(0)
                continue
            if age < RESUME_SETTLE_TICKS:
                return None
            signal, why = self._check_buy(market, portfolio, symbol)
            if signal is not None:
                self.resume_queue.pop(0)
                logger.info("RESUME BUY %d %s @ %.2f", signal.quantity,
                            symbol, signal.price)
                return signal
            if age < RESUME_FALLBACK_TICKS:
                return None
            self.resume_queue.pop(0)
            logger.info("Resume: %s not buyable (%s) — trading another "
                        "symbol instead", symbol, why)
            signal = self._any_buy(market, portfolio)
            if signal is not None:
                logger.info("RESUME BUY (fallback) %d %s @ %.2f",
                            signal.quantity, signal.symbol, signal.price)
                return signal
            self.last_order_tick = -10**9           # heartbeat asap
        return None

    def _earnings_step(self, market, portfolio) -> Signal | None:
        """Buy one share on a positive earnings print; skip negative ones."""
        for event in list(self.pending_events):
            symbol, pct = event["symbol"], event["pct"]
            if self.tick_count - event["tick"] > MAX_EVENT_WAIT_TICKS:
                self.pending_events.remove(event)
                logger.info("EVENT EXPIRED: %s (last blocker: %s)",
                            symbol, event.get("why", "n/a"))
                continue
            if (not isinstance(pct, (int, float)) or isinstance(pct, bool)
                    or not math.isfinite(pct) or pct <= 0):
                self.pending_events.remove(event)
                logger.info("EVENT SKIPPED: %s pct=%s — long-only, no buy "
                            "into a negative or unknown print", symbol, pct)
                continue
            signal, why = self._check_buy(market, portfolio, symbol)
            if signal is not None:
                self.pending_events.remove(event)
                logger.info("EVENT BUY %d %s @ %.2f (pct=%+.3f)",
                            signal.quantity, symbol, signal.price, pct)
                return signal
            event["why"] = why
        return None

    def _seed_step(self, market, portfolio) -> Signal | None:
        """One share of every equity after the cross, a few seconds apart."""
        if not self.seed_queue:
            return None
        if self.seed_start_tick is None:
            self.seed_start_tick = self.tick_count
        if self.tick_count - self.seed_start_tick > SEED_MAX_TICKS:
            logger.info("Seed basket: giving up on %s", self.seed_queue)
            self.seed_queue.clear()
            return None
        if self.tick_count - self.last_seed_tick < SEED_SPACING_TICKS:
            return None
        for symbol in list(self.seed_queue):
            signal, _ = self._check_buy(market, portfolio, symbol)
            if signal is not None:
                self.seed_queue.remove(symbol)
                self.last_seed_tick = self.tick_count
                logger.info("SEED BUY %d %s @ %.2f (%d left)",
                            signal.quantity, symbol, signal.price,
                            len(self.seed_queue))
                return signal
        return None

    def _momentum_step(self, market, portfolio) -> Signal | None:
        """Buy-only MSFT 5/20 cross; a bearish cross does nothing."""
        if self.momentum_orders >= MAX_MOMENTUM_ORDERS:
            return None
        symbol = PREFERRED_SYMBOL
        prices = market.prices(symbol)
        if len(prices) < 21 or not all(_valid_price(p) for p in prices[-21:]):
            return None
        prev_ma5 = sum(prices[-6:-1]) / 5
        prev_ma20 = sum(prices[-21:-1]) / 20
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20
        if not (prev_ma5 <= prev_ma20 and ma5 > ma20):
            return None
        signature = ("buy", tuple(prices[-21:]))
        if signature == self.last_momentum_signal:
            return None
        signal, _ = self._check_buy(market, portfolio, symbol)
        if signal is None:
            return None
        self.last_momentum_signal = signature
        self.momentum_orders += 1
        logger.info("MOMENTUM BUY %d %s @ %.2f", QUANTITY, symbol, signal.price)
        return signal

    def _heartbeat_step(self, market, portfolio) -> Signal | None:
        """Keep trading through the block: an order every couple of minutes."""
        if self.tick_count - self.last_order_tick < HEARTBEAT_TICKS:
            return None
        signal = self._any_buy(market, portfolio)
        if signal is not None:
            logger.info("HEARTBEAT BUY %d %s @ %.2f", signal.quantity,
                        signal.symbol, signal.price)
        return signal

    # ── hooks ───────────────────────────────────────────────────────────

    def _decide(self, market, portfolio) -> Signal | None:
        self._expire_halts()

        # Hard stops.
        if self.liquidated or self.risk_off:
            return None
        if not getattr(portfolio, "synced", True):
            return None             # positions unknown: the caps mean nothing
        if self.in_preopen:
            if self.preopen_tick is None:
                self.preopen_tick = self.tick_count
            if self.tick_count - self.preopen_tick <= PREOPEN_MAX_TICKS:
                return None
            logger.warning("Still pre-open after %d ticks — assuming the "
                           "cross happened and resuming", PREOPEN_MAX_TICKS)
            self.in_preopen = False
        if self.in_preclose:
            return None             # closing auction: send nothing
        if self.market_halt_until is not None:
            return None
        if self.tick_count - self.connect_tick < CONNECT_SETTLE_TICKS:
            return None
        if self.active_tick is None:
            self.active_tick = self.tick_count
        if self.tick_count - self.active_tick < POST_CROSS_WAIT_TICKS:
            return None

        for step in (self._resume_step, self._earnings_step, self._seed_step,
                     self._momentum_step, self._heartbeat_step):
            signal = step(market, portfolio)
            if signal is not None:
                return signal
        return None

    def on_tick(self, market, portfolio) -> Signal | None:
        self.tick_count += 1
        try:
            signal = self._decide(market, portfolio)
        except Exception:
            logger.exception("on_tick failed — sitting out this tick")
            return None
        if signal is None:
            return None
        # Fail closed, whatever a future strategy change returns.
        if (signal.side != "buy" or signal.symbol not in TRADABLE
                or signal.quantity != QUANTITY
                or not _valid_price(signal.price)):
            logger.error("Blocked unsafe signal: %s", signal)
            return None
        self._record(signal)
        return signal

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)

    def on_ipo(self, symbol: str, lo: float, hi: float, shares: int,
               data: dict) -> tuple[int, float] | None:
        """Bid the TOP of the range (the sure allocation), in small size."""
        try:
            if (not _valid_price(lo) or not _valid_price(hi) or hi < lo
                    or not isinstance(shares, int) or shares <= 0):
                logger.warning("IPO %s: invalid terms lo=%s hi=%s shares=%s "
                               "— passing", symbol, lo, hi, shares)
                return None
            limit = self.position_limit or DEFAULT_POSITION_LIMIT
            qty = min(IPO_SHARES, shares, limit // 10,
                      int(IPO_MAX_NOTIONAL // hi))
            if qty < 1:
                logger.warning("IPO %s: no affordable size at %.2f — passing",
                               symbol, hi)
                return None
            logger.info("IPO %s: bidding %d @ %.2f (range %.2f–%.2f)",
                        symbol, qty, hi, lo, hi)
            return (qty, hi)
        except Exception:
            logger.exception("on_ipo failed — passing on the deal")
            return None

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Never raises: an exception here would drop the connection."""
        try:
            data = data if isinstance(data, dict) else {}
            logger.info("EVENT %s — %s %s", event, message, data)
            self._handle_event(event, data)
        except Exception:
            logger.exception("on_event(%s) failed — ignored", event)

    def _handle_event(self, event: str, data: dict) -> None:
        now = self._now()

        if event == "WEEK_CHANGED":
            self.on_scenario(data)

        # ── session phase ──
        elif event == "SESSION_PREOPEN":
            self._reset_session()                   # a new session starts
            self.halted.clear()
            self.market_halt_until = None
            self.in_preclose = False
            self.in_preopen = True
            self.preopen_tick = None
            self.active_tick = None
        elif event == "AUCTION_RESULT":
            if data.get("phase", "open") == "open":
                self.in_preopen = False
                self.active_tick = None             # restart the post-cross wait
        elif event == "SESSION_PRECLOSE":
            self.in_preclose = True
            logger.info("Closing auction — no more orders this session")

        # ── halts ──
        elif event == "TRADING_HALT":
            symbol = data.get("symbol")
            if isinstance(symbol, str):
                duration = data.get("duration")
                if not _valid_price(duration):
                    duration = DEFAULT_HALT_SEC
                self.halted[symbol] = now + float(duration) + HALT_GRACE_SEC
                self.resume_queue = [r for r in self.resume_queue
                                     if r["symbol"] != symbol]
                logger.warning("HALT: %s for %.0fs — no orders in it",
                               symbol, duration)
        elif event == "TRADING_RESUMED":
            symbol = data.get("symbol")
            if isinstance(symbol, str):
                self.halted.pop(symbol, None)
                self._queue_resume(symbol, "TRADING_RESUMED")
        elif event == "MARKET_HALT":
            duration = data.get("duration")
            if not _valid_price(duration):
                duration = DEFAULT_HALT_SEC * 3
            self.market_halt_until = now + float(duration) + HALT_GRACE_SEC
            logger.warning("MARKET HALT for %.0fs — no orders at all", duration)
        elif event == "MARKET_RESUMED":
            self.market_halt_until = None
            symbols = data.get("symbols")
            for symbol in symbols if isinstance(symbols, list) else []:
                if isinstance(symbol, str):
                    self.halted.pop(symbol, None)
                    self._queue_resume(symbol, "MARKET_RESUMED")
            self.last_order_tick = -10**9           # trade again right away
            logger.info("RESUMED: market-wide — activity resumes")

        # ── risk ──
        elif event == "LIQUIDATION":
            if data.get("team_id") == self.team_id and self.team_id:
                self.liquidated = True
                logger.error("WE WERE LIQUIDATED — no further orders")
        elif event == "MARGIN_CALL":
            if data.get("team_id", self.team_id) == self.team_id:
                self.risk_off = True
                logger.error("MARGIN CALL — no further buys this session")

        # ── IPO lifecycle (the bid itself is on_ipo) ──
        elif event == "IPO_SUB_RESULT":
            if data.get("ok"):
                logger.info("IPO SUBSCRIPTION ACCEPTED: %s", data.get("symbol"))
            else:
                logger.warning("IPO SUBSCRIPTION REJECTED: %s", data)

        # ── fired earnings and injected shocks ──
        elif event in ("CALENDAR_EVENT", "SHOCK"):
            if event == "CALENDAR_EVENT":
                if data.get("kind") != "earnings":
                    return
                key_id = data.get("tick")
            else:
                key_id = data.get("shock_id")
            symbols = data.get("affected") or [data.get("symbol")]
            if isinstance(symbols, str):
                symbols = [symbols]
            for symbol in symbols:
                if symbol not in TRADABLE:
                    continue
                key = (event, key_id, symbol)
                if key in self.seen_events:
                    continue
                self.seen_events.add(key)
                self.pending_events.append({
                    "symbol": symbol, "event": event,
                    "pct": data.get("pct"), "tick": self.tick_count})
                logger.info("EVENT QUEUED: %s (%s pct=%s)", symbol, event,
                            data.get("pct"))

    # ── called by _SeatBot (things the SDK does not pass to on_event) ───

    def on_scenario(self, scenario: dict) -> None:
        """Pick up the scenario's position limit."""
        limit = (scenario or {}).get("position_limit")
        if (isinstance(limit, int) and not isinstance(limit, bool)
                and limit > 0 and limit != self.position_limit):
            self.position_limit = limit
            seat_cap, team_cap = self._caps()
            logger.info("POSITION LIMIT %d (week %s) -> our caps: %d per "
                        "seat, %d per team", limit,
                        (scenario or {}).get("week"), seat_cap, team_cap)

    def on_connect(self) -> None:
        """A (re)connection: wait for fresh books before trading again."""
        self.connect_tick = self.tick_count
        self.active_tick = None
        self.in_preopen = False     # the venue re-sends SESSION_PREOPEN

    def on_session_closed(self) -> None:
        logger.info("Session closed — %d orders sent, $%.0f spent, keeping "
                    "positions (no close-time orders)",
                    self.orders_sent, self.spent)

    def on_liquidated(self) -> None:
        if not self.liquidated:
            self.liquidated = True
            logger.error("Leaderboard shows us LIQUIDATED — no further orders")

    # ── entry point ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Same as arena.Trader.run(), but on _SeatBot below."""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        asyncio.run(_SeatBot(self).run())


class _SeatPortfolio(Portfolio):
    """The SDK portfolio, plus: do we actually know our positions yet?

    The venue sends no PortfolioUpdate at connect, so a fresh process starts
    with an empty book and would believe it holds nothing. `synced` stays
    False until a PortfolioUpdate or our own leaderboard row has arrived;
    `teammates` holds the other team seats' positions (leaderboard), because
    the venue enforces the position limit across the whole team.
    """

    def __init__(self) -> None:
        super().__init__()
        self.synced: bool = False
        self.teammates: dict[str, int] | None = None

    def apply_server_update(self, update) -> None:
        super().apply_server_update(update)
        self.synced = True


class _SeatBot(_AdaptedTraderBot):
    """The SDK bot with three changes, all in this file.

    1. No close-time flatten. On SESSION_CLOSED the engine sends one market
       order per held symbol to a venue that is already closed, and every
       one is rejected. Positions carry over between sessions anyway.
    2. The portfolio is synced from the leaderboard (see _SeatPortfolio).
    3. The Trader is told about (re)connects and the scenario.

    NOTE: this subclasses an underscore-prefixed SDK class. If a template
    update renames ``arena.trader._AdaptedTraderBot`` the import fails
    loudly at startup.
    """

    def __init__(self, owner: Trader) -> None:
        super().__init__(owner)
        self.portfolio = _SeatPortfolio()

    async def connect(self) -> None:
        await super().connect()
        self.portfolio.synced = False       # anything may have changed
        self._owner.on_connect()

    async def flatten_all_positions(self) -> None:
        return None

    def _on_leaderboard(self, msg) -> None:
        super()._on_leaderboard(msg)
        try:
            self._sync_from_leaderboard(msg)
        except Exception:
            logger.exception("leaderboard sync failed — ignored")

    def _sync_from_leaderboard(self, msg) -> None:
        me = _engine_config.TEAM_ID
        mine = None
        teammates: dict[str, int] = {}
        for row in list(msg.traders) + list(msg.brokers):
            bot_id = row.get("team_id")
            if bot_id == me:
                mine = row
            elif bot_id in TEAM_BOT_IDS:
                for symbol, qty in (row.get("positions") or {}).items():
                    teammates[symbol] = teammates.get(symbol, 0) + int(qty)
        self.portfolio.teammates = teammates
        if mine is None:
            return
        if mine.get("liquidated"):
            self._owner.on_liquidated()
        if not self.portfolio.synced:
            self.portfolio.positions = {
                s: int(q) for s, q in (mine.get("positions") or {}).items()}
            self.portfolio.cash = float(mine.get("cash", self.portfolio.cash))
            self.portfolio.synced = True
            logger.info("Portfolio synced from the leaderboard: cash=%.0f, "
                        "%d symbols held", self.portfolio.cash,
                        len(self.portfolio.positions))

    async def _on_session_event(self, msg) -> None:
        try:
            if msg.event == "SESSION_OPEN":
                self._owner.on_scenario((msg.data or {}).get("scenario") or {})
            elif msg.event == "SESSION_CLOSED":
                self._owner.on_session_closed()
        except Exception:
            logger.exception("session hook failed — ignored")
        await super()._on_session_event(msg)


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
    try:
        MyTrader().run()
    except KeyboardInterrupt:
        logger.info("Stopped by Ctrl-C")
