"""Week 3 behaviour that still holds in week 4: buy-only earnings reactions.

Symbols follow EVENT_SYMBOLS (AMZN/GOOGL for the week-4 calendar).
"""

from team.trader import MyTrader


class FakeMarket:
    def best_ask(self, symbol):
        return {"AMZN": 150.10, "GOOGL": 250.10, "MSFT": 400.10}.get(symbol)

    def best_bid(self, symbol):
        return {"AMZN": 149.90, "GOOGL": 249.90, "MSFT": 399.90}.get(symbol)

    def prices(self, symbol):
        return [400.0] * 30 if symbol == "MSFT" else []

    def symbols(self):
        return ["AMZN", "GOOGL", "MSFT"]


class FakePortfolio:
    def __init__(self, cash=10000, positions=None):
        self.cash = cash
        self.positions = positions or {}

    def can_buy(self, symbol, qty, price):
        return self.cash >= qty * price * 1.001

    def can_sell(self, symbol, qty):
        return self.positions.get(symbol, 0) >= qty


def _bot():
    bot = MyTrader()
    bot.post_cross_done = True      # isolate the earnings path
    return bot


def test_positive_earnings():
    bot = _bot()

    bot.on_event("CALENDAR_EVENT", "AMZN earnings", {
        "kind": "earnings",
        "symbol": "AMZN",
        "pct": 0.05,
        "tick": 350,
    })

    signal = bot.on_tick(FakeMarket(), FakePortfolio())

    assert signal is not None
    assert signal.symbol == "AMZN"
    assert signal.side == "buy"
    assert signal.quantity == 1


def test_negative_earnings_still_buys():
    """Long-only: a negative print is still answered with a small BUY."""
    bot = _bot()

    bot.on_event("CALENDAR_EVENT", "GOOGL earnings", {
        "kind": "earnings",
        "symbol": "GOOGL",
        "pct": -0.07,
        "tick": 800,
    })

    portfolio = FakePortfolio(positions={"GOOGL": 5})
    signal = bot.on_tick(FakeMarket(), portfolio)

    assert signal is not None
    assert signal.symbol == "GOOGL"
    assert signal.side == "buy"
    assert signal.quantity == 1


def test_shock_without_pct():
    bot = _bot()

    bot.on_event("SHOCK", "AMZN shock", {
        "shock_id": "test-shock-1",
        "affected": ["AMZN"],
        "prices": {"AMZN": 155.0},
    })

    signal = bot.on_tick(FakeMarket(), FakePortfolio())

    assert signal is not None
    assert signal.symbol == "AMZN"
    assert signal.side == "buy"
    assert signal.quantity == 1


def test_no_illegal_sell():
    bot = _bot()

    bot.on_event("CALENDAR_EVENT", "GOOGL earnings", {
        "kind": "earnings",
        "symbol": "GOOGL",
        "pct": -0.07,
        "tick": 800,
    })

    # No GOOGL shares and insufficient cash.
    portfolio = FakePortfolio(cash=0, positions={})
    signal = bot.on_tick(FakeMarket(), portfolio)

    assert signal is None
    assert len(bot.pending_events) == 1


def test_duplicate_event():
    bot = _bot()
    data = {
        "kind": "earnings",
        "symbol": "AMZN",
        "pct": 0.05,
        "tick": 350,
    }

    bot.on_event("CALENDAR_EVENT", "AMZN earnings", data)
    bot.on_event("CALENDAR_EVENT", "AMZN earnings", data)

    assert len(bot.pending_events) == 1


def test_other_symbols_ignored():
    bot = _bot()

    bot.on_event("CALENDAR_EVENT", "NVDA earnings", {
        "kind": "earnings", "symbol": "NVDA", "pct": 0.05, "tick": 300,
    })

    assert bot.pending_events == []
