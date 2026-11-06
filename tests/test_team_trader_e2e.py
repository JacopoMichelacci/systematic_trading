"""team/trader.py against the REAL exchange engine, in process.

The headless arena (engine/sim/arena.py) runs the same ExchangeServer the
class venue runs, one advance_tick() per simulated second. Here our seat is
plugged into it through the SDK's own code paths:

  venue -> seat   the JSON the venue sends is queued and drained by the real
                  TraderBot.listen(), so every handler runs as it does live
  seat -> venue   whatever the bot sends goes into the venue's real order
                  and IPO entry points, with all their risk checks

A week-6-shaped scenario (every feature on, opening and closing auctions,
an IPO, earnings) is played with a real halt in the middle. The assertions
are the graded requirements: zero rejections, positions inside the limit,
no liquidation, no orders into a halt and a trade after it, an IPO
allocation, and nothing sent in either auction or after the close.
"""

import asyncio
import json

import exchange.config as xconfig
import pytest
import trader.config as tconfig
from exchange import scenario as scenario_mod
from shared.messages import IPOSubscribe, PlaceOrder
from sim.arena import HeadlessArena
from sim.bots import NoiseTrader, PassiveMarketMaker, SimBot

import team.trader as tt
from team.trader import TRADABLE, MyTrader, _SeatBot

SEAT = "bcp_trader_1"
POSITION_LIMIT = 1000

WEEK6 = {
    "week": 6,
    "label": "Scenario tournament (test replica)",
    "flags": {"shorts_allowed": True, "post_only_allowed": True,
              "circuit_breakers": True, "cancellation_fees": True,
              "latency_enabled": True, "multi_venue": True,
              "futures_enabled": True, "purchase_window": True},
    "position_limit": POSITION_LIMIT,
    "maintenance_fraction": 0.4,
    "config": {"OPENING_AUCTION_TICKS": 12, "CLOSING_AUCTION": True,
               "CLOSING_AUCTION_TICKS": 10, "SSR_TRIGGER_PCT": 0.06,
               "SHORT_LOCATE_CAP": 2000, "LULD_BAND_PCT": 0.1},
    "events": [
        {"kind": "ipo", "symbol": "VLTA", "name": "Volta Systems",
         "offer_range": [40.0, 50.0], "shares": 5000,
         "window": [30, 80], "tick": 110},
        {"kind": "earnings", "tick": 60, "symbol": "INTC",
         "magnitude_range": [0.03, 0.04]},
        {"kind": "earnings", "tick": 140, "symbol": "MSFT",
         "magnitude_range": [0.03, 0.04]},
        {"kind": "dividend", "tick": 170, "symbol": "NVDA",
         "amount_per_share": 0.30},
    ],
}


class Wire:
    """Stands in for the websocket on both ends."""

    def __init__(self, seat: "SeatSim") -> None:
        self.seat = seat
        self.inbox: list[str] = []
        self.errors: list[dict] = []

    # venue -> seat (the server awaits client.send(json))
    async def send(self, data: str) -> None:
        self.inbox.append(data)
        if '"type":"error"' in data:
            self.errors.append(json.loads(data))

    # the real TraderBot.listen() does `async for raw in self._ws`
    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self.inbox:
            raise StopAsyncIteration
        return self.inbox.pop(0)


class Uplink:
    """bot._ws for the sending direction: the real _send() calls .send()."""

    def __init__(self, seat: "SeatSim") -> None:
        self.seat = seat

    def __aiter__(self):
        return self.seat.wire.__aiter__()

    async def send(self, data: str) -> None:
        raw = json.loads(data)
        server, wire, seat = self.seat.arena.server, self.seat.wire, self.seat
        if raw["type"] == "place_order":
            msg = PlaceOrder.model_validate(raw)
            seat.orders.append((server.tick, msg.symbol, msg.side,
                                msg.quantity, msg.order_type,
                                seat.phase()))
            await server._handle_place_order(wire, msg, seat.team_id)
        elif raw["type"] == "ipo_subscribe":
            msg = IPOSubscribe.model_validate(raw)
            seat.ipo_bids.append((msg.symbol, msg.quantity, msg.max_price))
            await server._handle_ipo_subscribe(wire, msg, seat.team_id)
        else:
            raise AssertionError(f"unexpected message to the venue: {raw}")


class SeatSim(SimBot):
    """Our real _SeatBot + MyTrader, driven like the live trading loop."""

    preset = "bcp_seat"
    role = "trader"

    def __init__(self, team_id: str, cash: float = 250_000.0) -> None:
        super().__init__(team_id, cash)
        self.bot = _SeatBot(MyTrader())
        self.wire = Wire(self)
        self.bot._ws = Uplink(self)
        self.orders: list[tuple] = []
        self.ipo_bids: list[tuple] = []
        self.max_position: dict[str, int] = {}

    def phase(self) -> str:
        server = self.arena.server
        if server.auction_phase:
            return server.auction_phase
        return "open" if server.session_open else "closed"

    def plug_in(self) -> None:
        """Replace the arena's SimClient with our wire (after attach)."""
        self.arena.server.clients[self.team_id] = self.wire
        self.client = self.wire
        self.clients[0] = self.wire

    async def pump(self) -> None:
        """Deliver everything the venue has sent, through the real listen()."""
        await self.bot.listen()

    async def act(self, arena) -> None:
        server = arena.server
        await self.pump()
        for symbol, book in server.books.items():
            self.bot.market.update_book(server._make_snapshot(symbol, book))
        if server.tick % 5 == 0:
            self.bot._on_leaderboard(server._build_leaderboard())
        for _ in range(2):                      # TICK_INTERVAL is 0.5 s
            if not self.bot._session_open:
                break
            signal = self.bot.strategy.generate_signal(
                self.bot.market, self.bot.portfolio)
            if signal is not None:
                await self.bot._place_order(signal)
                await self.pump()
        for symbol, qty in self.portfolio.positions.items():
            self.max_position[symbol] = max(
                self.max_position.get(symbol, 0), abs(qty))


