# butter_chicken_pasta

Budget invested: $1,000,000

- 🏦 `butter_chicken_pasta_broker` ($250,000):

      TEAM_ID=butter_chicken_pasta_broker python -m team.broker

- 🤖 `butter_chicken_pasta_trader_1` ($250,000):

      TEAM_ID=butter_chicken_pasta_trader_1 python -m team.trader

- 🤖 `butter_chicken_pasta_trader_2` ($250,000):

      TEAM_ID=butter_chicken_pasta_trader_2 python -m team.trader

- 🤖 `butter_chicken_pasta_trader_3` ($250,000):

      TEAM_ID=butter_chicken_pasta_trader_3 python -m team.trader

## Where to write code

- `trader.py` → `MyTrader.on_tick()` — your edge
- `broker.py` → `MyBroker.spread()/skew()` — quoting and inventory
- `config.py` → your tunables

Each class derives an `arena` base that handles all plumbing —
see `from arena import Trader, Broker, Exchange`.

## Running a live block

One machine only (two processes with the same bot id fight over the
connection). From the repo root, with the virtualenv active:

    python -m team.run_block                                  # all four seats
    python -m team.run_block --start-at 15:15 --stop-at 16:35  # unattended
    python -m team.run_block --no-broker                       # traders only

Start it before the block (connect before the 15:40 open) and leave it until
the block has ended; Ctrl-C stops every seat. It restarts a seat whose process
dies, keeps the Mac awake (lid open, charger in), and prints a status line per
seat every 30 seconds: connected / orders / flashes answered / rejections.

- `trader.py` — the seat bot, run by all four seats (the broker seat too).
  - Answers every `FLASH` with one passive 1-share limit order on that symbol,
    sent from the event handler (week 7).
  - Takes liquidity (seed basket, earnings buys, heartbeat) only on a venue
    whose taker fee is at most 50 bp; on the house venue at 200 bp it does not.
  - Hard position caps at seat and team scope, halt handling, IPO bid, nothing
    in either auction, never a cancel (cancellation-fee bill: $0).
- `broker.py` — the quoting market maker (not run in graded blocks yet).
- `passive_broker.py` — connect-only broker seat (superseded by `trader.py`).
- Logs: `logs/<bot id>.log`; console output in `logs/console_<bot id>.log`.

Quick health check while it runs:

    grep -c "Exchange error" logs/butter_chicken_pasta_*.log     # want 0
    grep -c "FLASH ORDER" logs/butter_chicken_pasta_*.log        # one per flash

Engine updates from the course: `git fetch template && git checkout
template/main -- engine season scripts Makefile README.md tests/conftest.py
tests/test_risk_controls.py`, then run the tests.
