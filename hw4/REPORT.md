# HW4 — Tapes in SQL and Parquet, and a backtest you can trust

Team: `butter_chicken_pasta` (trader ids `butter_chicken_pasta_trader_1/2/3`) · Members: Giando, Jacopo, Saatvik, Yixiao
Tapes: Session 2 (`session_week02_20261009_204000.jsonl.gz`, 264,958,268 bytes unzipped, 145,844 lines) and Session 4, the Oct 23 live
block (`session_week04_20261023_204025_market.jsonl.gz`, 19,630,381 bytes unzipped, 40,621 lines — the market tape: no order_ack or
leaderboard messages).

Rebuild everything from `hw4/` with `make all`; one command per store: `make sqlite`, `make parquet` (tapes in `hw4/sessions/`, not submitted).
Versions: Python 3.11 (the repo's uv environment), duckdb 2.0.0, pyarrow 26.0.0, SQLite from the standard library. Machine: Apple MacBook (macOS).
All timings are best of 5 runs on that machine (first run in brackets); sizes, row counts and query answers are exact.

Cross-check against the course's own numbers for the Session 2 tape: our loaders produce 55,375 book rows, 862,067 depth rows, 4,511
trades, 34,881 acks and 7 session events — identical to the lab's `sql_baseline.py` — and the AAPL t400 window (+2.69 / +7.74 / +8.64 %)
equals the lab's `queries.sql q1`.

---

## 1. Store it twice

**SQLite schema** (`load_sqlite.py`): one table per message type we use — `book` (top of book, one row per snapshot), `book_level`
(the depth, normalised: one row per price level, foreign key to `book`), `trade`, `order_ack`, `session_event`. Every table keeps both
clocks (`tick` = the venue's logical counter, `t` = scenario tick with `t400` = the AAPL print, `ts` = the wall clock when the line was
written) and the recording order `seq`. Primary key `(week, seq)` on every table, so loading a second week is a new load that cannot
collide with or edit an old one; re-running a week deletes and reloads only that week's rows. `t0` is derived from the tape itself (the
CALENDAR announcement in scenario ticks vs the CALENDAR_EVENT that fired): 824,202 for week 2, 1,541,832 for week 4.

**Indexes, and the query each one exists for.** A composite B-tree is used left to right, so the leading columns are what the WHERE
clauses filter on first:

| index | columns | query it serves |
|---|---|---|
| `ix_book_sym_tick` | (week, symbol, tick, seq) | Q2 as-of join and Q4 point lookup: "symbol S at or before tick T, last by seq" is one seek |
| `ix_book_tick` | (week, tick) | Q1 event windows: every book of one tick |
| `ix_trade_taker`, `ix_trade_maker` | (taker_id / maker_id, week, tick) | Q2 and Q4: our own fills, as taker or as maker |
| `ix_event_kind` | (event, week, tick) | Q1: every CALENDAR_EVENT |

The five indexes took 0.25 s to build and added 5 % to the file (83,177,472 → 87,101,440 bytes).

**Parquet warehouse** (`tape_to_parquet.py`): the same five tables with an explicit pyarrow schema, sorted by `(tick, seq)` so the
row-group min/max statistics let a filter on time skip row groups (pushdown), zstd-compressed, hive-partitioned `table/week=N/symbol=S/`.
Adding Session 4 created `week=4/` folders next to `week=2/`; nothing under `week=2/` was touched.

### Storage table

| week | JSONL bytes (uncompressed) | .gz on disk | lines | SQLite load s (parse + insert) | Parquet convert s (parse + write) | Parquet bytes | JSONL → Parquet | rows: book / book_level / trade / order_ack / session_event |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 2 | 264,958,268 | 54,446,912 | 145,844 | 10.2 + 2.55 (375,719 rows/s) | 8.9 + 1.07 | 3,430,788 | 77.2× | 55,375 / 862,067 / 4,511 / 34,881 / 7 (956,841) |
| 4 | 19,630,381 | 1,160,935 | 40,621 | 1.1 + 2.28 (304,581 rows/s) | 0.9 + 0.78 | 1,747,146 | 11.2× | 36,096 / 654,169 / 4,506 / 0 / 19 (694,790) |
| both | 284,588,649 | 55,607,847 | 186,465 | 16.1 | 11.7 | 5,177,934 | 55.0× | 1,651,631 rows |

