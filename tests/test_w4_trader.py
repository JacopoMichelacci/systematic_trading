"""Week 4 requirements: ORCA IPO bid, post-cross order, no close-time rejects.

The first group tests MyTrader's hooks directly. The last group drives the
real SDK adapter (arena.trader._AdaptedTraderBot) with SessionEvents and
captures what would go to the exchange, so the wiring is covered too.
"""

import asyncio

import pytest
from shared.messages import IPOSubscribe, SessionEvent

from team.trader import (
    IPO_SHARES,
    POST_CROSS_WAIT_SNAPSHOTS,
    PREOPEN_MAX_SNAPSHOTS,
    MyTrader,
    _NoFlattenBot,
)


class FakeMarket:
    """Flat MSFT history (no momentum cross) with a settable snapshot count."""

    def __init__(self, n=30, ask=400.10):
        self.n = n
        self.ask = ask

    def best_ask(self, symbol):
        return self.ask if symbol == "MSFT" else None

    def best_bid(self, symbol):
        return self.ask - 0.20 if symbol == "MSFT" else None

    def prices(self, symbol):
        return [400.0] * self.n if symbol == "MSFT" else []

    def symbols(self):
        return ["MSFT", "AMZN"]


class FakePortfolio:
    def __init__(self, cash=250_000.0):
        self.cash = cash
        self.positions = {}

    def can_buy(self, symbol, qty, price):
        return self.cash >= qty * price * 1.001

    def can_sell(self, symbol, qty):
        return False


# ── on_ipo ──────────────────────────────────────────────────────────────

def test_on_ipo_bids_top_of_range():
    assert MyTrader().on_ipo("ORCA", 24.0, 28.0, 4000, {}) == (IPO_SHARES, 28.0)


def test_on_ipo_clamps_to_deal_size():
    assert MyTrader().on_ipo("TINY", 10.0, 12.0, 50, {}) == (50, 12.0)


@pytest.mark.parametrize("lo,hi,shares", [(24.0, 0.0, 4000), (24.0, 28.0, 0)])
def test_on_ipo_passes_on_bad_input(lo, hi, shares):
    assert MyTrader().on_ipo("ORCA", lo, hi, shares, {}) is None


# ── opening auction gate + post-cross order ─────────────────────────────

def _run_ticks(bot, market, portfolio, count):
    """Advance `count` snapshots, one on_tick per snapshot; return signals."""
    out = []
    for _ in range(count):
        market.n += 1
        out.append(bot.on_tick(market, portfolio))
    return out


def test_no_orders_during_preopen():
    bot = MyTrader()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    signals = _run_ticks(bot, FakeMarket(), FakePortfolio(), 12)
    assert signals == [None] * 12
    assert bot.in_preopen


def test_exactly_one_buy_after_the_cross():
    bot = MyTrader()
    market, portfolio = FakeMarket(), FakePortfolio()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    _run_ticks(bot, market, portfolio, 12)
    bot.on_event("AUCTION_RESULT", "cross done", {"phase": "open", "symbols": {}})
    assert not bot.in_preopen

    signals = _run_ticks(bot, market, portfolio, POST_CROSS_WAIT_SNAPSHOTS + 10)
    buys = [s for s in signals if s is not None]
    assert len(buys) == 1
    assert buys[0].symbol == "MSFT" and buys[0].side == "buy" and buys[0].quantity == 1
    # It waited for the book to rebuild: nothing on the first ticks.
    assert signals[:POST_CROSS_WAIT_SNAPSHOTS] == [None] * POST_CROSS_WAIT_SNAPSHOTS


def test_post_cross_buy_without_seeing_preopen():
    """Connecting after the cross still yields the guaranteed order."""
    bot = MyTrader()
    signals = _run_ticks(bot, FakeMarket(), FakePortfolio(), POST_CROSS_WAIT_SNAPSHOTS + 5)
    assert sum(s is not None for s in signals) == 1


def test_post_cross_buy_waits_for_an_ask():
    bot = MyTrader()
    market, portfolio = FakeMarket(ask=None), FakePortfolio()
    signals = _run_ticks(bot, market, portfolio, POST_CROSS_WAIT_SNAPSHOTS + 5)
    assert all(s is None for s in signals)
    assert not bot.post_cross_done
    market.ask = 400.10
    assert bot.on_tick(market, portfolio) is not None
    assert bot.post_cross_done


def test_preopen_backstop_resumes_trading():
    bot = MyTrader()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    signals = _run_ticks(bot, FakeMarket(), FakePortfolio(),
                         PREOPEN_MAX_SNAPSHOTS + POST_CROSS_WAIT_SNAPSHOTS + 5)
    assert not bot.in_preopen
    assert sum(s is not None for s in signals) == 1


def test_fired_earnings_take_priority_over_post_cross_buy():
    bot = MyTrader()
    market, portfolio = FakeMarket(), FakePortfolio()
    bot.on_event("AUCTION_RESULT", "cross done", {"phase": "open"})
    bot.on_event("CALENDAR_EVENT", "AMZN earnings",
                 {"kind": "earnings", "symbol": "AMZN", "pct": 0.04, "tick": 350})
    # AMZN has no ask in this fake market → shock stays pending, nothing else runs.
    assert _run_ticks(bot, market, portfolio, 8) == [None] * 8
    assert len(bot.pending_events) == 1
    assert not bot.post_cross_done


def test_never_returns_a_sell():
    bot = MyTrader()
    bot.momentum_strategy = lambda m, p: type(
        "S", (), {"side": "sell", "symbol": "MSFT", "quantity": 1, "price": 1.0})()
    bot.post_cross_done = True
    assert bot.on_tick(FakeMarket(), FakePortfolio()) is None


# ── SDK wiring ──────────────────────────────────────────────────────────

def _sdk_bot():
    bot = _NoFlattenBot(MyTrader())
    sent = []

    async def capture(msg):
        sent.append(msg)

    bot._send = capture
    return bot, sent


def test_sdk_sends_ipo_subscription():
    bot, sent = _sdk_bot()
    asyncio.run(bot._on_session_event(SessionEvent(
        event="IPO_OPEN", message="ORCA book open",
        data={"symbol": "ORCA", "offer_range": [24.0, 28.0], "shares": 4000})))
    assert len(sent) == 1
    sub = sent[0]
    assert isinstance(sub, IPOSubscribe)
    assert (sub.symbol, sub.quantity, sub.max_price) == ("ORCA", IPO_SHARES, 28.0)


def test_sdk_session_closed_sends_no_orders():
    bot, sent = _sdk_bot()
    bot._session_open = True
    bot.portfolio.positions.update({"AAPL": 10, "MSFT": 3, "ORCA": 200})
    asyncio.run(bot._on_session_event(SessionEvent(
        event="SESSION_CLOSED", message="closed", data={})))
    assert sent == []
    assert bot._session_open is False


def test_sdk_forwards_auction_events_to_the_trader():
    bot, _ = _sdk_bot()
    owner = bot._owner
    asyncio.run(bot._on_session_event(SessionEvent(
        event="SESSION_PREOPEN", message="pre-open", data={"ticks": 12})))
    assert owner.in_preopen
    asyncio.run(bot._on_session_event(SessionEvent(
        event="AUCTION_RESULT", message="done", data={"phase": "open", "symbols": {}})))
    assert not owner.in_preopen
