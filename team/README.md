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

    python -m team.run_block              # 3 traders + the passive broker
    python -m team.run_block --no-broker  # traders only

Start it a few minutes before the block and leave it until the block has
ended; Ctrl-C stops every seat. It restarts a seat whose process dies, keeps
the Mac awake (lid open, charger in), and prints a status line per seat every
30 seconds: connected / orders / rejections.

- `trader.py` — risk-first, long-only trader (hard position caps at seat and
  team scope, halt handling, IPO bid, no orders in either auction).
- `passive_broker.py` — connects the broker seat without quoting.
- `broker.py` — the quoting market maker (not run in graded blocks yet).
- Logs: `logs/<bot id>.log`; console output in `logs/console_<bot id>.log`.

Quick health check while it runs:

    grep -c "Exchange error" logs/butter_chicken_pasta_*.log     # want 0
