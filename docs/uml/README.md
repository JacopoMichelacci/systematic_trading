# UML — where each box lives

`class_diagram.png` (classes) and `message_flow.png` (wire messages, in time order) were drawn in draw.io.

| Box | File |
|---|---|
| `Trader` («abstract» ABC), `Broker`, `Exchange` | `arena/trader.py:25`, `arena/broker.py:25`, `arena/exchange.py:26` |
| `MyTrader`, `MyBroker` (our subclasses) | `team/trader.py:36`, `team/broker.py:18` |
| `TraderBot` (started by `Trader.run()`), `MarketData`, `Portfolio` | `engine/trader/trader.py:638`, `:100`, `:229` |
| `Signal` | `engine/shared/messages.py:344` |
| Wire messages: `Handshake`, `PlaceOrder`, `CancelOrder`, `OrderAck`, `BookSnapshot`, `TradeExecution` | `engine/shared/messages.py:50`, `:60`, `:76`, `:180`, `:253`, `:234` |

Only `Trader` is an ABC (`on_tick` is its one `@abstractmethod`); `Broker` and `Exchange` are concrete bases whose hooks all have defaults.
