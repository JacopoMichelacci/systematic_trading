# HW4 — run from inside hw4/ :   make all
# One command per store (make sqlite / make parquet), one per task.
# NOTE: no comments on the variable lines below — make keeps trailing spaces, which broke PYTHONPATH once.

# the interpreter: the repo's uv environment (set PY=python if the venv is activated)
PY ?= uv run python
TAPES ?= sessions/session_week02_*.jsonl.gz sessions/session_week04_*.jsonl.gz
# team id, matched as a prefix: covers _trader_1, _trader_2, _trader_3
TEAM ?= butter_chicken_pasta
DB ?= hw4.sqlite
WH ?= hw4.parquet
# the week of the live block (Fri Oct 23), for the clock page
LIVE ?= 4
# your bot's source, for the wall-clock grep
CODE ?= ../team
# so backtest.py can `from team.trader import MyTrader` (ArenaAdapter) and team.trader can import arena / engine
export PYTHONPATH := ../engine:..

BT = $(PY) backtest.py --warehouse $(WH)

.PHONY: all sqlite parquet queries backtest clock clean check

all: sqlite parquet queries backtest clock

check:                                   ## does the backtester see your bot?
	$(PY) -c "from team.trader import MyTrader; print('team.trader.MyTrader import OK')"

sqlite:                                  ## Task 1a: rebuild the SQLite store from the tapes
	$(PY) load_sqlite.py $(TAPES) --db $(DB) | tee out_sqlite.md

parquet:                                 ## Task 1b: rebuild the Parquet warehouse from the tapes
	rm -rf $(WH) && $(PY) tape_to_parquet.py $(TAPES) --out $(WH) | tee out_parquet.md

queries:                                 ## Task 2: the four questions in both stores, timed
	$(PY) run_queries.py --db $(DB) --warehouse $(WH) --team $(TEAM) | tee out_queries.md

backtest: check                          ## Task 3: ours + 3 baselines on both weeks, two speeds, sensitivities, the planted bug
	$(BT) --week 2 --metrics --show-fills                                                     | tee    out_backtest.md
	$(BT) --week 4 --metrics --show-fills                                                     | tee -a out_backtest.md
	$(BT) --week 2 --strategy mine       --latency-table --fill-table                         | tee -a out_backtest.md
	$(BT) --week 4 --strategy mine       --latency-table --fill-table                         | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --latency-table --fill-table                         | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed inf                  | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed 20                   | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed inf --realclock      | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed 20  --realclock      | tee -a out_backtest.md
	$(BT) --week 2 --strategy event_ride --from-t 390 --to-t 480 --speed 2   --realclock      | tee -a out_backtest.md
	$(BT) --week 4 --strategy mine --speed inf --show-fills                                   | tee -a out_backtest.md
	$(BT) --week 4 --strategy mine --speed 50                                                 | tee -a out_backtest.md
	$(BT) --week 4 --strategy mine --latency-ticks 0 --show-fills                             | tee -a out_backtest.md

clock:                                   ## Task 4: the clock page numbers from the live block's tape
	$(PY) clock_page.py --db $(DB) --week $(LIVE) --team $(TEAM) --code $(CODE) | tee out_clock.md
	-grep -nE 'time\.time|monotonic|datetime\.now|perf_counter' ../team/*.py ../engine/trader/*.py ../arena/*.py | tee -a out_clock.md

clean:
	rm -rf $(DB) $(WH) out_*.md __pycache__