@pytest.fixture
def week6(monkeypatch, tmp_path):
    monkeypatch.setattr(tconfig, "TEAM_ID", SEAT)
    monkeypatch.setenv("TEAM_ID", SEAT)
    # The real exchange writes an end-of-day report at the close: keep it
    # out of the repository.
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    saved = {k: getattr(xconfig, k) for k in (
        "OPENING_AUCTION_TICKS", "CLOSING_AUCTION", "CLOSING_AUCTION_TICKS",
        "LULD_BAND_PCT", "SSR_TRIGGER_PCT", "SHORT_LOCATE_CAP",
        "MAINTENANCE_FRACTION", "POSITION_LIMIT_SHARES")}
    seat = SeatSim(SEAT)
    bots = [seat]
    bots += [PassiveMarketMaker(f"mm_{i}", quote_size=40) for i in range(3)]
    bots += [NoiseTrader(f"noise_{i}", trade_prob=0.05, size=3)
             for i in range(4)]
    arena = HeadlessArena(bots, seed=7,
                          scenario=scenario_mod.from_dict(WEEK6))
    seat.plug_in()
    yield arena, seat
    arena.server.scenario.restore()
    for key, value in saved.items():
        setattr(xconfig, key, value)


def test_week6_block_against_the_real_exchange(week6):
    arena, seat = week6
    server = arena.server

    async def block():
        # The seat joins before the open, as it will on Friday.
        seat.bot._owner.on_connect()
        seat.bot._on_leaderboard(server._build_leaderboard())
        seat.bot._owner.on_scenario(server.scenario.to_dict())

        await arena.open_session()          # pre-open, cross, SESSION_OPEN
        await seat.pump()
        opened_with = len(seat.orders)

        await arena.run(45)                 # seed basket + IPO book opens
        assert server.session_open

        # A real single-name halt on a symbol we have traded.
        assert "MSFT" in {o[1] for o in seat.orders}
        await server._halt_symbol("MSFT", "test: velocity", duration=600.0)
        halt_tick = server.tick
        await arena.run(150)                # heartbeats while MSFT is halted
        assert server.circuit_breaker.is_halted("MSFT")
        resumed_tick = server.tick
        await server.lift_circuit_breakers()    # teacher: MARKET_RESUMED
        await arena.run(20)

        # And one that resumes by itself (TRADING_RESUMED).
        await server._halt_symbol("AAPL", "test: velocity", duration=0.2)
        aapl_halt_tick = server.tick
        await arena.run(10)
        await asyncio.sleep(0.4)            # the venue's own resume task
        assert not server.circuit_breaker.is_halted("AAPL")
        aapl_resumed_tick = server.tick
        await arena.run(40)

        before_close = len(seat.orders)
        await arena.close_session()         # pre-close, closing cross, close
        await seat.pump()
        await arena.run(5)
        return (opened_with, halt_tick, resumed_tick, aapl_halt_tick,
                aapl_resumed_tick, before_close)

    (opened_with, halt_tick, resumed_tick, aapl_halt_tick,
     aapl_resumed_tick, before_close) = asyncio.run(block())
    portfolio = seat.portfolio
    owner = seat.bot._owner

    # ── nothing the venue refused, of any kind ──
    assert seat.wire.errors == [], seat.wire.errors
    assert seat.errors == [], seat.errors

    # ── the bot traded, buy-only, one share, equities only ──
    assert len(seat.orders) >= len(TRADABLE) + 3
    assert {o[1] for o in seat.orders} <= set(TRADABLE)
    assert all(o[2] == "buy" and o[3] == 1 and o[4] == "limit"
               for o in seat.orders)
    assert set(TRADABLE) <= {o[1] for o in seat.orders}     # seed basket

    # ── auctions and the close: nothing sent ──
    assert opened_with == 0                                 # pre-open
    assert {o[5] for o in seat.orders} == {"open"}
    assert len(seat.orders) == before_close                 # pre-close + after

    # ── halts: silent while halted, trading again afterwards ──
    during = [o for o in seat.orders if halt_tick < o[0] <= resumed_tick]
    assert during, "other symbols must keep trading during a one-name halt"
    assert "MSFT" not in {o[1] for o in during}
    assert "MSFT" in {o[1] for o in seat.orders if o[0] > resumed_tick}
    assert "AAPL" not in {o[1] for o in seat.orders
                          if aapl_halt_tick < o[0] <= aapl_resumed_tick}
    assert "AAPL" in {o[1] for o in seat.orders if o[0] > aapl_resumed_tick}

    # ── the IPO: one indication, accepted, allocated ──
    assert seat.ipo_bids == [("VLTA", tt.IPO_SHARES, 50.0)]
    assert 0 < portfolio.positions.get("VLTA", 0) <= tt.IPO_SHARES

    # ── risk: inside the limit the whole time, never liquidated ──
    seat_cap, _ = owner._caps()
    assert owner.position_limit == POSITION_LIMIT
    assert all(seat.max_position.get(s, 0) <= seat_cap for s in TRADABLE)
    assert max(seat.max_position.values()) <= POSITION_LIMIT
    assert not portfolio.liquidated and not owner.liquidated
    assert portfolio.cash > 200_000 and min(portfolio.positions.values()) >= 0

    # ── the bot's own book agrees with the venue's ──
    assert seat.bot.portfolio.positions == {
        s: q for s, q in portfolio.positions.items()}