| store, both weeks | bytes | vs JSONL |
|---|---:|---:|
| JSONL as recorded | 284,588,649 | 1× |
| SQLite, no indexes | 83,177,472 | 3.4× smaller |
| SQLite, 5 indexes | 87,101,440 | 3.3× smaller |
| Parquet (zstd, partitioned) | 5,177,934 | **55.0× smaller; SQLite is 16.8× Parquet** |

Notes. (1) Parsing costs more than storing: 10.2 s of `json.loads` against 2.55 s of `executemany` for week 2; one transaction per tape
gives 300–375 k rows/s. (2) Week 2's 77× is flattered by what we do not store: the leaderboard messages are 82 % of that tape's bytes and
carry no market data, so neither store keeps them; the fair comparison is the book, 25.5 MB of JSON → 1.08 MB of `book` + 0.96 MB of
`book_level` with every depth level kept. Week 4 is a market-only tape with no leaderboard, so its 11.2× is the honest ratio for market
data. (3) SQLite is 16.8× Parquet with the same rows: row pages store every value individually and uncompressed (the symbol text repeats
on every row; Parquet dictionary-encodes it and zstd-compresses each column chunk), and each index is another copy of its columns.

---

## 2. The same questions, both stores, timed

**Tie rule, stated once.** Several snapshots share one tick: on the week-4 tape GOOGL has 31 snapshots inside tick 1,542,632, and the
median tick carries 16 book snapshots. "The book AT tick T" means the last snapshot of that tick by `seq` (end of tick): the state a
tick-driven backtester sees after applying every snapshot of T (`ORDER BY tick DESC, seq DESC LIMIT 1` in SQLite, the `book_eot` view in
DuckDB). "The book BEFORE a fill" means the last snapshot with `seq <` the fill's `seq`: the state of the book when the fill printed,
which is finer than a tick.

**Directions.** Q2-backward joins each of our fills to the book *before* it (what we paid against the touch mid): it reads only the
past, so it is safe inside a backtest. Q2-forward joins to the end-of-tick book 60 ticks *later* (a markout): it reads the future, so it
belongs in this post-trade report and never in a decision.

### Query × store timings (`run_queries.py`, both weeks, best of 5; first run in brackets)

| query | rows | SQLite ms | DuckDB / Parquet ms | winner | same answer |
|---|---:|---:|---:|---|---|
| q1 event window: end-of-tick mid at t−1, +5, +15, +60 for every CALENDAR_EVENT | 3 | 0.05 (4.6) | 25.87 (35.1) | SQLite 478× | True |
| q2 as-of **backward**: the book just before each of our fills | 1 | 1.65 (15.4) | 28.24 (28.2) | SQLite 17× | True |
| q2 as-of **forward**: the end-of-tick book 60 ticks after each fill (markout) | 1 | 1.72 (6.9) | 34.59 (34.6) | SQLite 20× | True |
| q3 full scan: spread (bp) and touch depth by symbol, both weeks | 12 | 76.41 (98.2) | **12.40** (15.7) | **DuckDB 6.2×** | True |
| q4 point lookup: AAPL's book at tick 824,602 (t400, week 2) | 1 | 0.02 (0.2) | 8.65 (9.4) | SQLite 523× | True |
| q4 point lookup: our AAPL position at that tick | 1 | 0.57 (1.5) | 7.09 (8.4) | SQLite 12× | True |

### Outputs (identical in both stores)

**q1 — event windows** (`announced_pct` is the fraction recorded on the tape: 0.0620 = +6.2 %; the market-wide econ prints name no
symbol and are excluded):

| week | kind | symbol | t | announced | mid before (t−1) | ret +5 | ret +15 | ret +60 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 2 | earnings | AAPL | 400 | +6.20 % | 194.035 | +2.69 % | +7.74 % | +8.64 % |
| 4 | earnings | AMZN | 350 | −7.91 % | 177.165 | −6.64 % | −6.51 % | −5.84 % |
| 4 | earnings | GOOGL | 800 | −5.66 % | 173.945 | −4.77 % | −3.30 % | −1.41 % |

