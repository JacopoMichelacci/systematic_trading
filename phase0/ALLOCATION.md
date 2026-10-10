# Capital allocation — butter_chicken_pasta

## The split

| Seat | Bot id | Capital |
|---|---|---:|
| Exchange licence | not bought | $0 |
| Broker desk | `butter_chicken_pasta_broker` | $250,000 |
| Trader seat 1 | `butter_chicken_pasta_trader_1` | $250,000 |
| Trader seat 2 | `butter_chicken_pasta_trader_2` | $250,000 |
| Trader seat 3 | `butter_chicken_pasta_trader_3` | $250,000 |
| **Total** | | **$1,000,000** |

Nothing is left unallocated, since unspent budget is lost at registration.

Some numbers we looked at when deciding:

- **Seat minimums:** a broker desk needs at least $100k and a trader seat at least $50k, so every seat is well above its minimum.
- **Exchange licence:** $300k, and that money is a fee. It never becomes trading cash, so buying it would have left only $700k for the bots.
- **Position limits:** these grow from 200 shares per symbol in week 1 to 1,000 by week 6. A full 400-share position in MSFT (week 2) is about $163k, which fits in a $250k seat but not in a $50k one.
- **Broker margin:** a broker can borrow against half the value of the shares it holds, so $250k of cash can carry roughly $500k of inventory.
- **Liquidation:** a seat is force-closed if it drops below 10% of its starting capital, which is a $25k floor for each of our seats.

## Why we chose this

**No exchange.** There were already ten venues on the arena when we registered, so we would have been fighting for order flow from day one, and fees can't go below a 2 bps floor, so undercutting doesn't get you far. Routing between venues also only starts in week 8. $300k of fees for something that earns very little for most of the season didn't seem worth it. We can still buy a licence in the week 4 or week 7 window if it starts to look attractive.

**A few well-funded seats instead of many small ones.** Small seats can't hold a full position in the bigger stocks once the limits go up, and they sit much closer to the liquidation floor, so one bad drawdown hurts more. Four $250k seats can each trade any name properly and still keep a cash cushion.

**Three traders and one broker.** We want to try several trading strategies side by side, and three trader seats let us run different ones at the same time instead of putting everything on one idea. One broker desk is enough to learn market making without taking on double the inventory risk while we're still learning.

**Who runs what.** For Session 2 all three traders ran the same simple long-only Level 1 bot. From here on, Jacopo and Giandomenico will build the broker's market-making strategy, and Saatvik and Yixiao will build different strategies for the three trader seats.

**Why the broker hasn't run yet.** We decided not to switch the broker on until it has inventory management. The starter broker quotes both sides with no inventory skew, so if traders keep selling to it, it just keeps buying, and in a week with an earnings shock that's an easy way to lose money. Once Jacopo and Giandomenico have the skew working, the broker goes live.
