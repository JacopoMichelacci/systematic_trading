# Where each box in the diagram lives

We drew `class_diagram.png` and `message_flow.png` in draw.io (sources are the `.drawio` files here).

| Box | File |
|---|---|
| `Trader`, `Broker`, `Exchange` | `arena/trader.py`, `arena/broker.py`, `arena/exchange.py` |
| `MyTrader`, `MyBroker` (our subclasses) | `team/trader.py`, `team/broker.py` |
| `TraderBot`, `MarketData`, `Portfolio` | `engine/trader/trader.py` |
| `Signal` and the wire messages | `engine/shared/messages.py` |

Only `Trader` is actually an abstract class. `Broker` and `Exchange` are normal base classes where every hook already has a default.
