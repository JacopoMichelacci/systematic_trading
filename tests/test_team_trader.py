"""team/trader.py — the graded behaviours, week by week.

  week 3   earnings reactions (buy-only)
  week 4   IPO bid, nothing in the pre-open, orders after the cross,
           no close-time orders
  week 5   no liquidation, hard position limits, survive a halt and resume
  week 6   all of the above, the closing auction, staying connected

The first groups drive MyTrader's hooks directly. The last ones drive the
real SDK adapter (team.trader._SeatBot) with exchange messages and capture
what would be sent to the venue, so the wiring is covered too.
"""

import asyncio

import pytest
from shared.messages import (
    IPOSubscribe,
    Leaderboard,
    PlaceOrder,
    PortfolioUpdate,
    SessionEvent,
)

import team.trader as tt
from team.passive_broker import PassiveBroker
from team.trader import TRADABLE, MyTrader, _SeatBot


@pytest.fixture(autouse=True)
def _cheap_venue(monkeypatch):
    """Weeks 3–6 behaviour is tested on a venue where taking is cheap; the
    week-7 tests set the house venue's 200 bp themselves."""
    monkeypatch.setattr(tt, "DEFAULT_TAKER_FEE", 0.001)
    monkeypatch.setattr(tt, "TAKING_ENABLED", True)


PRICES = {"MSFT": 400.0, "INTC": 24.0, "AAPL": 220.0, "NVDA": 110.0,
          "TSLA": 240.0, "AMZN": 170.0, "GOOGL": 165.0, "META": 660.0,
          "NFLX": 570.0, "AMD": 180.0}


class FakeMarket:
    """Every equity quoted; flat MSFT history (no momentum cross)."""

    def __init__(self):
        self.asks = dict(PRICES)
        self.asks.update({"ARENA10": 300.0, "ORCA": 30.0, "VLTA": 50.0})
        self.history = [400.0] * 200        # a full, never-growing deque

    def best_ask(self, symbol):
        return self.asks.get(symbol)

    def best_bid(self, symbol):
        ask = self.asks.get(symbol)
        return None if ask is None else ask - 0.10

    def mid_price(self, symbol):
        ask = self.asks.get(symbol)
        return None if ask is None else ask - 0.05

    def prices(self, symbol):
        return list(self.history) if symbol == "MSFT" else []

    def symbols(self):
        return list(self.asks)


class FakePortfolio:
    def __init__(self, cash=250_000.0, positions=None, teammates=None):
        self.cash = cash
        self.positions = dict(positions or {})
        self.synced = True
        self.teammates = teammates

    def can_buy(self, symbol, qty, price):
        return self.cash >= qty * price * 1.001

    def can_sell(self, symbol, qty):
        return self.positions.get(symbol, 0) >= qty


def ticks(bot, market, portfolio, n):
    """n on_tick calls; fills are applied so positions stay truthful."""
    out = []
    for _ in range(n):
        signal = bot.on_tick(market, portfolio)
        if signal is not None:
            portfolio.positions[signal.symbol] = (
                portfolio.positions.get(signal.symbol, 0) + signal.quantity)
            portfolio.cash -= signal.price * signal.quantity
            out.append(signal)
    return out


WARMUP = tt.CONNECT_SETTLE_TICKS + tt.POST_CROSS_WAIT_TICKS + 2
SEED_TICKS = WARMUP + len(TRADABLE) * tt.SEED_SPACING_TICKS + 5


def seeded():
    """A bot that has finished its seed basket (one share of everything)."""
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    ticks(bot, market, portfolio, SEED_TICKS)
    assert bot.seed_queue == []
    return bot, market, portfolio


# ── every order: buy-only, one share, equities only ─────────────────────

def test_seed_basket_buys_one_share_of_every_equity_once():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    signals = ticks(bot, market, portfolio, SEED_TICKS)
    assert sorted(s.symbol for s in signals) == sorted(TRADABLE)
    assert all(s.side == "buy" and s.quantity == 1 for s in signals)
    assert all(s.price == PRICES[s.symbol] for s in signals)


def test_nothing_is_sent_before_the_books_have_settled():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    assert ticks(bot, market, portfolio, tt.CONNECT_SETTLE_TICKS) == []


def test_never_trades_futures_or_ipo_names():
    bot, market, portfolio = seeded()
    signals = ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 12)
    assert signals
    assert {s.symbol for s in signals} <= set(TRADABLE)


def test_heartbeat_keeps_trading_through_the_block():
    bot, market, portfolio = seeded()
    signals = ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 5 + 5)
    assert len(signals) == 5                    # one per heartbeat interval
    assert len({s.symbol for s in signals}) == 5    # rotating


def test_a_non_buy_signal_is_blocked():
    bot, market, portfolio = seeded()
    bot._decide = lambda m, p: type("S", (), {
        "side": "sell", "symbol": "MSFT", "quantity": 1, "price": 400.0})()
    assert bot.on_tick(market, portfolio) is None
    bot._decide = lambda m, p: type("S", (), {
        "side": "buy", "symbol": "MSFT", "quantity": 500, "price": 400.0})()
    assert bot.on_tick(market, portfolio) is None


