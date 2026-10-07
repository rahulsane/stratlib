# Research backtests (ground rules)

## Reading and publishing reports

The screening website's **Reports** tab lists the saved research as journal
posts. Major experiments have separate posts; minor variations share a post.
The original Markdown findings remain the source of truth. Engine-backed
posts also offer period comparisons, a variation inspector, saved equity
curves where available, and result/trade downloads. Reading reports makes no
FMP requests and does not run the research scripts.

`research/reports.json` is the publication catalog. Each entry has a permanent
`slug`, `title`, `strategy`, `summary`, and `documents`, a list of objects with
`path` and `title`. Paths are relative to `research/output`. Optional `runs`
patterns select output directories containing engine `results.json` files.
New runs matching an existing pattern appear inside that experiment. Add a
new catalog entry for a new research question, rather than publishing each
parameter variation separately. Optional `files` lists additional downloads;
`note` adds context such as corrections or exploratory status.

For example, `?page=Reports&report=breakout-entry-rules` links directly to the
entry study. Slugs should remain stable when a title changes. Updated dates
come from the source files' modification times in UTC. Copying or restoring
the files may change those dates; they are not experiment execution dates.

Every published document states the rules and assumptions of its runs, so a
reader who has not seen the scripts can tell what each one did: the setup,
entry, stop, exit, sizing, universe, costs, periods and data limits, and what
each variation's code or label means. Script-written reports open with a
`## Rules and assumptions` section built from `report_rules.py`, which holds
the wording several reports share (the ground rules, the table terms, the
breakout, episodic-pivot and VCP setups, the exit codes and market filters, and
the Traveling Trader checklist). Each script adds its own part in a
`rules_section()` function. A follow-up study states its rules too, rather than
pointing to the earlier report or to a script's docstring.

The catalog does not automatically publish runs from the app's Backtest tab.
That tab remains the place to execute and inspect CANSLIM backtests. Export a
written report and add a catalog entry when a run is ready to be published.

## Engine and data

Scripts for strategy backtests under a fixed set of ground rules, kept apart
from the CANSLIM app. They read the app's database (`data/stratlib.db`) and
use its FMP client and rate limiter.

## Ground rules

| Rule | Setting |
|---|---|
| Universe | US common stocks and ETFs; as-traded close of at least $5 and 20-day average dollar volume of at least $20M on the signal day |
| Delisted securities | Included where FMP has them (see coverage below) |
| Periods | In-sample 2016–2021, out-of-sample 2022 onward, and combined 2016 onward. Each is a separate run from $100,000; positions still open at a period's end are closed there |
| Parameters | Chosen on in-sample data only (`engine.sweep`); out-of-sample runs once per test name (`output/<test>/ledger.json`) |
| Signals | Use only data available at the decision; a rule that needs the close enters at that close or later |
| Costs | Slippage 0.10% per side; 0.25% when the as-traded price is under $20. No commissions |
| Size | 0.5% of equity at risk: shares = 0.5% × equity / (entry fill − stop). At most 20% of equity and the cash available (no margin) |
| Positions | At most 10. Extra signals are ranked by 63-session return, highest first |
| Benchmark | SPY bought at the first close, with and without dividends |

Engine details:
- `run_test` and `sweep` set strategies up through `engine.set_up`. Signals
  don't depend on the test period, so an identical recent set-up is reused:
  the same factory (a class, or the same function object) and panel, with
  equal parameters and constructor state. Each period still gets a fresh
  strategy with its own copy of the set-up state; the signal arrays are
  shared read-only, so a strategy that wrote to one during a run would fail
  rather than leak into another period. A strategy whose constructor holds
  anything but plain data (an intraday source, a wrapped strategy, a scan) is
  set up every time. The two most recent set-ups are kept (a Qullamaggie
  set-up is about 1 GB). On 2026-10-01 `run_qullamaggie.py` went from 298 s
  to 54 s with byte-identical reports, results and trade lists.
- Each session: exits at the open (scheduled exits, then stops gapped
  through, filled at the open), then next-open entries; intraday stops fill at
  the stop (stops before targets); at the close, strategy exits, delisting exits
  at the last close, then close entries.
- Positions still open at the end of a period are closed at its last close
  ("end of test").
- Buy-stop entries (`entry = "stop"`): an order at a trigger price stays live
  for `order_life` sessions after the signal and fills at the trigger, or at
  the open on a gap above it. On the entry day, an up day is assumed to run
  open, low, high, close, so a low that preceded an intraday breakout does not
  stop the trade. On a down day, or after a gap entry, any low below the stop
  does. `Rules(entry_day_path="worst")` assumes the worst case instead.
- Replayed entries (`entry = "scheduled"`, `strategies/exit_replay.py`): the
  exact entries of a finished test (entry session, price, initial stop) with
  other exit rules. `Rules(allow_margin=True, max_positions=...)` lifts the
  portfolio limits so every entry can be measured on its own.