AAPL overshot its designed +6.2 % (+8.64 % at sixty ticks); AMZN had priced 84 % of its −7.9 % within five ticks; GOOGL mean-reverted
from −4.8 % to −1.4 %.

**q2 — our 9 fills across both weeks (3 in week 2, 6 in week 4), all as taker.** Backward: we paid **+0.419 bp** over the touch mid
prevailing just before the fill, **88.9 %** of fills at or through the touch. Forward: markout at +60 ticks **−3.577 bp**, **−$1.31** in
total — small because we traded 9 single shares.

**q3 — full scan, 91,471 snapshots:**

| symbol | snapshots | spread (bp) | touch depth | symbol | snapshots | spread (bp) | touch depth |
|---|---:|---:|---:|---|---:|---:|---:|
| AAPL | 8,220 | 7.56 | 21.5 | META | 8,102 | 4.23 | 24.0 |
| AMD | 7,926 | 10.23 | 18.2 | MSFT | 7,997 | 5.35 | 19.8 |
| AMZN | 8,273 | 12.52 | 19.4 | NFLX | 7,811 | 19.15 | 22.0 |
| ARENA10 | 7,305 | — | — | NVDA | 8,196 | 9.36 | 20.4 |
| GOOGL | 8,389 | 10.94 | 20.8 | ORCA | 2,538 | 12.50 | 23.8 |
| INTC | 8,565 | 15.63 | 27.1 | TSLA | 8,149 | 6.64 | 23.7 |

(ARENA10 never had a two-sided quote; ORCA exists only after its week-4 IPO listed at t550.)

**q4 — point lookups.** AAPL's book at tick 824,602 (t400, the print): `seq 21044, best_bid 198.17, best_ask None, mid 194.11`. The
asks had vanished inside tick 400 — the venue's `mid_price` is the last value it computed — which is the lab's observation that filling
"on the book you decided on" at t400 was worse than filling on the next tick's book. Our AAPL position at that tick: 0 (our three week-2
fills were not in AAPL).

### Which store won which query, and why

SQLite won five of the six questions and DuckDB over Parquet won the one that reads a lot of rows, and the reasons are the two storage
layouts. The point lookups (q4) went to SQLite by 523× (0.02 ms vs 8.65 ms): the B-tree on (week, symbol, tick, seq) walks a few pages
to the one row, while Parquet has no index, only min/max statistics per row group, so DuckDB has to open the partition's file, read the
footer and decompress a whole row group to return one row — a fixed cost of 7–9 ms that shows on every single-row question (q4 position:
0.57 vs 7.09 ms). The event window (q1) is 3 events × 4 end-of-tick lookups, twelve B-tree seeks in 0.05 ms, whereas DuckDB first
materialises `book_eot` with a window function over all 91,471 snapshots and only then joins, 25.87 ms. The as-of joins (q2) went to
SQLite by 17–20× for the same reason: we have only 9 fills, so the correlated sub-query is 9 seeks (1.65 ms), while `ASOF JOIN` sorts and
merges the entire right side regardless of how few fills are on the left (28–35 ms); with thousands of fills the lab measured the
opposite (21 ms SQLite vs 6.8 ms DuckDB for 4,391 fills), because the sub-query cost grows with the fills and the merge does not. The one
scan (q3) went to DuckDB by 6.2× (12.40 vs 76.41 ms): it reads four of sixteen columns over 91,471 rows, binary and zstd-compressed, and
aggregates them vectorised, while SQLite reads every row page in full and its B-tree cannot help a query that touches the whole table.
The same layout difference is why the Parquet warehouse holds these rows in 16.8× fewer bytes. Rows plus a B-tree to find a record;
columns to read many rows — and on a two-session tape of 91 k snapshots the fixed cost of opening Parquet is still a visible share of
every DuckDB time, so SQLite also wins anything small.

---

## 3. A backtest you can trust

