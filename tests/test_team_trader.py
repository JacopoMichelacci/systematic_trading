"""Unit tests for team/trader.py — the Level 1 bot's order logic."""

from __future__ import annotations

import pytest

from team import trader as bot
from team.trader import MyTrader


class FakeMarket:
    """Minimal MarketData stand-in: symbol -> best ask (None = no ask)."""

    def __init__(self, asks: dict[str, float | None]) -> None:
        self._asks = asks

    def symbols(self) -> list[str]:
        return list(self._asks)

    def best_ask(self, symbol: str) -> float | None:
        return self._asks.get(symbol)


class FakePortfolio:
    """Minimal Portfolio stand-in with the live can_buy() rule."""

    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.positions: dict[str, int] = {}

    def can_buy(self, symbol: str, qty: int, price: float) -> bool:
        return self.cash >= price * qty * 1.001


@pytest.fixture
def market() -> FakeMarket:
    return FakeMarket({"AAPL": 229.20, "MSFT": 454.30})


def test_first_tick_buys_preferred_symbol_at_best_ask(market):
    sig = MyTrader().on_tick(market, FakePortfolio(250_000))
    assert sig is not None
    assert (sig.symbol, sig.side, sig.quantity, sig.price) == (
        bot.PREFERRED_SYMBOL, "buy", bot.QUANTITY, 454.30)


def test_falls_back_when_preferred_symbol_has_no_ask():
    m = FakeMarket({"MSFT": None, "AAPL": 229.20})
    sig = MyTrader().on_tick(m, FakePortfolio(250_000))
    assert sig is not None and sig.symbol == "AAPL"


def test_no_order_without_any_ask():
    m = FakeMarket({"MSFT": None, "AAPL": None})
    assert MyTrader().on_tick(m, FakePortfolio(250_000)) is None


def test_no_order_when_cash_is_insufficient(market):
    t = MyTrader()
    assert t.on_tick(market, FakePortfolio(10.0)) is None
    assert t.attempts == 0


def test_waits_for_fill_before_retrying(market):
    t, p = MyTrader(), FakePortfolio(250_000)
    assert t.on_tick(market, p) is not None
    for _ in range(bot.RETRY_AFTER_TICKS - 1):
        assert t.on_tick(market, p) is None
    assert t.on_tick(market, p) is not None          # retry after the wait
    assert t.attempts == 2


def test_sits_out_after_fill(market):
    t, p = MyTrader(), FakePortfolio(250_000)
    sig = t.on_tick(market, p)
    t.on_fill("buy", sig.symbol, sig.quantity, sig.price)
    for _ in range(bot.RETRY_AFTER_TICKS * 3):
        assert t.on_tick(market, p) is None


def test_attempts_are_capped(market):
    t, p = MyTrader(), FakePortfolio(250_000)
    orders = [t.on_tick(market, p)
              for _ in range(bot.RETRY_AFTER_TICKS * (bot.MAX_ATTEMPTS + 2))]
    assert sum(o is not None for o in orders) == bot.MAX_ATTEMPTS
