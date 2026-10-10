# HW1 — UML and our first bot

**Team:** butter_chicken_pasta 

Saatvik Rao
Jacopo Michelacci
Yixiao Zhang
Giandomenico Porfidia

**Repo:** [https://github.com/JacopoMichelacci/systematic_trading](https://github.com/JacopoMichelacci/systematic_trading)
**Files:** UML in `docs/uml/`, bot in `team/trader.py`

## Design justification

`Trader` is an abstract class with one abstract method, `on_tick`, instead of a function we register. The main thing this gives us is that mistakes show up early. If we forget to write `on_tick`, Python refuses to even create the bot (`MyTrader()` raises a `TypeError`), so we find out before connecting rather than halfway through a live session. 

It also means we get a lot for free. `run()` handles the connection, the handshake and sending orders, and the other hooks (`on_fill`, `on_event`, `on_ipo`) already do nothing by default, so we only override the ones we care about. 

Having a class also gives our bot a natural place to keep state between ticks. Ours remembers whether it has already been filled. 

The downside is that we don't control the main loop. The base class calls our code, there are a couple of hidden adapter layers in between, and running two strategies at once means writing two classes instead of just combining two functions. 

One small thing we noticed while drawing the diagram: only `Trader` is a real ABC. `Broker` and `Exchange` are normal base classes with defaults for everything.

## The bot

Our bot buys 1 share of MSFT at the best ask, checking `portfolio.can_buy(...)` first, and then returns `None` for the rest of the session. If the order doesn't fill within 30 ticks it tries again, but at most 3 times. We picked MSFT over AAPL because AAPL had an earnings shock scheduled for Session 2. We overrode `on_fill` and `on_event` so they write what happened to the terminal and to a log file.

## Log excerpt

From the live class session on Oct 9 (`make trader BOT=butter_chicken_pasta_trader_1`):

```
15:48:01 INFO    trader.trader: Connected to wss://feed.algoarena-st.duckdns.org as butter_chicken_pasta_trader_1 (trader)
15:48:01 INFO    team.trader: EVENT FEE_SCHEDULE — Venue fee schedule: taker 30.0 bps, maker rebate 5.0 bps
15:48:01 INFO    trader.trader: Session event: SESSION_OPEN — Session already open — you joined mid-session.
15:48:02 INFO    team.trader: Level 1 order 1/3: BUY 1 MSFT @ 408.38 (best ask)
15:48:02 DEBUG   trader.trader: Ack: buy 1 MSFT @ 408.3800
15:48:02 INFO    trader.trader: Fill: bought 1 MSFT @ 408.3800 (fee=0.6126)
15:48:02 INFO    team.trader: FILL BUY 1 MSFT @ 408.38
```

The bot connects, our `on_event` logs the fee schedule, the order goes out, and the exchange acks it (that's Level 1). It fills, and our `on_fill` logs the fill. After that the bot just sits out.

## Use of AI
- Understand problem statements, algo arena, setup, etc.
- Improve the write up language and code readability
- Review the code for any potential bugs

It wasn't always right. It told us the bot would sell all positions at the session close, but our log shows those orders were rejected because the session had already closed, and it initially misread the trading fee. We caught both by checking the actual logs, so we treated its answers as a starting point, not the final word.