Engine (`backtest.py`): event-driven on the logical clock. Once per scenario tick t: apply the snapshots of t in seq order; fill the
orders due at t on the first book of their symbol in t (latency 1 tick by default); deliver the events of t to `on_event(ev, now=t)`; ask
the strategy for decisions with `on_tick(now=t, market, pf)` — it sees only data with tick ≤ t and is told the time; mark to the mid.
Costs: every fill is a taker fill at the venue's 0.30 %; fills sweep the recorded depth (`--fill touch` takes the best level only).
Capital $250,000, **positions start flat** (our seats held nothing at the open of Session 4 — the only end position on the tape is the 6
MSFT we bought), long-only, position cap 400. IS/OOS split chosen before looking: the midpoint of each tape, **t < 1407 / t ≥ 1407 on
week 2, t < 1237 / t ≥ 1237 on week 4**. All five strategies run under identical settings, so the baselines are comparable to ours.

**Our strategy: `team/trader.py`, class `MyTrader`, run unchanged through `ArenaAdapter`.** It is the bot we ran live on Oct 23,
buy-only. (1) Ten ticks after the first tick it sends one guaranteed 1-share marketable buy, MSFT first (`post_cross_buy`). (2) On a
`CALENDAR_EVENT` of kind `earnings` for AMZN or GOOGL it queues the symbol and buys 1 share at the first valid ask, dropping the event
after 600 ticks without a fillable ask (`handle_shock`). (3) A buy-only MSFT 5/20 moving-average cross, at most 3 orders
(`momentum_strategy`). Every order passes `_safe_buy`: a finite ask, an internal cap of 20 shares per symbol, cash with a 1 % fee buffer,
and the SDK's `can_buy`. It never sells, so its P&L is the mark-to-mid of a few single shares; the point of replaying it is the fills.
Second strategy, for the sensitivity and determinism runs: the lab's `event_ride` (trade the CALENDAR_EVENT message: on good news buy
100 up to pre-mid × (1 + pct/2) and sell 15 ticks later; on bad news sell what we hold and buy back 15 ticks later; skip events whose move
is below two taker fees; ignore market-wide events that name no symbol).
Baselines: do nothing (flat, so exactly 0), buy-and-hold (90 % of cash spread equally at the first tick), tick momentum (long 100 when
the mid is above its 20-tick mean, flat otherwise).

### Results, week 2 (2,740 ticks, 45,935 snapshots, 2 events; loaded from Parquet in 2.3 s)

| strategy | P&L | IS (t<1407) | OOS (t≥1407) | max DD | fills | fees | digest |
|---|---:|---:|---:|---:|---:|---:|---|
| mine (MyTrader) | −27 | −29 | +2 | 60 | 4 | 5 | c5b3829ffd0a |
| event_ride | +107 | +107 | 0 | 299 | 2 | 43 | d1eff9653109 |
| do_nothing | 0 | 0 | 0 | 0 | 0 | 0 | 97d170e1550e |
| buy_hold | +1,049 | +198 | +851 | 3,002 | 8 | 316 | 97d9b2f2cb12 |
| momentum | −119,902 | −78,648 | −41,255 | 119,903 | 1,015 | 59,920 | 0b3fcb404312 |

Sharpe is `mean / std(ddof=1)` of the per-tick equity changes (sample standard deviation, n−1; `np.std` would be ddof=0), **not
annualised**: one 50-minute block is not a year. `× sqrt(N)` is over the block only.

| strategy | mean $/tick | std ddof=1 | std ddof=0 | Sharpe/tick | × sqrt(N) |
|---|---:|---:|---:|---:|---:|
| mine | −0.0097 | 0.430 | 0.430 | −0.0227 | −1.19 |
| event_ride | +0.0390 | 8.921 | 8.919 | +0.0044 | +0.23 |
| do_nothing | 0 | 0 | 0 | 0 (flat equity: no risk, undefined → 0) | 0 |
| buy_hold | +0.3831 | 23.901 | 23.897 | +0.0160 | +0.84 |
| momentum | −43.7759 | 191.982 | 191.947 | −0.2280 | −11.93 |

