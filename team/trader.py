"""
butter_chicken_pasta — trader bot (Level 1).

Level 1 goal: handshake, place ONE small, legal buy order that the exchange
acknowledges and fills, then sit out for the rest of the session.

The order is a marketable limit: a buy priced at the current best ask, so it
fills against resting liquidity instead of waiting in the book. If no fill
arrives within RETRY_AFTER_TICKS ticks, the bot tries again, at most
MAX_ATTEMPTS times — the cap bounds the worst case at MAX_ATTEMPTS × QUANTITY
shares even if every resting order eventually fills.

Run one seat:
    TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

Logs go to the terminal and to logs/<TEAM_ID>.log (gitignored).
"""

from __future__ import annotations

import logging
import os
import pathlib
import math

from arena import Signal, Trader

logger = logging.getLogger("team.trader")

# ── Level 1 tunables ─────────────────────────────────────────────────────────
PREFERRED_SYMBOL = "MSFT"   # not AAPL: Session 2 has an earnings shock on AAPL
QUANTITY = 1                # shares per order — deliberately tiny
MAX_ATTEMPTS = 3            # orders sent before giving up for the session
RETRY_AFTER_TICKS = 30      # ticks to wait for a fill before retrying


class MyTrader(Trader):
    """Level 1 trader: one small marketable buy, then sit out."""

    def __init__(self) -> None:
        self.attempts: int = 0
        self.filled: bool = False
        self.ticks_since_order: int = 0

        # W3: pending earnings shock
        self.pending_events = []
        self.seen_events = set()
        self.last_momentum_signal = None
        self.momentum_orders = 0

    def momentum_strategy(self, market, portfolio) -> Signal | None:
        if self.momentum_orders >= 3:
            return None
        symbol = "MSFT"
        prices = market.prices(symbol)

        if len(prices) < 21:
            return None

        prev_ma5 = sum(prices[-6:-1]) / 5
        prev_ma20 = sum(prices[-21:-1]) / 20

        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20

        side = None

        if prev_ma5 <= prev_ma20 and ma5 > ma20:
            side = "buy"

        elif prev_ma5 >= prev_ma20 and ma5 < ma20:
            side = "sell"

        if side is None:
            return None

        # Avoid repeating the same crossover on unchanged data.
        signature = (side, tuple(prices[-21:]))

        if signature == self.last_momentum_signal:
            return None

        if side == "buy":
            price = market.best_ask(symbol)

            if price is None or price <= 0:
                return None

            if not portfolio.can_buy(symbol, 1, price):
                return None

        else:
            price = market.best_bid(symbol)

            if price is None or price <= 0:
                return None

            if not portfolio.can_sell(symbol, 1):
                return None

        self.last_momentum_signal = signature

        logger.info(
            "MOMENTUM %s 1 %s @ %.2f",
            side.upper(), symbol, price
        )
        self.momentum_orders += 1
        return Signal(
            symbol=symbol,
            side=side,
            quantity=1,
            price=price
        )


    def handle_shock(self, market, portfolio) -> Signal | None:
        """React to the earliest pending NVDA/TSLA shock."""

        if not self.pending_events:
            return None

        shock = self.pending_events[0]
        symbol = shock["symbol"]
        pct = shock["pct"]

        # Use direction only when the event provides a valid pct.
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            if not math.isfinite(pct):
                pct = None
        else:
            pct = None

        # Positive earnings: prefer buying.
        # Negative earnings: prefer selling existing holdings.
        # Unknown direction: make a small, legal fallback trade.
        if pct is not None and pct < 0:
            preferred_sides = ("sell", "buy")
        else:
            preferred_sides = ("buy", "sell")

        for side in preferred_sides:
            if side == "buy":
                price = market.best_ask(symbol)

                if price is None or not math.isfinite(price) or price <= 0:
                    continue

                if not portfolio.can_buy(symbol, 1, price):
                    continue

            else:
                price = market.best_bid(symbol)

                if price is None or not math.isfinite(price) or price <= 0:
                    continue

                if not portfolio.can_sell(symbol, 1):
                    continue

            self.pending_events.pop(0)

            logger.info(
                "SHOCK RESPONSE: %s 1 %s @ %.2f (pct=%s)",
                side.upper(), symbol, price, pct
            )

            return Signal(
                symbol=symbol,
                side=side,
                quantity=1,
                price=price
            )

        logger.warning(
            "No legal shock trade available for %s; waiting",
            symbol
        )
        return None


    def on_tick(self, market, portfolio) -> Signal | None:
        """Send one size-limited buy at the best ask; afterwards return None.

        Args:
            market:    live MarketData — symbols(), best_ask(s), mid_price(s).
            portfolio: live Portfolio — cash, positions, can_buy(s, qty, px).

        Returns:
            A buy Signal on the tick we decide to trade, otherwise None.
        """
        if self.pending_events:
            return self.handle_shock(market, portfolio)

        return self.momentum_strategy(market, portfolio)
    
        
    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Log our own executions; the first fill completes Level 1."""
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)
        if side == "buy":
            self.filled = True

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Log market news (shocks, calendar, fee schedule, ...)."""
        logger.info("EVENT %s — %s %s", event, message, data or "")

        # w3
        if event == "CALENDAR_EVENT":
            if data.get("kind") != "earnings":
                return
            symbols = [data.get("symbol")]

        elif event == "SHOCK":
            symbols = data.get("affected", []) or []

            if isinstance(symbols, str):
                symbols = [symbols]
            if not symbols and data.get("symbol"):
                symbols = [data["symbol"]]

        else:
            return

        for symbol in symbols:
            if symbol not in ("NVDA", "TSLA"):
                continue

            event_id = (
                event,
                symbol,
                data.get("tick") if event == "CALENDAR_EVENT"
                else data.get("shock_id")
            )

            if event_id in self.seen_events:
                continue

            self.seen_events.add(event_id)
            self.pending_events.append({
                "symbol": symbol,
                "event": event,
                "pct": data.get("pct"),
                "data": data,
            })

            logger.info(
                "SHOCK DETECTED: %s, pct=%s",
                symbol,
                data.get("pct")
            )

    @staticmethod
    def _pick_symbol(market) -> str | None:
        """PREFERRED_SYMBOL if it has an ask, else any symbol that does."""
        candidates = [PREFERRED_SYMBOL] + [
            s for s in market.symbols() if s != PREFERRED_SYMBOL]
        for symbol in candidates:
            ask = market.best_ask(symbol)
            if ask is not None and ask > 0:
                return symbol
        return None


def _setup_logging() -> None:
    """Log to the terminal and to logs/<TEAM_ID>.log.
    """
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    team_id = os.environ.get("TEAM_ID", "trader")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(log_dir / f"{team_id}.log")],
        force=True, # replaces the handler the engine's imports have already put on the root logger
    )
    logging.getLogger("trader.trader").setLevel(logging.DEBUG)


if __name__ == "__main__":
    _setup_logging()
    MyTrader().run()
