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

    def on_tick(self, market, portfolio) -> Signal | None:
        """Send one size-limited buy at the best ask; afterwards return None.

        Args:
            market:    live MarketData — symbols(), best_ask(s), mid_price(s).
            portfolio: live Portfolio — cash, positions, can_buy(s, qty, px).

        Returns:
            A buy Signal on the tick we decide to trade, otherwise None.
        """
        if self.filled or self.attempts >= MAX_ATTEMPTS:
            return None

        # An order is out: give it time to fill before sending another.
        if self.attempts > 0:
            self.ticks_since_order += 1
            if self.ticks_since_order < RETRY_AFTER_TICKS:
                return None

        symbol = self._pick_symbol(market)
        if symbol is None:
            return None                      # no ask anywhere yet — wait

        ask = market.best_ask(symbol)
        if not portfolio.can_buy(symbol, QUANTITY, ask):
            logger.warning("Cannot afford %d %s @ %.2f (cash=%.2f) — skipping",
                           QUANTITY, symbol, ask, portfolio.cash)
            return None

        self.attempts += 1
        self.ticks_since_order = 0
        logger.info("Level 1 order %d/%d: BUY %d %s @ %.2f (best ask)",
                    self.attempts, MAX_ATTEMPTS, QUANTITY, symbol, ask)
        return Signal(symbol=symbol, side="buy", quantity=QUANTITY, price=ask)

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Log our own executions; the first fill completes Level 1."""
        logger.info("FILL %s %d %s @ %.2f", side.upper(), quantity, symbol, price)
        if side == "buy":
            self.filled = True

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Log market news (shocks, calendar, fee schedule, ...)."""
        logger.info("EVENT %s — %s %s", event, message, data or "")

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