- Confirmed-at-close entries (`entry = "close_confirm"`): the first session
  that trades above the trigger decides. `confirm()` either buys at that close
  or drops the order.
- Strategies can override `fill_order()` (for example, intraday entries) and
  `entry_day_stopped()`. A strategy may also define `size(j, t_signal, t, fill, stop, equity,
  available_cash, orders_left)` returning the shares to buy (None for the risk-based default); the
  position cap and the cash limit still apply (`strategies/tt_checklist.py` uses it for equal weights).
- `intraday.py` downloads and caches FMP one-minute bars per (symbol, day). FMP
  serves some one-minute histories adjusted for later splits and some as
  traded; the basis is chosen from the opening price, so no later information
  is used. Days whose one-minute range disagrees with the daily bar are
  rejected.
- `earnings.py` caches per-symbol earnings histories (FMP `earnings?symbol=`,
  back to 2002). FMP Premium refuses `earnings-calendar` dates older than about
  five years. The dates have no time of day. Renamed tickers merged in the panel
  pass their dates to the ticker that was kept.
- `strategies/exit_replay.py` holds the exit rules (`ExitRules`) shared by the
  replay and by live strategies such as `episodic_pivot.py`.
- `strategies/minervini.py` implements the trend template and the VCP. Swing
  points come from a zigzag with a 3% reversal threshold; each swing is
  confirmed only when the reversal happens, so no later data is used.
- `chart_ep_drift.py` draws charts with matplotlib from the scratch folder
  (`%TEMP%\canslim2007\pylib`); it is not a project dependency.
- `run_test(..., periods={...})` runs custom windows, such as the last two years
  where one-minute data exists.
- Profit targets can sell part of a position (`pos.target_fraction`).
- Partial exits: `on_close` can return `("partial", reason, fraction)`. Each
  position is one trade in the CSV, with the average exit price over all
  shares, plus the date and price of the first partial sale.
- Trade returns exclude dividends. Idle cash earns nothing. Shares are
  fractional.
- Sharpe uses daily returns above the 3-month T-bill yield.
- Exposure is reported as the share of sessions with any position, and as
  the average share of equity invested.

## Data

- Prices are FMP's split-adjusted daily bars. `panel.py` restores the
  as-traded price (for the $5 floor and the $20 cost tier) by undoing the
  splits after each day. Dollar volume does not depend on the split basis.
- `prepare_etfs.py` downloads current ETFs (NYSE, Nasdaq, NYSE Arca, Cboe)
  and delisted ETFs from FMP's delisted directory. It also fetches splits for
  every ETF that ever reached the dollar-volume floor.
  FMP labels exchange-traded notes (VXX, TVIX) as ETFs, and they are included.
  A short list of liquid leveraged and volatility products that FMP's directory
  misses (`KNOWN_DELISTED`) is added where FMP has prices; XIV has none.
- `benchmarks.py --refresh` caches SPY dividends and 3-month T-bill yields.
- Delisted coverage is thin before 2021. FMP has price histories for only
  28 stocks delisted in 2019 and 9 in 2020. In-sample results are flattered by
  survivorship more than out-of-sample ones.
- `panel.py` cleans the bars before use:
  - Tickers that carry the same company's history (a rename kept under the old
    and new ticker, e.g. PSTG and P) are merged, keeping the ticker whose
    record runs latest.
  - One-day spikes that reverse the next day are dropped, as are stretches
    where two series are interleaved under one ticker (QNC in 2014–2017).
    Garbage final bars of delisted stocks are dropped too.
  - A ticker whose record continues as another, penny-priced series after an
    acquisition (CBI, VVC, ABCO) ends at its last real bar.
  - Impossible lows and highs are clipped to the bar's body.
  - The counts are in each report's Data section.
- The daily app backfill (`stratlib backfill`) updates stocks only. Re-run
  `prepare_etfs.py`, then rebuild the panel (`python research/panel.py`) to
  extend the ETFs.

## Files

| File | Purpose |
|---|---|
| `panel.py` | Builds and caches the daily panel (`cache/panel_<date>.npz`) |
| `engine.py` | Simulation, metrics, three-period runner, report |
| `report_rules.py` | Shared wording for the reports' *Rules and assumptions* sections |
| `strategies/` | One module per strategy |
| `output/<test>/` | `report.md`, `results.json`, `trades_<period>.csv`, `ledger.json` |
| `test_engine.py` | Engine mechanics on synthetic prices |

The simulation core, the panel builder, the strategy classes and the market
filters live in the app's package, `src/stratlib/sim`, so the app's comparable
backtests (`stratlib strategy-backtest`) run the same code. `engine.py`,
`panel.py`, `market_filters.py` and `strategies/*.py` here re-export them, so
the scripts and their imports work as before; `engine.py` keeps the runner and
the report, and `panel.py` the cache.

The trade CSVs give prices on the split-adjusted basis (as most charts show
them), with slippage included, plus the as-traded prices.