def test_a_crashing_strategy_never_raises_out_of_on_tick():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    market.best_ask = lambda s: 1 / 0
    assert ticks(bot, market, portfolio, WARMUP + 10) == []


# ── week 5: hard position limits ────────────────────────────────────────

def test_default_caps_are_conservative_until_the_limit_is_known():
    assert MyTrader()._caps() == (tt.DEFAULT_POSITION_LIMIT // 5,
                                  tt.DEFAULT_POSITION_LIMIT // 2)


def test_week_changed_sets_the_position_limit():
    bot = MyTrader()
    bot.on_event("WEEK_CHANGED", "week 6", {"week": 6, "position_limit": 1000})
    assert bot.position_limit == 1000
    assert bot._caps() == (tt.SEAT_HARD_CAP, 500)
    bot.on_event("WEEK_CHANGED", "junk", {"position_limit": "lots"})
    assert bot.position_limit == 1000           # bad data is ignored


def test_seat_cap_blocks_a_symbol_and_only_that_symbol():
    bot, market = MyTrader(), FakeMarket()
    seat_cap, _ = bot._caps()
    portfolio = FakePortfolio(positions={"MSFT": seat_cap}, teammates={})
    signals = ticks(bot, market, portfolio, SEED_TICKS)
    assert "MSFT" not in {s.symbol for s in signals}
    assert len(signals) == len(TRADABLE) - 1
    assert portfolio.positions["MSFT"] == seat_cap


def test_team_cap_counts_the_other_seats():
    bot, market = MyTrader(), FakeMarket()
    _, team_cap = bot._caps()
    portfolio = FakePortfolio(positions={"MSFT": 10},
                              teammates={"MSFT": team_cap - 10})
    signals = ticks(bot, market, portfolio, SEED_TICKS)
    assert "MSFT" not in {s.symbol for s in signals}


def test_team_cap_is_assumed_when_teammates_are_unknown():
    bot, market = MyTrader(), FakeMarket()
    _, team_cap = bot._caps()
    seats = len(tt.TEAM_BOT_IDS)
    portfolio = FakePortfolio(positions={"MSFT": team_cap // seats + 1},
                              teammates=None)
    signal, why = bot._check_buy(market, portfolio, "MSFT")
    assert signal is None and "team cap" in why


def test_positions_never_exceed_the_caps_however_long_it_runs():
    bot, market = MyTrader(), FakeMarket()
    bot.on_event("WEEK_CHANGED", "w", {"position_limit": 200})
    seat_cap, team_cap = bot._caps()            # 40 per seat, 100 per team
    portfolio = FakePortfolio(positions={s: 35 for s in TRADABLE},
                              teammates={s: 55 for s in TRADABLE})
    ticks(bot, market, portfolio, 20_000)
    for symbol in TRADABLE:
        assert portfolio.positions[symbol] <= seat_cap
        assert portfolio.positions[symbol] + 55 <= team_cap


def test_session_budgets_bound_shares_orders_and_dollars():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("WEEK_CHANGED", "w", {"position_limit": 100_000})
    signals = ticks(bot, market, portfolio, 60_000)
    assert len(signals) <= tt.MAX_ORDERS_PER_SESSION
    assert sum(s.price for s in signals) <= tt.MAX_SESSION_NOTIONAL
    for symbol in TRADABLE:
        assert portfolio.positions.get(symbol, 0) <= tt.SESSION_SHARES_PER_SYMBOL


def test_no_orders_until_the_portfolio_is_known():
    """A fresh process holds an empty book: the caps would mean nothing."""
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    portfolio.synced = False
    assert ticks(bot, market, portfolio, SEED_TICKS) == []
    portfolio.synced = True
    assert ticks(bot, market, portfolio, SEED_TICKS)


def test_not_enough_cash_means_no_order():
    bot, market = MyTrader(), FakeMarket()
    assert ticks(bot, market, FakePortfolio(cash=10.0), SEED_TICKS) == []


def test_an_ask_far_from_the_mid_is_not_lifted():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    market.mid_price = lambda s: PRICES.get(s, 0) / 1.2 or None
    assert ticks(bot, market, portfolio, SEED_TICKS) == []


# ── week 5: no liquidation ──────────────────────────────────────────────

def test_liquidation_of_our_seat_stops_all_orders(monkeypatch):
    monkeypatch.setenv("TEAM_ID", "butter_chicken_pasta_trader_1")
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("LIQUIDATION", "x", {"team_id": "someone_else_trader_1"})
    assert not bot.liquidated
    bot.on_event("LIQUIDATION", "x", {"team_id": "butter_chicken_pasta_trader_1"})
    assert bot.liquidated
    assert ticks(bot, market, portfolio, SEED_TICKS) == []


def test_margin_call_stops_all_buys(monkeypatch):
    monkeypatch.setenv("TEAM_ID", "butter_chicken_pasta_trader_1")
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("MARGIN_CALL", "x", {"team_id": "butter_chicken_pasta_trader_1"})
    assert ticks(bot, market, portfolio, SEED_TICKS) == []


# ── week 5: halts ───────────────────────────────────────────────────────

def test_no_orders_in_a_halted_symbol_then_a_buy_when_it_resumes():
    bot, market, portfolio = seeded()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    during = ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 12)
    assert during, "other symbols keep trading during a single-name halt"
    assert "MSFT" not in {s.symbol for s in during}

    bot.on_event("TRADING_RESUMED", "resume", {"symbol": "MSFT"})
    after = ticks(bot, market, portfolio, tt.RESUME_SETTLE_TICKS + 2)
    assert [s.symbol for s in after] == ["MSFT"]


def test_a_halt_before_the_seed_buy_defers_that_symbol():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "INTC", "duration": 300})
    signals = ticks(bot, market, portfolio, SEED_TICKS)
    assert "INTC" not in {s.symbol for s in signals}
    bot.on_event("TRADING_RESUMED", "resume", {"symbol": "INTC"})
    after = ticks(bot, market, portfolio, tt.RESUME_SETTLE_TICKS + 2)
    assert after and after[0].symbol == "INTC"


