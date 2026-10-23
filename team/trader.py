
"""Week 4 trader for butter_chicken_pasta.

Primary objectives:
- Submit a valid ORCA IPO bid during the bookbuild.
- Place at least one order after the opening cross.
- Keep rejected orders to a minimum.
- React to earnings with small directional trades.

Week 4 allows short selling, but the strategy remains conservative.

Run: TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

NOTE: ``CALENDAR`` announces *future* events. ``CALENDAR_EVENT`` is emitted
when scheduled earnings actually fire; ``SHOCK`` is for injected shocks.
``IPO_OPEN`` starts the IPO bookbuild, after the opening cross.
"""

from __future__ import annotations

import logging
import math
import os
import pathlib

from arena import Signal, Trader

logger = logging.getLogger("team.trader")

PREFERRED_SYMBOL = "MSFT"
QUANTITY = 1
MAX_MOMENTUM_ORDERS = 3

# Account for venue taker fees and stale best-ask prices, rather than relying
# on Portfolio.can_buy's smaller 0.1% buffer alone.
BUY_CASH_BUFFER = 1.01

# Week 4 earnings.
EVENT_SYMBOLS = frozenset(("AMZN", "GOOGL"))

# IPO participation: bid at the top of the announced range.
IPO_SYMBOL = "ORCA"
IPO_QUANTITY = 5

# Conservative internal risk limits, well below the venue's 800-share cap.
MAX_ABS_POSITION = 20

# Short-sale restriction can trigger after a 6% decline.
# Leave a buffer rather than shorting aggressively near that threshold.
MAX_SHORT_SHOCK_DROP = 0.05

# Do not keep attempting an old event indefinitely.
MAX_EVENT_WAIT_TICKS = 60


