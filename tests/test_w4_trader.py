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
    POST_CROSS_WAIT_TICKS,
    PREOPEN_MAX_TICKS,
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

    signals = _run_ticks(bot, market, portfolio, POST_CROSS_WAIT_TICKS + 10)
    buys = [s for s in signals if s is not None]
    assert len(buys) == 1
    assert buys[0].symbol == "MSFT" and buys[0].side == "buy" and buys[0].quantity == 1
    # It waited for the book to rebuild: nothing on the first ticks.
    assert signals[:POST_CROSS_WAIT_TICKS] == [None] * POST_CROSS_WAIT_TICKS


def test_post_cross_buy_without_seeing_preopen():
    """Connecting after the cross still yields the guaranteed order."""
    bot = MyTrader()
    signals = _run_ticks(bot, FakeMarket(), FakePortfolio(), POST_CROSS_WAIT_TICKS + 5)
    assert sum(s is not None for s in signals) == 1


def test_post_cross_buy_waits_for_an_ask():
    bot = MyTrader()
    market, portfolio = FakeMarket(ask=None), FakePortfolio()
    signals = _run_ticks(bot, market, portfolio, POST_CROSS_WAIT_TICKS + 5)
    assert all(s is None for s in signals)
    assert not bot.post_cross_done
    market.ask = 400.10
    assert bot.on_tick(market, portfolio) is not None
    assert bot.post_cross_done


def test_preopen_backstop_resumes_trading():
    bot = MyTrader()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    signals = _run_ticks(bot, FakeMarket(), FakePortfolio(),
                         PREOPEN_MAX_TICKS + POST_CROSS_WAIT_TICKS + 5)
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


# ── pieces merged from the main-branch implementation ───────────────────

def test_position_cap_blocks_further_buys():
    from team.trader import MAX_ABS_POSITION
    bot = MyTrader()
    bot.post_cross_done = True
    portfolio = FakePortfolio()
    portfolio.positions["AMZN"] = MAX_ABS_POSITION
    bot.on_event("CALENDAR_EVENT", "AMZN earnings",
                 {"kind": "earnings", "symbol": "AMZN", "pct": 0.04, "tick": 350})
    market = FakeMarket()
    market.best_ask = lambda s: 150.10          # AMZN has an ask this time
    assert bot.on_tick(market, portfolio) is None
    assert len(bot.pending_events) == 1         # still queued, not sent


def test_stale_event_expires():
    from team.trader import MAX_EVENT_WAIT_TICKS
    bot = MyTrader()
    bot.post_cross_done = True
    bot.on_event("CALENDAR_EVENT", "AMZN earnings",
                 {"kind": "earnings", "symbol": "AMZN", "pct": 0.04, "tick": 350})
    market, portfolio = FakeMarket(), FakePortfolio()   # AMZN has no ask
    for _ in range(MAX_EVENT_WAIT_TICKS + 1):
        assert bot.on_tick(market, portfolio) is None
    assert bot.pending_events == []             # dropped, momentum resumes
    assert bot.on_tick(market, portfolio) is None


def test_on_ipo_rejects_inverted_range():
    assert MyTrader().on_ipo("ORCA", 28.0, 24.0, 4000, {}) is None


def test_ipo_lifecycle_events_are_harmless():
    bot = MyTrader()
    for ev, data in [("IPO_SUB_RESULT", {"ok": True, "symbol": "ORCA"}),
                     ("IPO_SUB_RESULT", {"ok": False, "error": "x"}),
                     ("IPO_PRICED", {"symbol": "ORCA", "offer_price": 27.0}),
                     ("IPO_LISTED", {"symbol": "ORCA"}),
                     ("TRADING_HALT", {"symbol": "AMZN"})]:
        bot.on_event(ev, "msg", data)
    assert bot.pending_events == [] and not bot.in_preopen


def test_post_cross_buy_fires_on_a_long_lived_process():
    """Regression (live, week 4): the SDK price history is a deque(maxlen=200).

    After ~200 snapshots its length stops growing, so a wait measured on
    len(market.prices()) never completes. The wait must be on our own tick
    counter: here the history length is pinned at 200 and never changes.
    """
    bot = MyTrader()
    market, portfolio = FakeMarket(n=200), FakePortfolio()
    bot.on_event("AUCTION_RESULT", "cross done", {"phase": "open", "symbols": {}})
    signals = [bot.on_tick(market, portfolio) for _ in range(POST_CROSS_WAIT_TICKS + 5)]
    assert sum(s is not None for s in signals) == 1
    assert bot.post_cross_done