def test_a_halt_times_out_locally_if_the_resume_event_is_missed():
    bot, market, portfolio = seeded()
    clock = [1000.0]
    bot._now = lambda: clock[0]
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    clock[0] += 300 + tt.HALT_GRACE_SEC - 1
    assert "MSFT" not in {s.symbol for s in ticks(bot, market, portfolio, 50)}
    clock[0] += 2
    after = ticks(bot, market, portfolio, tt.RESUME_SETTLE_TICKS + 3)
    assert "MSFT" in {s.symbol for s in after}


def test_resume_falls_back_to_another_symbol_if_the_book_is_empty():
    bot, market, portfolio = seeded()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    market.asks["MSFT"] = None                  # nobody quotes it afterwards
    bot.on_event("TRADING_RESUMED", "resume", {"symbol": "MSFT"})
    after = ticks(bot, market, portfolio, tt.RESUME_FALLBACK_TICKS + 3)
    assert after and after[0].symbol != "MSFT"  # activity still resumed


def test_market_wide_halt_stops_everything_until_resumed():
    bot, market, portfolio = seeded()
    bot.on_event("MARKET_HALT", "L1", {"level": 1, "duration": 900})
    assert ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 3) == []
    bot.on_event("MARKET_RESUMED", "lifted", {"level": 1})
    assert len(ticks(bot, market, portfolio, 3)) == 1


def test_teacher_lifting_the_breakers_resumes_the_named_symbols():
    bot, market, portfolio = seeded()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "AMD", "duration": 300})
    bot.on_event("MARKET_RESUMED", "lifted", {"symbols": ["AMD"]})
    assert "AMD" not in bot.halted
    after = ticks(bot, market, portfolio, tt.RESUME_SETTLE_TICKS + 2)
    assert "AMD" in {s.symbol for s in after}


def test_a_symbol_halted_again_right_after_resuming_gets_no_order():
    bot, market, portfolio = seeded()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    bot.on_event("TRADING_RESUMED", "resume", {"symbol": "MSFT"})
    bot.on_event("TRADING_HALT", "halt #2", {"symbol": "MSFT", "duration": 300})
    signals = ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 3)
    assert "MSFT" not in {s.symbol for s in signals}


# ── week 3: earnings ────────────────────────────────────────────────────

def earnings(bot, symbol, pct, tick=250):
    bot.on_event("CALENDAR_EVENT", f"{symbol} earnings", {
        "kind": "earnings", "symbol": symbol, "pct": pct, "tick": tick,
        "affected": [symbol]})


def test_positive_earnings_buy_one_share():
    bot, market, portfolio = seeded()
    earnings(bot, "INTC", 0.05)
    signals = ticks(bot, market, portfolio, 2)
    assert [(s.symbol, s.side, s.quantity) for s in signals] == [("INTC", "buy", 1)]


def test_negative_earnings_are_skipped_not_bought_and_never_sold():
    bot, market, portfolio = seeded()
    earnings(bot, "MSFT", -0.07)
    assert ticks(bot, market, portfolio, 5) == []
    assert bot.pending_events == []


def test_duplicate_and_non_equity_events_are_ignored():
    bot, _, _ = seeded()
    earnings(bot, "INTC", 0.05)
    earnings(bot, "INTC", 0.05)
    earnings(bot, "VLTA", 0.05)
    bot.on_event("CALENDAR_EVENT", "econ", {"kind": "econ_print", "pct": 0.02,
                                            "market_wide": True, "tick": 950})
    assert [e["symbol"] for e in bot.pending_events] == ["INTC"]


def test_earnings_on_a_halted_symbol_wait_for_the_resume():
    bot, market, portfolio = seeded()
    earnings(bot, "MSFT", 0.06)
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    assert "MSFT" not in {s.symbol for s in ticks(bot, market, portfolio, 100)}
    bot.on_event("TRADING_RESUMED", "resume", {"symbol": "MSFT"})
    after = ticks(bot, market, portfolio, tt.RESUME_SETTLE_TICKS + 4)
    assert [s.symbol for s in after].count("MSFT") >= 1


def test_an_unfillable_event_expires_without_blocking_anything_else():
    bot, market, portfolio = seeded()
    market.asks["INTC"] = None
    earnings(bot, "INTC", 0.05)
    signals = ticks(bot, market, portfolio, tt.MAX_EVENT_WAIT_TICKS + 5)
    assert bot.pending_events == []
    assert signals and "INTC" not in {s.symbol for s in signals}