class MyTrader(Trader):
    """Week 4: IPO participation, directional earnings and momentum trading."""

    def __init__(self) -> None:
        self.pending_events: list[dict] = []
        self.seen_events: set[tuple] = set()
        self.last_momentum_signal: tuple | None = None
        self.momentum_orders: int = 0

        # Week 4 additions.
        self.trading_started: bool = False
        self.post_open_order_pending: bool = False
        self.post_open_order_sent: bool = False
        self.ipo_submitted: bool = False
        self.tick_count: int = 0

    @staticmethod
    def _valid_price(price) -> bool:
        """Reject missing, non-finite or non-positive book prices."""
        return (
            isinstance(price, (int, float))
            and not isinstance(price, bool)
            and math.isfinite(price)
            and price > 0
        )

    @staticmethod
    def _safe_buy(market, portfolio, symbol: str) -> Signal | None:
        """Return a small marketable limit BUY only when affordable."""
        ask = market.best_ask(symbol)

        if not MyTrader._valid_price(ask):
            return None

        current_position = portfolio.positions.get(symbol, 0)

        if current_position + QUANTITY > MAX_ABS_POSITION:
            return None

        if (
            not isinstance(portfolio.cash, (int, float))
            or not math.isfinite(portfolio.cash)
            or portfolio.cash < ask * QUANTITY * BUY_CASH_BUFFER
        ):
            return None

        if not portfolio.can_buy(symbol, QUANTITY, ask):
            return None

        return Signal(
            symbol=symbol,
            side="buy",
            quantity=QUANTITY,
            price=ask,
        )

    @staticmethod
    def _safe_sell(
        market, portfolio, symbol: str, allow_short: bool = False
    ) -> Signal | None:
        """Return a small marketable limit SELL.

        When allow_short=True, a sell may open a short position.
        The SDK's Portfolio.can_sell() does not support short selling,
        so short orders use separate position and price checks.
        """
        bid = market.best_bid(symbol)

        if not MyTrader._valid_price(bid):
            return None

        current_position = portfolio.positions.get(symbol, 0)

        if current_position - QUANTITY < -MAX_ABS_POSITION:
            return None

        if not allow_short:
            if not portfolio.can_sell(symbol, QUANTITY):
                return None

        return Signal(
            symbol=symbol,
            side="sell",
            quantity=QUANTITY,
            price=bid,
        )

    def on_ipo(
        self,
        symbol: str,
        lo: float,
        hi: float,
        shares: int,
        data: dict,
    ) -> int | tuple[int, float] | None:
        """Week 4: participate in the ORCA IPO bookbuild.

        Returning (quantity, max_price) lets the SDK send the
        subscription automatically. This is not a regular order.
        """
        if symbol != IPO_SYMBOL:
            return None

        if self.ipo_submitted:
            return None

        if (
            not self._valid_price(lo)
            or not self._valid_price(hi)
            or hi < lo
            or shares <= 0
        ):
            logger.warning(
                "Invalid IPO terms: %s lo=%s hi=%s shares=%s",
                symbol, lo, hi, shares
            )
            return None

        quantity = min(IPO_QUANTITY, shares)

        # Bid at the top of the offer range to prioritize participation.
        self.ipo_submitted = True

        logger.info(
            "IPO BID: %s quantity=%d max_price=%.2f",
            symbol, quantity, hi
        )

        return (quantity, hi)

    def handle_shock(self, market, portfolio) -> Signal | None:
        """Act on fired earnings events with small directional orders."""
        if not self.pending_events:
            return None

        shock = self.pending_events[0]
        symbol = shock["symbol"]
        pct = shock["pct"]

        # Remove expired event opportunities.
        if self.tick_count - shock["created_tick"] > MAX_EVENT_WAIT_TICKS:
            self.pending_events.pop(0)
            logger.info("SHOCK EXPIRED: %s", symbol)
            return None

        if not isinstance(pct, (int, float)) or not math.isfinite(pct):
            self.pending_events.pop(0)
            logger.warning("SHOCK SKIPPED: invalid pct for %s", symbol)
            return None

        if pct > 0:
            # Positive earnings surprise: small long exposure.
            signal = self._safe_buy(market, portfolio, symbol)

        elif pct < 0:
            # Negative earnings surprise: consider a short.
            # Avoid opening new shorts near the short-sale restriction.
            if pct <= -MAX_SHORT_SHOCK_DROP:
                self.pending_events.pop(0)
                logger.info(
                    "SHOCK SKIPPED: %s drop %.2f%% near short restriction",
                    symbol, pct * 100
                )
                return None

            signal = self._safe_sell(
                market, portfolio, symbol, allow_short=True
            )

        else:
            self.pending_events.pop(0)
            return None

        if signal is None:
            # Keep the event temporarily if the book is not ready.
            logger.debug(
                "Shock %s pending: no valid or affordable order",
                symbol
            )
            return None

        self.pending_events.pop(0)

        logger.info(
            "SHOCK RESPONSE: %s %d %s @ %.2f (event=%s, pct=%s)",
            signal.side.upper(),
            signal.quantity,
            signal.symbol,
            signal.price,
            shock["event"],
            pct
        )

        return signal

    def momentum_strategy(self, market, portfolio) -> Signal | None:
        """Optional MSFT 5/20 crossover with limited bearish exposure."""
        if self.momentum_orders >= MAX_MOMENTUM_ORDERS:
            return None

        symbol = PREFERRED_SYMBOL
        prices = market.prices(symbol)

        if len(prices) < 21 or not all(
            isinstance(p, (int, float))
            and math.isfinite(p)
            and p > 0
            for p in prices[-21:]
        ):
            return None

        prev_ma5 = sum(prices[-6:-1]) / 5
        prev_ma20 = sum(prices[-21:-1]) / 20
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20

        # Bullish crossover: buy.
        if prev_ma5 <= prev_ma20 and ma5 > ma20:
            side = "buy"

        # Bearish crossover: reduce a long position if one exists.
        # Do not open additional speculative shorts on MSFT here.
        elif prev_ma5 >= prev_ma20 and ma5 < ma20:
            if portfolio.positions.get(symbol, 0) <= 0:
                return None
            side = "sell"

        else:
            return None

        signature = (side, tuple(prices[-21:]))

        if signature == self.last_momentum_signal:
            return None

        if side == "buy":
            signal = self._safe_buy(market, portfolio, symbol)
        else:
            signal = self._safe_sell(
                market, portfolio, symbol, allow_short=False
            )

        if signal is None:
            return None

        self.last_momentum_signal = signature
        self.momentum_orders += 1

        logger.info(
            "MOMENTUM %s %d %s @ %.2f",
            side.upper(),
            QUANTITY,
            symbol,
            signal.price
        )

        return signal

    def on_tick(self, market, portfolio) -> Signal | None:
        self.tick_count += 1

        # Do not send ordinary orders before continuous trading
        # has been confirmed by the Week 4 IPO_OPEN event.
        if not self.trading_started:
            return None

        # Week 4 scoring: ensure an order is attempted after the opening cross.
        # The ORCA bookbuild opens at t150, after continuous trading begins.
        if self.post_open_order_pending:
            signal = self._safe_buy(
                market, portfolio, PREFERRED_SYMBOL
            )

            if signal is not None:
                self.post_open_order_pending = False
                self.post_open_order_sent = True

                logger.info(
                    "POST-OPEN BUY %d %s @ %.2f",
                    signal.quantity,
                    signal.symbol,
                    signal.price
                )

                return signal

        # Fired earnings get priority over unrelated momentum trading.
        if self.pending_events:
            signal = self.handle_shock(market, portfolio)
        else:
            signal = self.momentum_strategy(market, portfolio)

        # Final safety guard.
        if signal is not None:
            if signal.side not in ("buy", "sell"):
                logger.error("Blocked invalid side: %s", signal)
                return None

            if signal.quantity <= 0:
                logger.error("Blocked invalid quantity: %s", signal)
                return None

            if not self._valid_price(signal.price):
                logger.error("Blocked invalid price: %s", signal)
                return None

        return signal

    def on_fill(
        self,
        side: str,
        symbol: str,
        quantity: int,
        price: float,
    ) -> None:
        logger.info(
            "FILL %s %d %s @ %.2f",
            side.upper(), quantity, symbol, price
        )

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Trade on fired events, not CALENDAR previews."""
        data = data or {}

        logger.info("EVENT %s — %s %s", event, message, data)

        # Week 4: IPO bookbuild begins after the opening cross.
        if event == "IPO_OPEN":
            if data.get("symbol") == IPO_SYMBOL:
                self.trading_started = True

                if not self.post_open_order_sent:
                    self.post_open_order_pending = True

                logger.info(
                    "IPO_OPEN: continuous trading confirmed; "
                    "post-open order enabled"
                )
            return

        if event == "IPO_SUB_RESULT":
            if data.get("ok"):
                logger.info("IPO SUBSCRIPTION ACCEPTED: %s", data)
            else:
                logger.warning("IPO SUBSCRIPTION RESULT: %s", data)
            return

        if event == "IPO_PRICED":
            logger.info("IPO PRICED: %s", data)
            return

        if event == "IPO_LISTED":
            logger.info("IPO LISTED: %s", data)
            return

        if event == "TRADING_HALT":
            logger.warning("TRADING HALT: %s", data)
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
                "symbol": symbol,
                "event": event,
                "pct": data.get("pct"),
                "created_tick": self.tick_count,
            })

            logger.info(
                "SHOCK DETECTED: %s (event=%s pct=%s)",
                symbol, event, data.get("pct")
            )


def _setup_logging() -> None:
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)

    team_id = os.environ.get("TEAM_ID", "trader")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / f"{team_id}.log")
        ],
        force=True,
    )

    logging.getLogger("trader.trader").setLevel(logging.DEBUG)


if __name__ == "__main__":
    _setup_logging()
    MyTrader().run()