Our bot made 4 fills, all 1 share of MSFT: the post-cross buy at t12 (407.97) and three momentum buys at t78, t160 and t214
(408.38, 408.43, 408.43). It never sells, so its −$27 is $5 of fees plus the mark-to-mid of 4 shares bought at the ask; the bot
ignored the week-2 AAPL print because AAPL is not one of its EVENT_SYMBOLS (AMZN, GOOGL).
`event_ride`'s two fills are the whole story of the week: it bought 36 AAPL at 197.18 at t401 (the tick after the print; its price cap
pre-mid × 1.031 = 200.05 stopped the sweep at 36 of the 100 shares it wanted) and sold them at 201.34 at t416 — +$150 gross, $43 of fees,
+$107 net, all in the IS half because both week-2 events (t400, t900) fall before t1407, so its OOS P&L is exactly 0: the edge lives only
where the message is. Buy-and-hold made +$1,049 by owning a market that rose after t400, with a $3,002 drawdown, ten times event_ride's.
Momentum paid 30 bp a fill 1,015 times: $59,920 of fees on a −$119,902 result, the venue's charge made visible by the fee column.

### Results, week 4 (the Oct 23 block; 2,130 ticks, 33,336 snapshots, 3 events)

| strategy | P&L | IS (t<1237) | OOS (t≥1237) | max DD | fills | fees | digest |
|---|---:|---:|---:|---:|---:|---:|---|
| mine (MyTrader) | −42 | −39 | −4 | 46 | 5 | 6 | 4072e7953ced |
| event_ride | 0 | 0 | 0 | 0 | 0 | 0 | 97d170e1550e |
| do_nothing | 0 | 0 | 0 | 0 | 0 | 0 | 97d170e1550e |
| buy_hold | −1,694 | −4,026 | +2,332 | 6,499 | 10 | 604 | 3ebac9ea1bdc |
| momentum | −85,530 | −36,046 | −49,484 | 85,530 | 752 | 45,778 | 96e231ea51fd |

| strategy | mean $/tick | std ddof=1 | std ddof=0 | Sharpe/tick | × sqrt(N) |
|---|---:|---:|---:|---:|---:|
| mine | −0.0199 | 0.821 | 0.821 | −0.0242 | −1.12 |
| event_ride | 0 | 0 | 0 | 0 | 0 |
| do_nothing | 0 | 0 | 0 | 0 | 0 |
| buy_hold | −0.7958 | 75.351 | 75.333 | −0.0106 | −0.49 |
| momentum | −40.1738 | 161.671 | 161.633 | −0.2485 | −11.47 |

Our bot made 5 fills of 1 share: MSFT at t11 (post-cross, 418.43), t39, t154 and t455 (momentum, 418.54 / 418.58 / 418.65), and
**AMZN at t351 (170.16), one tick after the −7.9 % print** — the `handle_shock` buy. The GOOGL shock at t800 was detected and an order
sent at the ask it saw (169.03), but at latency 1 the first t801 ask was above that cap, so it did not fill (see the sensitivity table).
Both week-4 earnings were negative (AMZN −7.9 %, GOOGL −5.7 %) and the book starts flat, so a long-only event strategy has nothing to
sell and `event_ride` correctly did nothing: 0 fills, same digest as do_nothing. Buy-and-hold lost $4,026 in the IS half, which contains
both prints, and recovered $2,332 after; momentum lost $85,530, $45,778 of it fees.

### Deterministic: two replay speeds, one digest (event_ride, t390–480, the 91 ticks around the AAPL print)

| run | speed | P&L | fills | digest | wall s |
|---|---|---:|---:|---|---:|
| `--from-t 390 --to-t 480 --speed inf` | ∞ | +106.81 | 2 | **d1eff9653109** | 0.0 |
| `--from-t 390 --to-t 480 --speed 20` | 20× | +106.81 | 2 | **d1eff9653109** | 4.7 |
| same, `--realclock` (planted `time.time()` exit timer) | ∞ | +470.10 | 1 | d4e00b5a1a87 | 0.0 |
| same, `--realclock` | 20× | +470.10 | 1 | d4e00b5a1a87 | 4.7 |
| same, `--realclock` | 2× | +391.22 | 2 | 16e165237fdd | 46.8 |

Same digest at ∞ and 20×: pacing reads the wall clock only to decide *when* a tick is delivered, never *what* is decided. With one line
changed — the exit timer reads `time.time()` instead of the tick — the result is different (one fill instead of two: the 15.9-second exit
never arrives inside a replay that finishes in 0.0 s or 4.7 s, so the position rides to the end of the slice and "earns" +$470 instead of
+$107), and it becomes a function of replay speed: at 2× the slice takes 46.8 s, the exit fires late (2 fills, +$391, digest 16e165237fdd),
so three replay speeds gave three different results from one tape. The
bug flatters the fastest replay, which is the one a parameter search would run a thousand times.

