# HW4 — tapes in SQLite and Parquet, timed queries, a logical-clock backtester, the clock page

Rebuild from this folder (tapes in `sessions/`, never committed):

    uv add duckdb pyarrow            # once, from the repo root
    make sqlite                      # Task 1a  -> hw4.sqlite        (one command)
    make parquet                     # Task 1b  -> hw4.parquet/      (one command)
    make queries                     # Task 2   -> out_queries.md
    make backtest                    # Task 3   -> out_backtest.md   (needs team/trader.py importable: `make check`)
    make clock                       # Task 4   -> out_clock.md
    make all                         # everything

| file | what it does |
|---|---|
| `tape.py` | one reader for both stores: parses a recording, derives the scenario tick t from the tape's own CALENDAR / CALENDAR_EVENT lines, flattens to five tables, keeps seq / tick / t / ts |
| `load_sqlite.py` | Task 1a: schema, (week, seq) primary keys, five indexes (each commented with the query it serves), sizes with and without indexes |
| `tape_to_parquet.py` | Task 1b: hive partitions by week and symbol, sorted by (tick, seq), zstd; a new week is a new folder |
| `run_queries.py` | Task 2: the four questions in both dialects, same-answer check, best-of-5 timings |
| `backtest.py` | Task 3: event-driven on the logical clock; `--strategy mine` runs `team.trader.MyTrader` unchanged through `ArenaAdapter` |
| `clock_page.py` | Task 4: tick lengths, our live fills, reaction ticks, the wall-clock grep |
| `REPORT.md` | the write-up: storage table, query x store table and paragraph, backtest tables with the two-speed digests, the clock page |
| `out_*.md` | the raw outputs the report's numbers come from |