# ── week 4: the auctions ────────────────────────────────────────────────

def test_no_orders_in_the_pre_open_then_orders_after_the_cross():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    assert ticks(bot, market, portfolio, 60) == []
    bot.on_event("AUCTION_RESULT", "cross", {"phase": "open", "symbols": {}})
    assert ticks(bot, market, portfolio, tt.POST_CROSS_WAIT_TICKS - 2) == []
    assert ticks(bot, market, portfolio, SEED_TICKS)


def test_pre_open_backstop_if_the_cross_event_is_missed():
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    assert ticks(bot, market, portfolio, tt.PREOPEN_MAX_TICKS) == []
    assert ticks(bot, market, portfolio, SEED_TICKS)


def test_orders_after_the_cross_on_a_process_that_has_run_for_hours():
    """Regression (live, week 4): the SDK price history is deque(maxlen=200).

    FakeMarket's history is pinned at 200 entries; every wait in the bot is
    on its own tick counter, so it must still trade.
    """
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    ticks(bot, market, portfolio, 5_000)        # hours of open play
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    ticks(bot, market, portfolio, 30)
    bot.on_event("AUCTION_RESULT", "cross", {"phase": "open", "symbols": {}})
    signals = ticks(bot, market, portfolio, SEED_TICKS)
    assert sorted(s.symbol for s in signals) == sorted(TRADABLE)


def test_a_new_session_resets_budgets_and_halts():
    bot, _, _ = seeded()
    bot.on_event("TRADING_HALT", "halt", {"symbol": "MSFT", "duration": 300})
    assert bot.orders_sent == len(TRADABLE)
    bot.on_event("SESSION_PREOPEN", "pre-open", {"ticks": 12})
    assert bot.orders_sent == 0 and bot.halted == {} and bot.spent == 0.0
    assert bot.seed_queue == list(TRADABLE)


def test_no_orders_in_the_closing_auction():
    bot, market, portfolio = seeded()
    bot.on_event("SESSION_PRECLOSE", "closing", {"ticks": 10})
    assert ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 3) == []
    bot.on_event("AUCTION_RESULT", "closing cross", {"phase": "close"})
    assert ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 3) == []


# ── week 4 / 6: the IPO bid ─────────────────────────────────────────────

def test_ipo_bid_is_top_of_range_and_small():
    bot = MyTrader()
    bot.on_event("WEEK_CHANGED", "w6", {"position_limit": 1000})
    assert bot.on_ipo("VLTA", 40.0, 50.0, 5000, {}) == (tt.IPO_SHARES, 50.0)