### Sensitivity (event_ride, week 2)

| setting | P&L | fills | fees | max DD | digest |
|---|---:|---:|---:|---:|---|
| latency 0 ticks (fill on the book you decided on) | +121 | 2 | 43 | 191 | d71d7ea59ea2 |
| latency 1 tick (default) | +107 | 2 | 43 | 299 | d1eff9653109 |
| latency 2 ticks | 0 | 0 | 0 | 0 | 97d170e1550e |
| fill = touch (best level only) | +531 | 2 | 24 | 188 | 4cc1fc0d1782 |
| fill = sweep (walk the depth) | +107 | 2 | 43 | 299 | d1eff9653109 |

### Sensitivity (our bot, MyTrader)

| setting | week | P&L | fills | fees | digest |
|---|---|---:|---:|---:|---|
| latency 0 ticks | 2 | −27 | 4 | 5 | 31e017e62d96 |
| latency 1 tick (default) | 2 | −27 | 4 | 5 | c5b3829ffd0a |
| latency 2 ticks | 2 | −20 | 3 | 4 | 81600df425a6 |
| latency 0 ticks | 4 | −39 | 6 | 6 | a10de2def81f |
| latency 1 tick (default) | 4 | −42 | 5 | 6 | 4072e7953ced |
| latency 2 ticks | 4 | −38 | 6 | 6 | 4ebbe4419da7 |
| fill = touch vs sweep | 2 and 4 | identical | | | c5b3829ffd0a / 4072e7953ced |

Our bot sends 1-share marketable limits at the ask it sees, so touch and sweep fills are identical (one share never needs a second
level). Latency is where it is exposed: on week 4 the GOOGL shock order fills at latency 0 (6 fills) and at latency 2 (the ask came back
to 169.03 by t802), but not at latency 1, when the first t801 ask was above the cap. On week 2 the third momentum buy disappears at
latency 2 for the same reason. A bot that prices at the ask it saw is a bot whose fills depend on the book one tick later.

Latency is not a smooth cost for `event_ride` either: one tick of delay costs $14, two ticks cost the trade. At t402 the AAPL asks had already moved above
the strategy's price cap (pre-mid × 1.031), so the order found nothing it was allowed to take and the event was missed entirely. The fill
model matters as much as latency: taking only the touch bought fewer shares at a better average price and paid $24 of fees instead of
$43; sweeping the thin post-print depth paid up for the extra shares. Thin books make both assumptions lumpy — measure, don't assume.

---

## 4. One page: logical vs real clock (Friday Oct 23, Session 4)

