
from team.trader import MyTrader


class FakeMarket:
    def best_ask(self, symbol):
        return {"NVDA": 150.10, "TSLA": 250.10}.get(symbol)

    def best_bid(self, symbol):
        return {"NVDA": 149.90, "TSLA": 249.90}.get(symbol)


class FakePortfolio:
    def __init__(self, cash=10000, positions=None):
        self.cash = cash
        self.positions = positions or {}

    def can_buy(self, symbol, qty, price):
        return self.cash >= qty * price * 1.001

    def can_sell(self, symbol, qty):
        return self.positions.get(symbol, 0) >= qty


def test_positive_earnings():
    bot = MyTrader()

    bot.on_event("CALENDAR_EVENT", "NVDA earnings", {
        "kind": "earnings",
        "symbol": "NVDA",
        "pct": 0.05,
        "tick": 300,
    })

    signal = bot.on_tick(FakeMarket(), FakePortfolio())

    assert signal is not None
    assert signal.symbol == "NVDA"
    assert signal.side == "buy"
    assert signal.quantity == 1


def test_negative_earnings():
    bot = MyTrader()

    bot.on_event("CALENDAR_EVENT", "TSLA earnings", {
        "kind": "earnings",
        "symbol": "TSLA",
        "pct": -0.07,
        "tick": 700,
    })

    portfolio = FakePortfolio(positions={"TSLA": 5})
    signal = bot.on_tick(FakeMarket(), portfolio)

    assert signal is not None
    assert signal.symbol == "TSLA"
    assert signal.side == "sell"
    assert signal.quantity == 1


def test_shock_without_pct():
    bot = MyTrader()

    bot.on_event("SHOCK", "NVDA shock", {
        "shock_id": "test-shock-1",
        "affected": ["NVDA"],
        "prices": {"NVDA": 155.0},
    })

    signal = bot.on_tick(FakeMarket(), FakePortfolio())

    assert signal is not None
    assert signal.symbol == "NVDA"
    assert signal.side == "buy"
    assert signal.quantity == 1


def test_no_illegal_sell():
    bot = MyTrader()

    bot.on_event("CALENDAR_EVENT", "TSLA earnings", {
        "kind": "earnings",
        "symbol": "TSLA",
        "pct": -0.07,
        "tick": 700,
    })

    # No TSLA shares and insufficient cash.
    portfolio = FakePortfolio(cash=0, positions={})
    signal = bot.on_tick(FakeMarket(), portfolio)

    assert signal is None
    assert len(bot.pending_events) == 1


def test_duplicate_event():
    bot = MyTrader()
    data = {
        "kind": "earnings",
        "symbol": "NVDA",
        "pct": 0.05,
        "tick": 300,
    }

    bot.on_event("CALENDAR_EVENT", "NVDA earnings", data)
    bot.on_event("CALENDAR_EVENT", "NVDA earnings", data)

    assert len(bot.pending_events) == 1