def test_ipo_bid_is_bounded_by_deal_limit_and_notional():
    bot = MyTrader()
    bot.on_event("WEEK_CHANGED", "w6", {"position_limit": 1000})
    assert bot.on_ipo("TINY", 10.0, 12.0, 50, {}) == (50, 12.0)
    assert bot.on_ipo("RICH", 900.0, 1000.0, 5000, {}) == (10, 1000.0)
    assert MyTrader().on_ipo("VLTA", 40.0, 50.0, 5000, {}) == (
        tt.DEFAULT_POSITION_LIMIT // 10, 50.0)


@pytest.mark.parametrize("lo,hi,shares", [
    (24.0, 0.0, 4000), (24.0, 28.0, 0), (28.0, 24.0, 4000),
    (float("nan"), 28.0, 4000), (24.0, 2_000_000.0, 4000)])
def test_ipo_passes_on_bad_terms(lo, hi, shares):
    assert MyTrader().on_ipo("VLTA", lo, hi, shares, {}) is None


# ── week 6: nothing may raise into the SDK's listen loop ────────────────

@pytest.mark.parametrize("event", [
    "WEEK_CHANGED", "SESSION_PREOPEN", "AUCTION_RESULT", "SESSION_PRECLOSE",
    "TRADING_HALT", "TRADING_RESUMED", "MARKET_HALT", "MARKET_RESUMED",
    "LIQUIDATION", "MARGIN_CALL", "IPO_SUB_RESULT", "IPO_PRICED",
    "FLASH", "FEE_SCHEDULE",
    "CALENDAR_EVENT", "SHOCK", "DIVIDEND", "SOMETHING_NEW"])
@pytest.mark.parametrize("data", [
    None, {}, [], "text", {"symbol": None}, {"symbol": 5, "duration": "x"},
    {"symbols": "MSFT", "affected": 7, "kind": "earnings", "pct": "up"},
    {"position_limit": -3, "team_id": None, "phase": 9, "duration": -1}])
def test_on_event_survives_any_payload(event, data):
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event(event, "message", data)
    ticks(bot, market, portfolio, 50)           # and the bot still ticks


# ── week 7: flashes, fees, and an O(1) hot path ────────────────────────

HOUSE = {"taker": 0.02, "rebate": -0.005}


def flash(bot, symbol, pct, start=None):
    bot.on_event("FLASH", "flash", {
        "symbol": symbol, "pct": pct, "revert_ticks": 2, "tick": 123,
        "start_price": PRICES.get(symbol, 30.0) if start is None else start})


def week7_bot(positions=None):
    bot, market = MyTrader(), FakeMarket()
    bot.on_event("FEE_SCHEDULE", "house", HOUSE)
    portfolio = FakePortfolio(
        positions={s: 40 for s in TRADABLE} if positions is None else positions,
        teammates={})
    return bot, market, portfolio


def test_flash_down_is_answered_with_a_buy_at_the_bottom():
    bot, market, portfolio = week7_bot()
    flash(bot, "TSLA", -0.02)
    signal = bot.take_flash(market, portfolio)
    assert (signal.symbol, signal.side, signal.quantity) == ("TSLA", "buy", 1)
    assert signal.price == 235.20               # 240 × 0.98, on the cent grid


def test_flash_up_is_answered_with_a_sell_of_shares_we_hold():
    bot, market, portfolio = week7_bot()
    flash(bot, "AAPL", 0.025)
    signal = bot.take_flash(market, portfolio)
    assert (signal.symbol, signal.side, signal.quantity) == ("AAPL", "sell", 1)
    assert signal.price == 225.50               # 220 × 1.025


def test_flash_up_without_inventory_is_answered_with_a_passive_buy():
    bot, market, portfolio = week7_bot(positions={})
    flash(bot, "AAPL", 0.02)
    signal = bot.take_flash(market, portfolio)
    assert signal.side == "buy" and signal.price == 215.60     # 220 × 0.98


def test_flash_orders_are_passive_never_marketable():
    """Buys below the pre-flash bid, sells above the pre-flash ask."""
    bot, market, portfolio = week7_bot()
    for symbol in TRADABLE:
        for pct in (-0.025, -0.015, 0.015, 0.025):
            flash(bot, symbol, pct)
            signal = bot.take_flash(market, portfolio)
            if signal.side == "buy":
                assert signal.price < market.best_bid(symbol)
            else:
                assert signal.price > market.best_ask(symbol)
            assert abs(signal.price / PRICES[symbol] - 1) < 0.031  # in the band


def test_flash_sells_never_take_the_position_below_the_floor():
    bot, market, portfolio = week7_bot(
        positions={"AAPL": tt.FLASH_KEEP_SHARES + 2})
    sides = []
    for _ in range(6):
        flash(bot, "AAPL", 0.02)
        sides.append(bot.take_flash(market, portfolio).side)
    assert sides == ["sell", "sell", "buy", "buy", "buy", "buy"]


def test_one_order_per_flash_and_none_without_one():
    bot, market, portfolio = week7_bot()
    assert bot.take_flash(market, portfolio) is None
    flash(bot, "TSLA", -0.02)
    assert bot.take_flash(market, portfolio) is not None
    assert bot.take_flash(market, portfolio) is None
    assert ticks(bot, market, portfolio, 500) == []


def test_flash_is_answered_from_on_tick_if_the_event_path_did_not():
    bot, market, portfolio = week7_bot()
    flash(bot, "NVDA", -0.02)
    signal = bot.on_tick(market, portfolio)     # very first tick, no warm-up
    assert (signal.symbol, signal.side) == ("NVDA", "buy")
    assert bot.on_tick(market, portfolio) is None


def test_flash_on_an_ipo_name_is_answered_too():
    bot, market, portfolio = week7_bot(positions={"ORCA": 158})
    flash(bot, "ORCA", 0.02, start=30.0)
    signal = bot.take_flash(market, portfolio)
    assert (signal.symbol, signal.side, signal.price) == ("ORCA", "sell", 30.60)


def test_flash_reference_falls_back_to_the_book_mid():
    bot, market, portfolio = week7_bot()
    bot.on_event("FLASH", "flash", {"symbol": "MSFT", "pct": -0.02})
    signal = bot.take_flash(market, portfolio)
    assert signal.side == "buy" and signal.price == 391.95     # 399.95 × 0.98


def test_a_stale_flash_is_not_answered():
    bot, market, portfolio = week7_bot()
    clock = [100.0]
    bot._now = lambda: clock[0]
    flash(bot, "TSLA", -0.02)
    clock[0] += tt.FLASH_MAX_AGE_SEC + 0.1
    assert bot.take_flash(market, portfolio) is None


@pytest.mark.parametrize("block", [
    ("SESSION_PREOPEN", {"ticks": 12}), ("SESSION_PRECLOSE", {"ticks": 10}),
    ("TRADING_HALT", {"symbol": "TSLA", "duration": 300}),
    ("MARKET_HALT", {"level": 1, "duration": 900})])
def test_no_flash_order_in_an_auction_or_a_halt(block):
    bot, market, portfolio = week7_bot()
    bot.on_event(*block[:1], "msg", block[1])
    flash(bot, "TSLA", -0.02)
    assert bot.take_flash(market, portfolio) is None


def test_no_flash_order_after_a_liquidation(monkeypatch):
    monkeypatch.setenv("TEAM_ID", "butter_chicken_pasta_trader_1")
    bot, market, portfolio = week7_bot()
    bot.on_event("LIQUIDATION", "x", {"team_id": "butter_chicken_pasta_trader_1"})
    flash(bot, "TSLA", -0.02)
    assert bot.take_flash(market, portfolio) is None


def test_flash_buy_respects_the_position_caps():
    bot, market, _ = week7_bot()
    seat_cap, _ = bot._caps()
    portfolio = FakePortfolio(positions={"TSLA": seat_cap}, teammates={})
    flash(bot, "TSLA", -0.02)
    signal = bot.take_flash(market, portfolio)
    assert signal.side == "sell"                # at the cap: sell, never buy more
    assert signal.price > market.best_ask("TSLA")


def test_flash_is_answered_even_before_the_portfolio_is_known():
    bot, market, portfolio = week7_bot()
    portfolio.synced = False
    flash(bot, "TSLA", 0.02)
    assert bot.take_flash(market, portfolio).side == "buy"   # no blind sells


def test_flash_order_budget():
    bot, market, portfolio = week7_bot()
    sent = 0
    for _ in range(tt.MAX_FLASH_ORDERS + 10):
        flash(bot, "TSLA", -0.02)
        sent += bot.take_flash(market, portfolio) is not None
    assert sent == tt.MAX_FLASH_ORDERS


@pytest.mark.parametrize("data", [
    {"symbol": None, "pct": 0.02}, {"symbol": "TSLA", "pct": "big"},
    {"symbol": "TSLA", "pct": float("nan")}, {"symbol": "TSLA", "pct": 0},
    {"symbol": "TSLA", "pct": True}, {"pct": 0.02}, {}])
def test_a_malformed_flash_is_ignored(data):
    bot, market, portfolio = week7_bot()
    bot.on_event("FLASH", "flash", data)
    assert bot.take_flash(market, portfolio) is None
    assert bot.on_tick(market, portfolio) is None


def test_no_liquidity_taking_on_an_expensive_venue():
    """200 bp to take: no seeds, no heartbeat, no earnings buys — nothing."""
    bot, market, portfolio = week7_bot()
    earnings(bot, "TSLA", 0.08)
    assert ticks(bot, market, portfolio, 5_000) == []
    assert bot.orders_sent == 0


def test_taking_is_off_until_the_fee_is_known(monkeypatch):
    monkeypatch.setattr(tt, "DEFAULT_TAKER_FEE", None)
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    assert ticks(bot, market, portfolio, SEED_TICKS) == []
    bot.on_event("FEE_SCHEDULE", "cheap", {"taker": 0.0008, "rebate": 0.0005})
    assert len(ticks(bot, market, portfolio, SEED_TICKS)) == len(TRADABLE)
    bot.on_event("FEE_SCHEDULE", "repriced", HOUSE)
    assert ticks(bot, market, portfolio, tt.HEARTBEAT_TICKS * 5) == []


def test_taking_is_off_on_a_cheap_venue_unless_asked_for(monkeypatch):
    """The seats also run on the 8–14 bp team venues: flash-only there too."""
    monkeypatch.setattr(tt, "TAKING_ENABLED", False)
    bot, market, portfolio = MyTrader(), FakeMarket(), FakePortfolio()
    bot.on_event("FEE_SCHEDULE", "team venue", {"taker": 0.0008, "rebate": 0.0005})
    earnings(bot, "TSLA", 0.08)
    assert ticks(bot, market, portfolio, 5_000) == []
    flash(bot, "TSLA", -0.02)
    assert bot.take_flash(market, portfolio).side == "buy"   # flashes still answered


def test_run_block_builds_one_seat_per_bot_and_venue():
    import team.run_block as rb
    urls = rb.venue_urls("wss://feed.algoarena-st.duckdns.org", rb.TEAM_VENUE_PORTS)
    assert urls == {str(p): f"wss://feed-{p}.algoarena-st.duckdns.org"
                    for p in (8767, 8769, 8770, 8773)}
    assert rb.venue_urls("ws://localhost:8765", rb.TEAM_VENUE_PORTS) == {}
    seat = rb.Seat("bot_1", "team.trader", "8770", urls["8770"])
    assert seat.name == "bot_1@8770"
    assert rb.Seat("bot_1", "team.trader").name == "bot_1"


def test_hot_path_is_constant_time_and_never_reads_the_history():
    """Week 7: snapshots every 0.2 s. on_tick must not scan anything.

    The market's history accessor raises if touched, and a tick is timed:
    the budget is 50 µs, against a 200 000 µs gap between snapshots.
    """
    import time as _time
    bot, market, portfolio = week7_bot()

    def no_history(symbol):
        raise AssertionError("the hot path read the price history")

    market.prices = no_history
    bot.on_event("TRADING_HALT", "halt", {"symbol": "AMD", "duration": 300})
    ticks(bot, market, portfolio, 2_000)            # warm
    n = 50_000
    started = _time.perf_counter()
    for _ in range(n):
        bot.on_tick(market, portfolio)
    per_tick = (_time.perf_counter() - started) / n
    assert per_tick < 50e-6, f"on_tick takes {per_tick * 1e6:.1f} µs"

    # ...and a flash is turned into an order in constant time too.
    started = _time.perf_counter()
    for _ in range(200):
        bot.pending_flash = {"symbol": "TSLA", "pct": -0.02,
                             "start": 240.0, "at": bot._now()}
        bot.flash_orders = 0
        assert bot._flash_signal(market, portfolio) is not None
    assert (_time.perf_counter() - started) / 200 < 2e-3


# ── SDK wiring (the real adapter) ───────────────────────────────────────

def sdk_bot():
    bot = _SeatBot(MyTrader())
    sent = []

    async def capture(msg):
        sent.append(msg)

    bot._send = capture
    return bot, sent


def event(bot, name, data=None, message="msg"):
    asyncio.run(bot._on_session_event(
        SessionEvent(event=name, message=message, data=data or {})))


def leaderboard(rows, brokers=()):
    return Leaderboard(traders=list(rows), brokers=list(brokers),
                       exchange_fees=0.0, tick=1)


def test_sdk_sends_the_ipo_subscription():
    bot, sent = sdk_bot()
    event(bot, "WEEK_CHANGED", {"week": 6, "position_limit": 1000})
    event(bot, "IPO_OPEN", {"symbol": "VLTA", "offer_range": [40.0, 50.0],
                            "shares": 5000})
    assert len(sent) == 1 and isinstance(sent[0], IPOSubscribe)
    assert (sent[0].symbol, sent[0].quantity, sent[0].max_price) == (
        "VLTA", tt.IPO_SHARES, 50.0)


def test_sdk_answers_a_flash_from_inside_the_event_handler(monkeypatch):
    monkeypatch.setattr(tt._engine_config, "TEAM_ID",
                        "butter_chicken_pasta_trader_1")
    bot, sent = sdk_bot()
    bot._session_open = True
    bot._on_leaderboard(leaderboard(
        [{"team_id": "butter_chicken_pasta_trader_1", "cash": 248_000.0,
          "positions": {"TSLA": 40, "AAPL": 40}}]))
    event(bot, "FEE_SCHEDULE", {"taker": 0.02, "rebate": -0.005})
    event(bot, "FLASH", {"symbol": "TSLA", "pct": -0.021, "revert_ticks": 2,
                         "tick": 500, "start_price": 240.0})
    event(bot, "FLASH", {"symbol": "AAPL", "pct": 0.018, "revert_ticks": 2,
                         "tick": 650, "start_price": 220.0})
    assert [type(m) for m in sent] == [PlaceOrder, PlaceOrder]
    assert [(m.symbol, m.side, m.quantity, m.order_type, m.price) for m in sent] == [
        ("TSLA", "buy", 1, "limit", 234.96), ("AAPL", "sell", 1, "limit", 223.96)]
    assert all(m.team_id == "butter_chicken_pasta_trader_1" for m in sent)
    # the trading loop's next tick must not answer the same flash again
    assert bot.strategy.generate_signal(FakeMarket(), bot.portfolio) is None


def test_sdk_no_flash_order_while_the_session_is_closed():
    bot, sent = sdk_bot()
    bot._session_open = False
    event(bot, "FLASH", {"symbol": "TSLA", "pct": -0.02, "start_price": 240.0})
    assert sent == []


def test_sdk_session_closed_sends_no_orders():
    bot, sent = sdk_bot()
    bot._session_open = True
    bot.portfolio.positions.update({"AAPL": 35, "MSFT": 39, "ORCA": 158})
    event(bot, "SESSION_CLOSED")
    assert sent == [] and bot._session_open is False


def test_sdk_mid_session_join_reads_the_scenario_limit():
    bot, _ = sdk_bot()
    event(bot, "SESSION_OPEN", {"late_join": True,
                                "scenario": {"week": 6, "position_limit": 1000}})
    assert bot._owner.position_limit == 1000


def test_sdk_portfolio_starts_unsynced_and_syncs_from_the_leaderboard(monkeypatch):
    monkeypatch.setattr(tt._engine_config, "TEAM_ID",
                        "butter_chicken_pasta_trader_1")
    bot, _ = sdk_bot()
    assert bot.portfolio.synced is False
    bot._on_leaderboard(leaderboard(
        [{"team_id": "butter_chicken_pasta_trader_1", "cash": 248_000.0,
          "positions": {"MSFT": 39, "ORCA": 158}},
         {"team_id": "butter_chicken_pasta_trader_2", "cash": 1.0,
          "positions": {"MSFT": 41}},
         {"team_id": "someone_else_trader_1", "positions": {"MSFT": 900}}],
        brokers=[{"team_id": "butter_chicken_pasta_broker",
                  "positions": {"MSFT": 38}}]))
    assert bot.portfolio.synced is True
    assert bot.portfolio.cash == 248_000.0
    assert bot.portfolio.positions == {"MSFT": 39, "ORCA": 158}
    assert bot.portfolio.teammates == {"MSFT": 79}   # our other seats only


def test_sdk_portfolio_update_marks_the_book_synced():
    bot, _ = sdk_bot()
    bot.portfolio.apply_server_update(PortfolioUpdate(
        team_id="x", cash=1000.0, positions={"MSFT": 3}, realized_pnl=0.0,
        unrealized_pnl=0.0, total_fees_paid=0.0, net_worth=2200.0))
    assert bot.portfolio.synced and bot.portfolio.positions == {"MSFT": 3}


def test_sdk_leaderboard_liquidation_flag_stops_the_bot(monkeypatch):
    monkeypatch.setattr(tt._engine_config, "TEAM_ID",
                        "butter_chicken_pasta_trader_1")
    bot, _ = sdk_bot()
    bot._on_leaderboard(leaderboard(
        [{"team_id": "butter_chicken_pasta_trader_1", "cash": 0.0,
          "positions": {}, "liquidated": True}]))
    assert bot._owner.liquidated is True


def test_sdk_a_malformed_leaderboard_does_not_raise():
    bot, _ = sdk_bot()
    bot._on_leaderboard(leaderboard([{"team_id": "trader_alpha",
                                      "positions": "oops", "cash": "n/a"}]))


def test_sdk_reconnect_waits_for_a_fresh_portfolio():
    bot, _ = sdk_bot()
    bot.portfolio.synced = True
    bot._owner.tick_count = 500
    bot._owner.on_connect()
    assert bot._owner.connect_tick == 500 and bot._owner.active_tick is None


def test_sdk_full_block_timeline(monkeypatch):
    """A whole week-6 block through the real adapter, order by order."""
    monkeypatch.setattr(tt._engine_config, "TEAM_ID",
                        "butter_chicken_pasta_trader_1")
    monkeypatch.setenv("TEAM_ID", "butter_chicken_pasta_trader_1")
    bot, sent = sdk_bot()
    owner, market = bot._owner, FakeMarket()
    bot._on_leaderboard(leaderboard(
        [{"team_id": "butter_chicken_pasta_trader_1", "cash": 248_000.0,
          "positions": {s: 35 for s in TRADABLE} | {"ORCA": 158}}]))

    def run(n):
        """What the engine's trading loop does, n times."""
        orders = []
        for _ in range(n):
            if not bot._session_open:
                continue
            signal = bot.strategy.generate_signal(market, bot.portfolio)
            if signal is not None:
                orders.append(signal)
                bot.portfolio.positions[signal.symbol] = (
                    bot.portfolio.positions.get(signal.symbol, 0) + 1)
        return orders

    event(bot, "WEEK_CHANGED", {"week": 6, "position_limit": 1000})
    assert run(50) == []                                    # market closed
    event(bot, "SESSION_PREOPEN", {"ticks": 12})
    assert run(30) == []                                    # opening auction
    event(bot, "AUCTION_RESULT", {"phase": "open", "symbols": {}})
    event(bot, "SESSION_OPEN", {"symbols": []})
    seeds = run(SEED_TICKS)
    assert sorted(o.symbol for o in seeds) == sorted(TRADABLE)

    event(bot, "IPO_OPEN", {"symbol": "VLTA", "offer_range": [40.0, 50.0],
                            "shares": 5000})
    assert [type(m) for m in sent] == [IPOSubscribe]

    event(bot, "CALENDAR_EVENT", {"kind": "earnings", "symbol": "INTC",
                                  "pct": 0.06, "affected": ["INTC"], "tick": 250})
    assert [o.symbol for o in run(2)] == ["INTC"]

    event(bot, "CALENDAR_EVENT", {"kind": "earnings", "symbol": "MSFT",
                                  "pct": -0.065, "affected": ["MSFT"], "tick": 600})
    event(bot, "TRADING_HALT", {"symbol": "MSFT", "reason": "velocity",
                                "duration": 300, "halt_num": 1})
    halted = run(tt.HEARTBEAT_TICKS * 3)
    assert halted and "MSFT" not in {o.symbol for o in halted}
    event(bot, "TRADING_RESUMED", {"symbol": "MSFT"})
    assert "MSFT" in {o.symbol for o in run(tt.RESUME_SETTLE_TICKS + 3)}

    event(bot, "SESSION_PRECLOSE", {"ticks": 10})
    assert run(tt.HEARTBEAT_TICKS * 2) == []                # closing auction
    event(bot, "AUCTION_RESULT", {"phase": "close", "symbols": {}})
    event(bot, "SESSION_CLOSED")
    assert run(100) == []
    assert [type(m) for m in sent] == [IPOSubscribe]        # no flatten orders

    seat_cap, _ = owner._caps()
    assert all(bot.portfolio.positions[s] <= seat_cap for s in TRADABLE)
    assert not owner.liquidated


# ── the passive broker seat ─────────────────────────────────────────────

def test_passive_broker_never_sends_an_order():
    broker = PassiveBroker()
    sent = []

    async def capture(msg):
        sent.append(msg)

    broker._send = capture
    broker._session_open = True
    broker.state.yahoo_prices.update({"AAPL": 220.0})
    broker.state.exchange_prices.update({"AAPL": 220.0})

    async def scenario():
        broker.start_yahoo_feeds()
        for symbol in ("AAPL", "MSFT"):
            await broker.quote_symbol(symbol)
        await broker._on_session_event(SessionEvent(
            event="TRADING_HALT", message="halt", data={"symbol": "AAPL"}))
        await broker._on_session_event(SessionEvent(
            event="SESSION_CLOSED", message="closed", data={}))

    broker.state.positions.update({"AAPL": 35, "MSFT": 38})
    asyncio.run(scenario())
    assert [m for m in sent if isinstance(m, PlaceOrder)] == []
    assert sent == []