**The clocks on the tape.** SESSION_OPEN at tick 1,541,832 (15:40:25 CT), SESSION_CLOSED at tick 1,544,224: 2,392 scenario ticks, of
which 2,132 carried book snapshots, in 2,985.1 s of wall clock — **1.400 s per tick** on average. One tick of wall clock took p1 0.155 s,
median 1.278 s, **p99 9.98 s, max 11.08 s**: a tick is not a second, and on Oct 23 it was far less regular than on Oct 9 (1.062 s per
tick, max 7 s in the lab's measurement). The scheduled events drifted away from the "1 tick = 1 s" timetable: AMZN earnings at t350 fired
435 s after the open (+85 s), GOOGL at t800 after 1,003 s (+203 s), the econ print at t1150 after 1,431 s (+281 s). Up to 31 GOOGL
snapshots shared one tick (1,542,632); their order is `seq`. The median tick carried 16 book snapshots across symbols (max 62).

**Live vs replay, the same `on_tick`.** Live, three seats (`_trader_1/2/3`) ran the same `MyTrader`; the replay runs one.

| | live, 3 seats (from the tape) | replay, 1 seat, latency 1 (`--week 4 --strategy mine`) | replay, latency 0 |
|---|---:|---:|---:|
| fills | 6 (all taker, all 1 share) | 5 (4 MSFT + 1 AMZN) | 6 (4 MSFT + 1 AMZN + 1 GOOGL) |
| shares | 6 | 5 | 6 |
| average price | 418.6167 | 368.87 | 335.57 |
| fees (taker, on the tape) | 7.54 | 5.53 | 6.04 |
| P&L (cash + mark at last mid − fees) | −52.76 | −42.28 | −39 |
| end position | MSFT +6 | MSFT +4, AMZN +1 | MSFT +4, AMZN +1, GOOGL +1 |
| fill digest at speed ∞ / speed 50 | — | 4072e7953ced / 4072e7953ced (equal) | a10de2def81f |

What the live fills are: each seat's guaranteed post-cross MSFT buy plus one MSFT momentum buy — two single shares per seat, 6 in
all, every one as taker, paying $7.54 of fees to end −$52.76 on a $2,512 position. What changed between replay and live, from the replay's
fill list: (1) **The replay bought AMZN at t351 (170.16), one tick after the −7.9 % print; no live seat did.** The tape shows an AMZN ask
again within a tick of the event, so `handle_shock` fires in the replay. The code that ran live had `MAX_EVENT_WAIT_TICKS = 60`, counted
in `on_tick` calls at 0.5 s, about 30 wall-seconds; the brokers were out of the AMZN book for roughly that long after the print, so every
seat's event expired before a quote came back — and on this tape one venue tick took up to 11.08 s, so 60 of our calls was far fewer than
60 ticks of market time. The code we replay has the later value (600). (2) The replay seat made three momentum buys, each live seat made
one: the SDK's `market.prices()` appends one price per *book snapshot* (up to 31 per tick on this tape, capped at a deque of 200), while
the replay appends one mid per tick, so the 5/20 crossovers fall in different places. (3) The replay fills every order in full on the
first book of t+1 at the ask the bot saw as a cap; live, orders met whatever book the venue had when they arrived after a 0.5-second loop
and a network hop, and one of the three seats also lost a fill to a higher ask (the GOOGL case above is the replay's version of that).
(4) Maker rebates and rejected orders are not on the market tape. Net: the same decision code produced 6 live fills and 5 replayed fills,
a −$52.76 vs −$42.28 P&L, and the difference is entirely in the clock the event timer was counting.

**Reaction to each event.** None of our three seats traded an event symbol after its print:

| event | symbol | announced | event t | our first fill in that symbol | ticks to react |
|---|---|---:|---:|---|---:|
| earnings | AMZN | −7.91 % | 350 | none | — |
| earnings | GOOGL | −5.66 % | 800 | none | — |
| econ_print | market-wide | −2.49 % | 1150 | none (not an EVENT_SYMBOL) | — |

The fastest bots traded inside the event tick — q1 shows AMZN already −6.6 % five ticks later — while our bot waited for a valid ask and
expired; in the replay the same code reacted in 1 tick to AMZN (fill at t351) and in 0 or 2 ticks to GOOGL depending on latency. A bot that reacts one 0.5-second `on_tick` after the message is at least one tick late, and on this tape one tick was up to 11 s.

**Every wall-clock read in our code.** `grep -nE 'time\.time|monotonic|datetime\.now|perf_counter' team/*.py engine/trader/*.py arena/*.py`:

| file:line | code | decision path or edge? |
|---|---|---|
| `team/trader.py`, `team/broker.py`, `team/config.py` | — | **no wall-clock read**: every decision is a function of `market`, `portfolio` and our own tick counter |
| `engine/trader/trader.py:214` | `#   cutoff = time.time() - window_sec` | a comment, not executed |
| `arena/*.py` | — | none |

Zero reads in a decision path, which is why the replay of `MyTrader` prints the same fill digest at speed ∞ and speed 50
(4072e7953ced, the 59.7-second paced run and the 0.0-second one). The engine
paces `trading_loop` with `await asyncio.sleep(TICK_INTERVAL_SEC)` and backs off reconnects with a constant 3 s: both decide *when* our
`on_tick` is called, never *what* it returns, and may stay. The one time-like quantity in our bot, `MAX_EVENT_WAIT_TICKS`, is counted in
calls of `on_tick`, which is wall time in disguise (0.5 s per call); the honest fix, after this analysis, is to count venue ticks from the
`tick` field of the book snapshot so that the expiry means the same thing live, in replay, and on a day when a tick takes 11 s.
