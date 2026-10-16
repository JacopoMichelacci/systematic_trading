"""Week 3 long-only trader for butter_chicken_pasta.

Primary objective: react to the real NVDA/TSLA earnings events with a
small, legal BUY within the event window, while never submitting a sell.

Run: TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

NOTE: ``CALENDAR`` announces *future* events. ``CALENDAR_EVENT`` is emitted
when scheduled earnings actually fire; ``SHOCK`` is for injected shocks.
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
EVENT_SYMBOLS = frozenset(("NVDA", "TSLA"))


class MyTrader(Trader):
    """Week 3: BUY-only, with earnings reactions ahead of momentum trading."""

    def __init__(self) -> None:
        self.pending_events: list[dict] = []
        self.seen_events: set[tuple] = set()
        self.last_momentum_signal: tuple | None = None
        self.momentum_orders: int = 0

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
        # Week 3: NEVER place a sell, even if the crossover is bearish.
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

    def on_tick(self, market, portfolio) -> Signal | None:
        # Fired earnings get priority: do not spend the shock's response
        # window executing the unrelated momentum strategy.
        if self.pending_events:
            signal = self.handle_shock(market, portfolio)
        else:
            signal = self.momentum_strategy(market, portfolio)
        # Final fail-closed guard for any future strategy modifications.
        if signal is not None and signal.side != "buy":
            logger.error("Blocked non-buy signal in Week 3: %s", signal)
            return None
        return signal

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Only trade on events that have already fired, not CALENDAR previews."""
        data = data or {}
        logger.info("EVENT %s — %s %s", event, message, data)
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
