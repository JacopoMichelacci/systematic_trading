# Week 7 live block — seat logs (Fri Nov 13, 2026, 15:30–16:30 CST)

Scenario: *Execution in a fast market* (0.2 s snapshots, unannounced flashes,
quota of 8 messages per tick per team and venue, cancellation fees, house
taker fee 200 bp). Phase 4, Part A.

One log per seat per venue, 20 files: the four seats
(`trader_1`, `trader_2`, `trader_3`, `broker`) on the house venue
(`<bot id>.log`) and on the four team venues (`<bot id>@<port>.log`:
8767 STT_11, 8769 2026Autumn 3, 8770 Group 8, 8773 3-sigma).
Code: `team/trader.py` and `team/run_block.py` at commit `bd67ebd`, started
with `python -m team.run_block --start-at 15:15 --stop-at 16:35`.

Each file also holds a 45-second connection check from 12:04 the same day
(no orders). The block itself is everything from the `Connected to` line at
15:20:42 onward.

## Results, from these logs

| Venue | Flashes | Answered by our 4 seats | Acknowledged | Rejections |
|---|---|---|---|---|
| House (8765) | 9 | 36 of 36 | 36 | 0 |
| 8767 STT_11 | 17 | 68 of 68 | 68 | 0 |
| 8769 2026Autumn 3 | 16 | 64 of 64 | 64 | 0 |
| 8770 Group 8 | 17 | 68 of 68 | 68 | 0 |
| 8773 3-sigma | 17 | 68 of 68 | 68 | 0 |
| **Total** | **76** | **304 of 304** | **304** | **0** |

| Graded item | What the logs show |
|---|---|
| Connected ≥ 80%, before 15:40 | All 20 seats connected at 15:20:42 and stayed on that one connection until stopped at 16:35; no reconnects |
| Quota rejections < 5% | 0 `Exchange error` lines of any kind, out of 304 messages sent |
| Order on the flashed symbol within 5 s | Every flash on every venue; the venue's `Ack:` follows the `FLASH ORDER` line within 1 s for 288 of 304 orders and within 3 s for all of them (log timestamps have 1 s resolution) |
| O(1) hot path | Every flash answered in the second it arrived on the 0.2 s feed; see `tests/test_team_trader.py::test_hot_path_is_constant_time_and_never_reads_the_history` |
| Cancellation-fee bill | $0: the bot never sends a cancel |
| Purchase window 2 | Nothing bought |

Other facts: 96 of the 304 one-share orders filled, each at its limit, about
2% away from the pre-flash fair value. No liquidity-taking orders were sent
($0 in taker fees). Nothing was sent in the opening or closing auction, or
into a halted symbol (TSLA, AMD and INTC were halted at times).

## How to read a log

    EVENT FLASH — …            the venue's flash event, as received
    FLASH ORDER BUY 1 MSFT @ … our answer, sent from the event handler
    Placed: / Ack:             the order leaving, and the venue accepting it
    Fill: / FILL …             the order filling (only some do)
    HALT: … / RESUMED: …       a halt; no orders in that symbol meanwhile

Counts used above:

    grep -c "EVENT FLASH"    <file>     # flashes seen
    grep -c "FLASH ORDER"    <file>     # flashes answered
    grep -c "Ack:"           <file>     # orders accepted
    grep -c "Exchange error" <file>     # rejections
