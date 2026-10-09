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
