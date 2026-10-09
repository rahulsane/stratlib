# StratLib

A local Python application that screens US stocks with strategies such as William O'Neill's
CANSLIM method, using data from Financial Modeling Prep (FMP).

Status: **phase 6 of 6**. Optional on-demand Jev base reviews are implemented
and disabled by default. The backtest engine, CLI and Backtest page are built.
Historical runs require dated universe and statement archives that the current
cache does not contain. The app reports missing coverage before producing a
result. Phase 6 is ready for review.

## Setup

The **Reports** tab is a browsable research journal with permanent post links,
strategy filters, and grouped backtest variations. It reads the saved studies
under `research/output` without running tests or making API calls. See
[publishing reports](research/README.md#reading-and-publishing-reports) to add
an experiment or attach new variations.

Requires Python 3.11 or later.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

This installs the `stratlib` command into the virtual environment
(`.venv\Scripts\stratlib`).

## The .env file

Secrets live in `.env` in the project root. The file is listed in
`.gitignore`; never commit it.

```
FMP_API_KEY=your-fmp-key
AI_GATEWAY_API_KEY=your-vercel-ai-gateway-key   # phase 6 only
```

The FMP key is sent in the `apikey` request header, never in the URL, so it
does not appear in logs, the cache, or error messages. Log output is also
scrubbed for the key as a second safeguard.

## Configuration

`config.yaml` holds settings and every implemented threshold. Paths are relative
to the file. The Settings page edits all screening and sell-rule thresholds. The main
data settings:

| Setting | Default | Meaning |
|---|---|---|
| `fmp.calls_per_minute` | 700 | Shared cap for all scripts and the GUI together (Premium allows 750). |
| `fmp.max_workers` | 8 | Concurrent requests during a backfill. |
| `universe.exchanges` | NYSE, NASDAQ, AMEX | Exchanges in the universe. |
| `prices.history_years` | 5 | Years of daily bars to keep. Raising it downloads the older range on the next run. |
| `prices.eod_final_hour_et` | 18 | Today's bar is stored only after this hour (New York time). |

## First backfill

```powershell
.venv\Scripts\stratlib backfill
```

The first run:

1. Pulls the universe from FMP's company screener, one call per exchange,
   excluding ETFs and funds. It then drops preferreds, warrants, units,
   rights, and exchange-listed notes, recognised from symbol suffixes and
   security names. Then it keeps one listing per company. Among common
   listings with the same company name (ignoring punctuation and "Class A")
   on the same exchange, it keeps the most traded. The others become
   "secondary listing": other share classes (BRK-A beside BRK-B, PBR-A beside
   PBR) and notes that carry the parent's exact name (SOMN beside SO). This
   stops one company from counting twice in RS and industry ranks, or being
   held twice. The reason for each exclusion is stored in the `symbols` table.
2. Downloads `history_years` of split-adjusted daily bars for every common
   stock and every open position, plus ^GSPC, ^IXIC, SPY, and QQQ for market
   direction. That is one call per symbol.

It prints the number of API calls and the elapsed time at the end, and records
both in the `runs` table.

Run the same command after 6 pm New York time each trading day to extend the
history. Each symbol then costs one call, which fetches only the bars since its
last stored bar. If FMP has re-adjusted a stock's history since the last run
(usually a split), the whole history for that stock is downloaded again.
Symbols already brought up to date since the last close are skipped, so
repeat runs cost nothing.

Once a month a full backfill (without `--symbols` or `--sample`) also saves a
snapshot of analysts' consensus estimates (`src/stratlib/estimate_snapshots.py`,
about 610 calls). FMP serves only today's estimates, so these snapshots build
the point-in-time record that research on forecasts needs. Each is labelled
with the month that has just ended and records the day it was taken. It holds,
for the MSCI USA and S&P 500 members:
- FMP's annual EPS and revenue estimates and free float;
- iShares' published holdings for its MSCI USA Equal Weighted ETF (MSCI USA's
  members with GICS sectors) and its MSCI USA Quality GARP ETF (the index's
  real holdings);
- MSCI's own constituent lists with weights for both indexes.

The snapshots are saved as `research:estimates:<month>`. A failed snapshot is
logged and retried at the next backfill; it never stops the price update.
`stratlib estimates-snapshot` takes one on demand (`--force` retakes the
month's).

Options for development:

| Option | Effect |
|---|---|
| `--sample 50` | Limit the run to a fixed sample of 50 universe symbols (same symbols every time, so repeats hit the cache). |
| `--symbols AAPL,MSFT` | Run on specific tickers; skips the universe call. |
| `--skip-universe` | Reuse the stored universe instead of refreshing it. |
| `--workers N` | Override `fmp.max_workers`. |
| `-v` | Log every API call to the console, with the running count. |

`stratlib status` shows what is in the database and today's API call count
across all processes. It does not need the API key.

## Rate limiting and caching

- Every process builds its limiter on `data/ratelimit.db`, so a backfill and
  the GUI running together still stay under `calls_per_minute` combined.
  Calls are spaced evenly rather than sent in bursts.
- 429 and 5xx responses and network errors are retried with exponential
  backoff (honouring `Retry-After`). A 402 means the plan does not include the
  endpoint or symbol; it is not retried.
- Responses for the universe, statements, calendars, and reference data are
  cached in `data/stratlib.db` and served from there until they expire. Price
  history is cached as bars with per-symbol fetch state.
- Logs go to the console and to `logs/stratlib.log`. The client logs the running
  call count every 100 calls.

## GUI

The GUI is a web app built with NiceGUI: a dark trading dashboard in the manner
of TradingView. It replaced the Streamlit interface on 2026-10-01; the dated
phase notes further down that mention Streamlit describe that earlier interface.

```powershell
.venv\Scripts\stratlib web
```

Open http://127.0.0.1:8600. `--port` changes the port and `--open` opens a
browser; `stratlib gui` does the same. Pages read the local database and make no
API calls when they open. Only a few actions call out, and each says so:
- Run screen, when it refreshes fundamentals;
- Prepare data on Backtest, and the strict method's delisting refresh and
  universe capture;
- a Jev review.

The top bar switches pages and shows the market state and allowed exposure,
the market data date and today's combined API call count for all processes,
in UTC. The interface bundles Inter (SIL Open Font License) in
`src/stratlib/static/fonts`, so it renders the same offline. Its stylesheet is
`src/stratlib/web/assets/theme.css`; the design system is in `DESIGN.md`.
Wording, ordering and formatting the pages share live in
`src/stratlib/presentation.py`.

The shared **Strategy** selector carries any of the nine strategies (CANSLIM,
Trend Leaders, Qullamaggie Breakout, Minervini VCP, Episodic Pivot, Traveling
Trader 9/21 EMA, Traveling Trader Checklist, Nash Quality Screen and MSCI GARP) across
Screen, Portfolio, Market, Positions and Backtest. It appears in the address,
for example `/portfolio?strategy=trend`. Each page's **Strategy rules and
research** section explains selection, ranking, sizing and exits and links to
Backtest and Reports. Every page reads the config file again when it changes,
whether it was saved on Settings or in an editor.

- **Screen** keeps dated runs for each strategy and variant. Use the saved-run
  picker to revisit an earlier result. Trend Leaders can evaluate existing
  CANSLIM screen facts without an API call when their thresholds match; the
  original dates, sample coverage and warnings remain attached. CANSLIM's
  **Entry candidates** step also applies the configured breakout entry window.
  The other six strategies list their dated candidates with their own evidence
  (see Scan strategies below).
- **Portfolio** separates a model plan from recorded account value. Plans use
  current allocation policy and each holding's saved exit assessment. Missing,
  sampled, stale or mismatched evidence blocks proposed new entries. Model
  reductions require review; no orders are placed. Holdings and manually
  recorded cash are scoped to the selected strategy and variant.
- **Market** shows shared index observations alongside the selected strategy's
  exposure policy. Trend Leaders limits new entries without forced market
  sales; CANSLIM respects the configured cash-raising policy.
- **Positions** saves strategy, variant and a complete rule snapshot with new
  entries. Editing price or shares preserves that snapshot. To change it,
  choose another strategy or explicitly check **Assign current rules**.
  Existing database entries remain **Legacy** CANSLIM positions using current
  thresholds until explicitly assigned saved rules. Choose **All strategies**
  to review holdings together; alerts still use each position's own rules.

Shares are optional. When quantities or cash are unknown, total account value
and actual weights remain unknown. Price and share references adjust together
when the cache is restated for a split. Cash is a manual balance: adding,
closing or reopening a position does not adjust it. Displayed valuations use
cached closes and show their dates. Live alerts use closes; historical backtest
stops use intraday lows and opening gaps.

SQLite adds position tracking columns without rewriting existing entries.
Screen histories use `screen:<strategy>:<variant>:<price-date>:<run-id>`;
the old `latest_screen` document remains a CANSLIM compatibility reference.
Research reports do not automatically register their experiments as supported
live strategies: only the nine listed above have a screen, plan and alerts.

The pages in turn:

**Screen** loads the last completed result without API calls. Run screen
recalculates the market ranks and refreshes survivor fundamentals as needed;
Run options holds the development sample and the cache-only choice, and
progress and the API count show while it runs. The page has:
- the funnel (All stocks, Survivors, Pass C/A/S/L, Pass C/A/N/S/L/M, Entry
  candidates), which chooses the stocks listed;
- search, sector, minimum RS, a filter by one check's result, and ranking by RS,
  six-month change, checks passed, price or any criterion's measurement
  (multi-year EPS ranks by its weakest year, share growth by its three-year
  total and debt/equity by its latest ratio; unavailable values go last);
- a panel for the selected stock, with a candlestick chart (50- and 200-day
  averages, volume, the last close and the base's pivot) and its CANSLIM checks
  or signal fields.

J/K or the arrow keys step through the list. On a phone the panel opens as a
sheet over the list. Older Phase 2 screens still open; run the screen again to
add their N and M checks.

**Stock detail** opens from a stock's panel on Screen, or from
the tab, which opens on the stock you last looked at. It shows:
- a large chart, daily or weekly over six months, one year or three years, with
  the detected base outlined, its handle shaded and the first closing breakout
  marked;
- the stock's standing in the saved screen, and its signal when a scan strategy
  is selected;
- the base and market checks, recalculated from cached prices and current
  thresholds (saved C/A/S/L checks keep their screen's date and thresholds);
- the saved C/A/S/L checks, each opening to show its explanation, under the
  screen's own thresholds;
- the price prefilter, quarterly and annual statements, and the manual I
  assessment;
- the optional Jev base review, which asks before making its paid request.

Each stock has its own address, for example `/stock?symbol=AMD`.

**Market** opens from the market chip in the top bar. It shows:
- the market card with the allowed exposure, or, for a scan strategy, that
  strategy's own market filter;
- one tab per index with its state, distribution count and exposure;
- the selected index's chart, with distribution days marked above the bars and
  follow-through days below, over three months, six months or a year;
- the index's details, beside the selected strategy's market policy;
- the last 126 sessions' distribution and follow-through days, and the market
  rules.

**Positions**:
- **Summary:** counts of open holdings by alert, beside the exit defaults for new
  entries under the selected strategy.
- **List:** the holdings, most urgent alert first, with close, gain, shares,
  value, stop, target and entry date. Switch between the selected strategy and
  all strategies.
- **Side panel:** pick a holding to see the alert's reason and every
  measurement, open its chart, review its saved rules, edit it or mark it
  closed.
- **Add and edit:** both open a form that saves the chosen strategy's current
  rules with a new entry. An edit keeps the saved rules unless you assign
  current ones or change strategy.
- **Closed positions:** listed below, each with Reopen.

**Portfolio** opens with a **hypothetical portfolio**: what the strategy says to
buy today, from its latest saved screen.
- **Amount:** set the starting capital on the page; it defaults to the backtest's
  $100,000.
- **Market rule:** CANSLIM and Trend Leaders fill only as many slots as the
  market's allowed exposure permits and keep the rest in cash. The scan
  strategies fill their own count at the research's sizing.
- **Pricing:** positions are bought in whole shares at the latest cached close;
  buy-stop strategies (Qullamaggie, Minervini) are priced at their buy stop.
- **Scope:** it ignores recorded holdings, is rebuilt on every visit and can be
  downloaded as a CSV. Nothing is saved or ordered.

Below it, the page shows the strategy's model plan beside your recorded
holdings and cash:
- the market card (for strategies that use the exposure ladder) and the slots:
  initial slot or sizing, held symbols, open slots and candidates;
- the recorded account: holdings value, recorded cash and the total, with
  **Record cash** for a manual balance per strategy;
- the holdings with their saved-rule alerts, any model reduction to review,
  value, actual weight and gain;
- the model changes: each candidate from the saved screen marked Proposed buy,
  Held, Watch or Review data, with its sizing and evidence.

Data issues that block new entries, such as a stale or sampled screen, appear
as notices at the top. Symbols link to Stock detail.

**Settings** puts every threshold in its rule's group:
- **Layout:** a side list jumps between the groups, and each field shows its
  saved value and its default.
- **Editing:** edited fields are marked, and a notice counts the unsaved changes.
- **Buttons:** **Save thresholds** writes only the thresholds block of the config
  file. **Load defaults** fills the form without saving. **Reload saved**
  discards the edits.
- **Safety:** a save is refused if the file changed since the form was loaded,
  so an edit made elsewhere is never overwritten.
- **Scan strategy parameters:** shown read-only beneath the thresholds.

**Reports**:
- **Index:** lists every saved study with search, a strategy filter and sorting.
- **Studies:** each one opens as an article, at an address such as
  `/reports?report=minervini-vcp`. A side panel lists its sections. The
  findings are followed by a comparison of the saved variations: choose the
  test period and inspect one variation's equity curve against SPY with
  dividends.
- **Files:** the source files and each variation's results, trades and equity
  files can be downloaded.

Opening a report never runs a backtest.

**Strategies** (`/strategies`) lists the nine strategies side by side: what
each looks for, the data its signals need, its market rule, its holdings, its
latest candidate count and its comparable backtest (CAGR, the margin over SPY
with dividends and the maximum drawdown) for the period chosen above the list:
2016 to present, 2022 to present or 2016 to 2021. Picking one shows its rules,
sizing, candidates and backtest figures, and every other page then opens on it.

**Backtest** opens on the strategy's comparable backtest, the one every
strategy has on the same engine and rules (see Backtesting): a switch between
the combined, in-sample and out-of-sample periods; total return, CAGR, maximum
drawdown and Sharpe ratio against SPY with dividends; the equity curve; trade
measures and each year's return against SPY; the trades (with a CSV); and the
rules, exits and saved inputs. **Run again** reruns this strategy and **Run all**
every strategy. The older kinds of backtest follow:
- **Saved results:** the twelve most recent, compared side by side. Pick one to
  see its figures against SPY, the equity curve over the market's allowed
  exposure, the measures table, how candidates were filtered (or how the
  portfolio was held), the closed trades, open positions and skipped entries,
  and the saved rules and data coverage. Trades and the full result can be
  downloaded.
- **Run controls:** choose the approximate or strict method and, for
  approximate runs, Breakout trades, Leaders portfolio or Trend leaders. Then
  set the dates, capital, holdings, sample, label and the raise-cash rule, and
  check the data coverage or run the backtest. Progress shows while it runs.
- **Prepare data:** downloads what an approximate run needs from FMP and asks
  first, because a first preparation takes thousands of API calls.
- **Strict method:** adds the archive import, the delisting-directory refresh and
  today's universe capture.
- **Scan strategies:** for the six scan strategies the approximate and strict
  methods give way to their saved research runs, as Reports shows them. Both apps read and write the
same database. The selected strategy carries across pages and appears in the
address, for example `/?strategy=trend`.

The web app bundles Inter (SIL Open Font License) in `src/stratlib/static/fonts`.
Its stylesheet is `src/stratlib/web/assets/theme.css`, and its design record is
`DESIGN.md`. Wording, ordering and formatting shared by both
front ends live in `src/stratlib/presentation.py`.

### Scan strategies

Nine strategies are supported. Two read the C/A/S/L screen's facts (CANSLIM and
Trend Leaders). Six come from the research folder and scan the cached market with
their research rules, and MSCI GARP holds an index rebuilt from its published rules
(see MSCI GARP below):

| Strategy | Selection | Entry and exit | Research report |
|---|---|---|---|
| Qullamaggie Breakout | Large prior move, 10 to 40 sessions of tight consolidation, rising 10/20-day averages, 4% daily range | Buy stop at the pivot for 5 sessions; stop at the prior low (skipped beyond 1 daily range); a third sold on day 3, then a 10-day average trail | Qullamaggie breakout posts |
| Minervini VCP | Stage 2 trend template, relative-strength percentile 70 or more, contracting pullbacks on dry volume | Buy stop at the last swing high; breakout-day volume of 1.4x or the research sells it; stop at the pullback low (8% at most); third on day 3 and a 10-day trail, or half at +20% and a 50-day trail | Minervini VCP post |
| Episodic Pivot | 20% earnings gap on 3x volume, strong close, SPY above its 50-day average | Buy the close; stop at the day's low; third on day 3 if above entry, then a 10-day trail (or a 20-session time exit) | Episodic pivot posts (corrected) |
| Traveling Trader 9/21 EMA | 9 over 21 over 50-day average, close over the 200-day, fresh pullback to the 9-day EMA | Buy the close; stop 3% under the 21-day EMA; sell at the first close under it | 9/21 EMA pullback post |
| Traveling Trader Checklist | Trailing P/E under its own median, PEG 1 or less, ROIC 15% or more, debt/equity under 1, free cash flow rising | Top ten by 63-session return; 20% stop; quarterly rebalance | Checklist posts |
| Nash Quality Screen | More cash than debt, revenue growth 10% or more, 15% margin, revenue outgrowing expenses; ranked by Rule of 40 | Ten holdings kept while they pass; quarterly rebalance; no stop | Nash posts |

**The rules are the research rules.** `src/stratlib/setups.py` and
`fundamental_scans.py` port the signal code of `research/strategies/*.py`,
`market_filters.py`, `nash_screen.py`, `nash_screen_clean.py` and `tt_quality.py`.
`tests/test_setups_parity.py` and `tests/test_fundamental_parity.py` run both on
the same arrays and statements and require identical masks, pivots, snapshots
and checklist flags. The research filters that need a split history or ETF
universe differ in two documented ways: the live universe is current common
stocks only, and Episodic Pivot's rule dropping stocks with a 100-fold split
adjustment is not applied.

**Checked against the published trades.** On 2026-09-30 the live scan was run as
of the session before entry for samples of each strategy's saved research trades
(2023 to 2026, 25 to 30 trades each): it listed 30 of 30 Qullamaggie trades, 30 of 30 Episodic Pivot
trades, 23 of 25 Minervini trades and 18 of 25 EMA pullback trades, with stops
matching the research to within its 0.1% slippage. The misses are stocks the
research panel holds and today's listing does not (ETFs, and companies acquired
or delisted since), plus one relative-strength percentile that sits just under
the cutoff once those names are absent. Nash and the checklist have no saved
trade lists; their statement logic is covered by the parity tests.

**Screens.** Run screen applies the rules to the last completed close and saves a
dated screen under the strategy's own key, so strategies never overwrite each
other. The cached-price scans make no API calls. Episodic Pivot makes one
earnings-calendar call (without it, gap signals are listed as unverified). Nash
and the checklist read company statements from the bundles the research harness
caches (`research:nash2:stmts:<symbol>`: 80 quarters of income, balance sheet,
cash flow and key metrics). An online run refetches a bundle when its company
has reported since it was fetched, or when it is over 120 days old, at four
calls per company; a failed fetch never replaces a good bundle. Buy-stop
strategies also list orders that already triggered, setups the market filter held
back, and setups the strategy's own rules refused, each with its reason.

```powershell
.venv\Scripts\stratlib screen --strategy qullamaggie
.venv\Scripts\stratlib screen --strategy episodic_pivot
.venv\Scripts\stratlib screen --strategy nash_quality --offline
```

**Portfolio and sizing.** Scan strategies are sized as the research sized them:
each trade risks 0.5% of equity between entry and stop, capped at 20% of equity
(a third for the checklist); the checklist and Nash split equally. The exposure
ladder belongs to CANSLIM and Trend Leaders. A scan strategy carries its own
market filter inside the scan (Episodic Pivot: SPY above its 50-day average; the
others none by default; the Market page shows the selected strategy's filter).

**Positions and alerts.** A position saves the strategy's parameters and an
optional initial stop. Without a stop the alert derives the research's: the prior
session's low (Qullamaggie), the widest stop the rules allow (Minervini, because
the pattern's own low is not stored), the entry session's low (Episodic Pivot),
3% under the 21-day EMA (EMA), 20% under entry (checklist). Alerts replay the
research exits over completed closes from the entry date: a close at or below the
stop, a close under the trailing average, a scheduled partial sale (**Take
profits**) or a time exit. A Sell that fired earlier stays open until the position
is marked closed. The checklist and Nash also compare the holding with the latest
saved screen's selection: a holding outside it is sold at the quarterly rebalance
(the first session of January, April, July and October), and waits until then if
it was bought after the latest one.

**Backtest.** These strategies were tested with the research engine (2016 to
present, stocks and ETFs, risk-based sizing, slippage, SPY with dividends), not the
CANSLIM engine behind Approximate and Strict runs. The Backtest page therefore
shows their saved research runs (period comparison, equity curves, downloads) and
links the written findings. Re-run a variation with the scripts in `research/`
and publish it in `research/reports.json`.

**What the research found.** The rule sets as listed on each page trailed SPY with
dividends over 2016 to present, and most lost money: the Qullamaggie runs -3.1% and
-3.8% a year, Minervini -15.2% and -32.3% (drawdowns of 86% and 99%), the EMA trade
-3.9% and -6.0%, and the checklist top ten +10.4% against SPY's +15.1% (its
trend-gated variants lower). Episodic Pivot earned +1.6% to +2.4% a year on about
5% average invested capital; the one result that beat SPY is its overlay variant,
which holds SPY with the trades on top (+17.3% at 1.0% risk, against +15.1%) and so
is mostly SPY. The Nash posts have no portfolio runs to compare here. Each page's
research panel reads these numbers from the saved files, not from this README. The
workspace supports screening, holding and alerting on these rules. It does not claim
they work.

**Not included.** The index dip ladder, the VIX, seasonal and midterm timing rules
and the put hedge pick no stocks, so they have no screen, positions or exit
alerts and stay research-only.

**Settings.** Parameters live under `strategies:` in `config.yaml`
(`src/stratlib/strategy_params.py` lists every field and its research default);
the Settings page shows the values in force. Unknown keys are rejected. The
thresholds editor does not change them, and editing one never alters an open
position's saved exits.

### MSCI GARP

MSCI GARP holds the MSCI USA Quality GARP Select Index, the index of the iShares
GARP ETF, rebuilt from MSCI's methodology by `research/mscigarp_index.py`. The rules
are in `src/stratlib/msci_garp.py`; the report
(`research/output/mscigarp/report.md`, "MSCI USA Quality GARP" in Reports) sets out
every step with its formulas.

- **Rules.** Each quarter the S&P 500's companies (standing in for the MSCI USA
  Index, with FMP's sectors for GICS) are scored on growth, taken in growth order
  until they cover half the S&P 500's market cap (current holdings ranked up to
  65% come first once 35% is reached), and weighted by market cap times a tilt
  toward quality and value, capped at 5% per company with each sector within 5
  points of its share of the selected stocks. Reviews take effect at the last
  session of February, May, August and November, on data as of the month before.
  Between reviews a stock that leaves the S&P 500 is sold and nothing is bought.
  There is no stop and no market rule.
- **What the rebuild cannot see.** MSCI's growth score leans on analysts' forward
  EPS forecasts, which FMP does not keep point in time. The long-term forecast is
  left out, as MSCI's own missing-data rule does, and trailing-year EPS growth
  stands in for the short-term one (`growth_variant: proxy`). The parent (S&P
  500, full market caps) and the sectors (FMP's) also stand in for MSCI's. The
  rebuild tracks MSCI's official index with about 3% tracking error and returned
  16.0% a year from December 2015 against MSCI's 17.6%, most of the gap in
  2023–2025. At the August 2026 review its holdings overlapped the real index's
  by 66% of their weight, and 85% when that review was rebuilt with MSCI's
  universe, free float, GICS sectors and current forecasts
  (`research/notes/mscigarp_full_recipe_2026-08.md`). Past forecasts are licensed
  data, so the monthly estimates snapshot (First backfill) collects them from now
  on. The Backtest and Strategies pages and the research report say which figures
  are MSCI's own and which come from the rebuild. The Screen's holdings are the
  index's own, from iShares (below).
- **Screen: the index's live holdings.** The Screen lists the index's actual
  holdings and weights: the file the iShares GARP ETF, which holds the index,
  publishes every day. An online run downloads it (a free download, not an FMP
  call) and caches it as `research:mscigarp:fund_holdings`. A cache-only run uses
  that copy or the latest monthly estimates snapshot's, and warns when it is more
  than five days old. A card at the top of the page reads "Live index holdings"
  with the file's date.
  - MSCI's own constituent list is free too, but it is months late (1 June on
    2026-10-02) and has names without tickers, so the monthly snapshot keeps it as
    a record only.
  - Beside each holding the Screen shows the rebuild's weight and its growth,
    value and quality scores and tilt. Stocks outside the S&P 500 have no scores.
  - The funnel counts the index's holdings, the rebuild's, those held by both and
    how much of the index's weight the rebuild matches. On 30 September 2026:
    130, 95, 62 and 66%.
  - Holdings without stored prices take the file's price, for example Alphabet's
    class A shares, a secondary listing the app does not backfill.
- **Screen: the rebuild behind it.** Each screen also rebuilds the latest review
  that has taken effect, after the 12 reviews before it (`warmup_reviews`), so the
  buffer for current holdings acts as it would have.
  - It reads the research's caches: the S&P 500 list and change log
    (`research:garp:sp500`), statements (`research:garp:stmts:<symbol>`), cash
    flow (`research:mscigarp:extra:<symbol>`) and dividends
    (`research:nash:div:<symbol>`).
  - An online run refreshes the list at most weekly (two calls) and refetches a
    company when it is missing, over 120 days old or has reported since (five
    calls each).
  - If iShares' file is unavailable and nothing is cached, the Screen shows the
    rebuild's holdings instead, under a "Rebuilt holdings" card.
  - The rebuild's August 2026 review matched the research rebuild's on 93 of 94
    stocks. The exceptions: Alphabet's class C shares (GOOG) in place of class A,
    and Gartner, which the research's longer run of reviews left out.
- **Portfolio and alerts.** The hypothetical portfolio buys each holding's index
  weight rounded to the nearest whole share, as the backtest does. Alerts compare a
  holding with the latest screen: Sell when the index's published holdings leave it
  out, otherwise Hold until the next review. A screen built from the rebuild uses
  the rebuild's reviews instead: Sell when it dropped the stock at its last review
  or when the stock left the S&P 500.

```powershell
.venv\Scripts\stratlib screen --strategy msci_garp            # refreshes the list and statements
.venv\Scripts\stratlib screen --strategy msci_garp --offline  # cached data only
```

### Positions and sell alerts

Add a ticker, entry date and entry price on Positions. Each purchase is a
separate lot, including multiple entries in one ticker. Edit a lot, mark it
closed, or reopen it from Closed positions. These actions only update the
local SQLite database. They do not place orders.

For CANSLIM, the defaults selected for Phase 4 are a **7% loss limit** and **20% profit
target**. Alerts use the latest completed daily close, with its date shown
alongside the result. A stop takes priority over the hold exception. An alert
continues beyond either threshold, so a gap through a limit still flags it.
Daily lows and historical intraday touches do not imply an execution here;
the backtester will check daily lows in Phase 5.

For the fast-gain exception, optionally enter the actual breakout date and
its pivot/reference price. The app checks for a completed close at least 20%
above that reference within three calendar weeks, including the boundary
date. If found, the profit rule is suspended until eight calendar weeks
from breakout, even if the gain later retreats. The loss limit still applies.
The hold expires exactly on the eight-week date, using the assessment's price
date rather than today's wall clock. All five sell parameters are editable.

Entry is not assumed to equal breakout. Missing breakout evidence produces
**Review breakout** when the profit target is reached while a hold might
still apply. Missing early bars also prevent an unqualified profit exit.
SPY's cached sessions distinguish holidays from gaps. An observed qualifying
close is sufficient to establish the hold. Once eight weeks have passed
since entry, any earlier breakout's hold has necessarily expired.

Enter prices on the same split-adjusted basis as the current cached chart.
The app saves a dated close as a price reference. If a later backfill restates
that close after a split, it scales the entry and breakout references by the
same ratio. Original inputs stay stored. If prices were absent when a lot
was added, backfill the ticker and edit/re-save the lot to confirm its price
basis. Missing references and stale histories are shown explicitly.

### Settings

Thresholds are grouped by sell rules and CANSLIM criteria. Save thresholds
validates the full set, including related duration and depth limits, before
atomically replacing the YAML file. It preserves comments and unrelated
settings, including data paths and the shared rate limiter. A form based on
outdated thresholds cannot overwrite newer saved edits.

Load defaults into form changes the draft only. Reload saved settings discards
draft edits. Saved changes apply to new position rule snapshots, legacy CANSLIM
alerts and current N/M checks on their next view. Positions with saved rules
retain those settings until explicitly reassigned. Existing screen results keep
their recorded thresholds until you run the screen again. Neither page makes
API calls.

### TradeTest

**TradeTest** (`/tradetest`, in both apps) deals ten blind daily charts to mark up and trade forward one bar at
a time; the instrument and dates show only once a chart is finished. Its charts come from a bank of random
250-session windows (150 bars of history, 100 to trade), built offline from the database, which is opened
read-only:

```powershell
.venv\Scripts\stratlib tradetest-bank   # writes public\tradetest_bank.npz; --windows, --seed, --db, --output
```

The windows are US common stocks, delisted ones included, and up to 15% ETFs from an allowlist of plain,
unleveraged funds read from the database (`research/cache/supply_demand_all_daily.json` fills in any the database
lacks); no two windows of one instrument share a session, which holds the ETFs to about 12%. Every window is
complete, liquid, above $5 as traded (split records undo the split adjustment), free of spliced histories and
reused tickers, and clear of the market events a visitor would recognise; the constants at the top of
`src/stratlib/tradetest_bank.py` set the rules. A build takes about seven minutes.

The app reads `public/tradetest_bank.npz` beside `config.yaml`, or the file `STRATLIB_TRADETEST_BANK` names, and
signs its chart tokens with `STRATLIB_TRADETEST_KEY` (else `/etc/machine-id`, else a key per process). Rebuilding
the bank expires the sets visitors have in progress.

### Public read-only site

`stratlib web --public` serves a read-only copy for visitors. It opens on
**Strategies**, the page described above: the nine strategies side by side
with their comparable backtests. Picking one shows its rules, sizing,
candidates and backtest, and every other page then opens on it.
Screen, Portfolio, Market, Stock detail, Backtest and Reports work as usual,
apart from these changes:
- Positions and Settings are gone (their addresses return 404);
- nothing runs: no Run screen, Evaluate, Run backtest, Prepare data or Jev
  review, and no API key is read;
- nothing is saved: no cash, sponsorship or archive import. Portfolio drops the
  recorded account and holdings; Stock detail drops sponsorship and Jev;
- the top bar says "Read-only demo" instead of the API call count, and every
  page ends with a not-investment-advice footer.

The public site reads a snapshot, not the working database:

```powershell
.venv\Scripts\stratlib publish                 # writes public\stratlib.db
.venv\Scripts\stratlib web --public --db public\stratlib.db --port 8601
```

The snapshot holds the universe, every saved screen and backtest result (of the
comparable backtests, each strategy's newest), the cached statements, the full history of the market symbols, and the last
`--years` (default 4) of prices for every other stock. That is what the pages
read: market direction replays each index from its first session, and Stock
detail's three-year chart and base search need just over three years. On
2026-10-01 it came to 424 MB and took seconds to write; its market state and
bases matched the full database. Positions, recorded cash, sponsorship notes,
Jev reviews, the run log, the API response cache and the backtest preparation
data stay behind. The file is opened read-only, so SQLite itself refuses any
write.

The pages never name the data provider (Financial Modeling Prep), on either app, since 2026-10-01.

To deploy it, copy the code, `config.yaml`, `research/reports.json`,
`research/output` and the snapshot to the server, with no `.env`. Then run
`stratlib web --public --db <snapshot> --host 0.0.0.0` behind a reverse proxy
that serves HTTPS. NiceGUI keeps a websocket open to each tab, so the host must
run a long-lived process; static or serverless hosting will not work. To
refresh the site, run the backfill, the screens and `stratlib strategy-backtest`
locally, publish again, upload the new file and restart the app. Strategies with no saved screen show as not
screened, so run every strategy's screen before publishing. Financial Modeling
Prep's terms govern showing its prices and statements to other people; check
your plan before going live.

## Screening

Update daily prices first with `stratlib backfill`, then:

```powershell
.venv\Scripts\stratlib screen
.venv\Scripts\stratlib screen --sample 50
.venv\Scripts\stratlib screen --offline
```

The `--sample` option limits the number of **survivors** whose fundamentals
are evaluated. RS and industry ranks still use the whole stored universe.
The command refuses to rank when any universe stock has no price history.
Short or incomplete histories get no rating; coverage and stale symbols are
reported. A failed run preserves the previous completed screen.

The weekly base search is most of a screen's work, so `stratlib screen` and the
web app's Run screen spread it over every processor core; `--workers N` sets
the number of processes. On 2026-10-01 a cache-only screen of 5,567 stocks took
19 seconds on 16 cores, against 193 seconds before, with an identical result.
Code that calls `run_screen` itself uses one process unless it passes
`workers`. On Windows, more than one needs the calling script to guard its work
with `if __name__ == "__main__":`, or every worker restarts the script and the
screen hangs.

Each check shows Pass, Fail, or Unavailable. A stock passes the C/A/S/L
rule when the price prefilter passes, quarterly EPS growth, quarterly sales
and RS all pass, and enough of the other eight checks pass:

- `thresholds.scored_checks_required` is how many of those eight must pass,
  and `min_measurable_scored_checks` is how many must be measurable. The
  defaults, 8 and 8, are the brief's rule: every check passes.
- With fewer required, a check that cannot be measured (for example cash flow
  against negative EPS, or a young company without 12 quarters) is left out
  when enough others are measurable. The requirement then shrinks in
  proportion: with 5 of 8 required and 6 measurable, 4 must pass.
- On 2026-09-29 `config.yaml` chose 5 of 8 with at least 6 measurable. Over
  the previous year that admitted about 19 stocks a day, against 0.1 when
  every check was required.

This is not a complete CANSLIM buy signal. The separate Phase 3 result also
requires both N checks, and a market that allows new buying. The manual I
field does not alter either verdict.

### Rule definitions

- Price prefilter: latest completed close at least $15, average daily volume
  at least 400,000 shares, within 15% of the highest daily high in 252 sessions,
  and strictly above the 50- and 200-session simple averages. The volume
  minimum was unspecified in the brief; 400,000 is the configurable default.
- RS: four separate 63-session close-to-close returns, weighted 2:1:1:1,
  ranked across all eligible common stocks before any filters. Average tied
  ranks map linearly to 1..99 with half-up rounding. An all-tied population
  ranks 50. Requires 253 aligned closes. SPY supplies the session calendar.
  Stock histories cover that entire date range before alignment, so extra
  provider dates cannot displace the oldest RS anchor.
- Industry groups: equal-weight 126-session returns across all eligible
  members, including stocks rejected by the prefilter. Tied groups share
  competition rank. Top 40 passes.
- C EPS uses diluted EPS from continuing operations. FMP does not expose a
  separate diluted continuing-EPS field. The calculation starts with
  `epsDiluted` and subtracts `(netIncome - netIncomeFromContinuingOperations)
  / weightedAverageShsOutDil`. If reported EPS is missing, it derives EPS
  from continuing income and diluted weighted-average shares. Missing
  continuing income is not replaced with total net income.
- EPS acceleration counts increases in the latest four quarterly YoY growth
  rates, requiring at least two of three increases. Sales passes at 25% YoY
  growth or when the latest three YoY rates strictly increase. Comparisons
  match fiscal year/quarter; gaps and currency changes produce Unavailable.
  Growth uses `100 * (current - prior) / abs(prior)` for nonzero prior values.
  A smaller loss is positive growth, a larger loss is negative growth, and
  crossing from loss to profit can exceed 100%. For example, EPS moving from
  -2.13 to -1.96 improves 7.98%. Quarterly and annual EPS growth and EPS
  acceleration use this convention. A large enough loss reduction can pass
  an EPS growth check even while EPS remains negative; explanations state
  whether a loss narrowed, widened, or turned into a profit. This convention
  also applies to sales growth. Share-growth inputs must still be positive.
  A zero prior value remains undefined. Its explanation says "Not meaningful"
  and shows the dated EPS inputs. This does not indicate a missing download.
  Annual operating cash flow per diluted share is also shown when available,
  even if nonpositive EPS prevents the separate cash-flow-above-EPS percentage.
- After-tax margin is continuing income / revenue. "Near its high" means
  at least 95% of the maximum of 12 consecutive quarterly margins and a
  positive current margin. The 5% tolerance is relative, not five points.
- Annual EPS must grow at least 25% in each of three comparisons, requiring
  four consecutive fiscal years. ROE uses latest annual continuing income
  divided by average opening and closing stockholders' equity, both positive.
- Cash flow per share uses annual operating cash flow divided by diluted
  weighted-average shares, for the same fiscal period and currency as EPS.
  EPS must be positive; cash flow per share must be at least 20% higher.
- S requires diluted share count to be flat or falling over three years
  and in the latest annual comparison. Older intermediate increases are
  allowed. Debt/equity compares the latest and oldest of four annual
  statements and must be flat or falling overall. Its ratio is
  `totalDebt / totalStockholdersEquity`; nonpositive equity is unavailable.
  The total share change, latest share change and total debt/equity change
  each have a configurable maximum growth percentage, defaulting to zero.
- FMP's recorded NVDA statements already restate historical EPS and diluted
  shares for splits. Applying split ratios from fiscal year-end would double
  adjust them. The app records the share-basis date on fetch and applies only
  later actions from FMP's split history. Split history refreshes separately
  from statements, with a shared one-day response cache.
- Material discontinued income and `otherAdjustmentsToNetIncome` are flagged
  when identifiable and at least 20% of continuing income. This does not
  identify all unusual charges, tax effects, or adjusted earnings.
- Quarterly data older than 190 days and annual data older than 550 days are
  unavailable. Semiannual foreign reporters may lack the quarterly history
  necessary for C; they remain in the price and RS universe.

### Fundamental refresh policy

The initial survivor pull uses six calls: quarterly and annual income,
balance sheet, and cash flow statements. A split-history request is the
seventh. Income history is 12 quarters and four years; balance/cash history
is four quarters and four years. Completed bundles persist in SQLite.

Later runs check the current Monday-Sunday earnings calendar and refresh
statements at most once per day after a listed announcement, during that
week. Stocks not reporting this week retain their bundles. A report delayed
beyond that week or a missed calendar entry will need a future catch-up
policy. Empty/short responses yield Unavailable criteria. Plan restrictions
stop the run and are reported; no other data provider is substituted.

The live screener reads current cached data. Backtesting uses a separate dated
archive; filing dates alone cannot undo later restatements in FMP data.

## Backtesting

### Comparable backtests: every strategy

`stratlib strategy-backtest` backtests every strategy under one set of rules, so
they can be compared. The Strategies page and the top of each strategy's Backtest
page show the results.

```powershell
.venv\Scripts\stratlib strategy-backtest                     # all of them, about five minutes
.venv\Scripts\stratlib strategy-backtest --strategy trend --strategy nash_quality
```

- **Engine and ground rules.** The research engine, now in `src/stratlib/sim`
  (the `research/` scripts import it from there), under the research ground
  rules: $100,000 to start; slippage of 0.10% a side, 0.25% under $20; stops
  filled during the day at the stop or a worse opening gap; SPY with dividends
  as the benchmark, and the 3-month T-bill for Sharpe ratios.
- **Liquidity floor.** No buy under $5 or under $5M of average daily dollar
  volume (20 sessions) on the signal day. For the six scan strategies this is
  their own `min_price` and `min_dollar_volume_m` in `config.yaml`, which their
  live screens use too; CANSLIM and Trend Leaders use $5 and $5M. The research
  used $20M, and the app's floor was lowered to $5M on 2026-10-01 (see below).
  The price panel is built at the lowest floor in use; a strategy with a
  stricter one buys only what clears it.
- **Universe.** US common stocks, including those delisted since 2016, as the
  screens see them; no ETFs. SPY and QQQ are in the price panel for the
  benchmark and the market filters but are never traded. FMP lists few
  delistings before 2020, so the earlier years lean toward survivors.
- **Periods.** In-sample 2016–2021, out-of-sample 2022 onward, and combined, as
  in the research.
- **Each strategy's own rules**, from `config.yaml` as the live screens use
  them: entries, exits, position sizes and market rule. The six scan strategies
  are the research strategy classes, with risk-based sizes and their market
  filter. CANSLIM and Trend Leaders keep the rules of the app's own backtests:
  the approximate method's daily breakout signals and trend-leader rankings,
  equal slots under the market's exposure ladder, raise cash, and a full slot
  or none. Nash rebalances quarterly into equal slots, keeping holdings that
  still pass.
- **Data.** CANSLIM and Trend Leaders need the approximate method's prepared
  data (`stratlib backtest-approx-prepare`); a run ends at the earlier of SPY's
  last close and the prepared end, for every strategy. The checklist and Nash
  read cached statements, Episodic Pivot cached earnings dates, and the
  benchmarks the cached SPY dividends and T-bill yields
  (`python research/benchmarks.py --refresh`). A run makes no API calls. The
  research cached statements and earnings for the stocks above $20M only; those
  for the 1,001 statement stocks and 1,796 tickers the $5M floor added were
  fetched on 2026-10-01 (about 5,800 calls). Stocks without them cannot pass the
  statement or earnings rules.
- **MSCI GARP** copies an index, topping up and trimming every holding at each
  review, which the trade-by-trade engine cannot do. `src/stratlib/sim/msci_garp.py`
  rebuilds the index review by review from 2007 and keeps a self-managed account
  under the same ground rules ($100,000, whole shares, the same slippage,
  dividends and the proceeds of stocks leaving the S&P 500 held in cash until the
  next review), measured by the engine's own metrics. Each stock's holding period
  counts as one trade. It reads the S&P 500 history the research prepared
  (`research/garp_data.py`, `research/mscigarp_data.py`): every member since 2004
  mapped to a price series, with statements, cash flow, dividends and splits. On
  the real data its reviews equal the research rebuild's (all 44 since November
  2015: the same holdings, weights within 0.0001%) and its account the research's
  self-managed one day by day.
- **Saved results.** Each strategy's run is saved as
  `backtest:engine:<strategy>:<time>`, in the research results format plus its
  trades. `stratlib publish` copies each strategy's newest.

Checks, 2026-10-01, made at the research's $20M floor. On the research panel,
the five strategies with research runs (Qullamaggie, Minervini, Episodic Pivot,
the 9/21 EMA and the checklist) reproduce `research/output` exactly. With
slippage and the liquidity floor turned off, CANSLIM's port matches the approximate method's simulator (112 of
117 entries the same, 2016-01-04 to 2026-09-25), and Trend Leaders' makes 19.4%
a year against the old simulator's 18.3%, with about three quarters of its entries
the same and most of the rest a few days apart.

**Why the floor is $5M.** At the research's $20M floor Trend Leaders made 4.5%
a year from 2016, not the 18% of the app's own backtest. Slippage cost 0.8
points and the floor the rest: without it, 22 trades in stocks that traded
under $20M a day at the signal made $291,000 of the $571,000 profit, and two of
them (WGS +235%, ALPN +306%) made $221,000. With $100,000, positions of $10,000
in such stocks are easy to trade, so the floor was lowered to $5M for every
strategy, the live screens included.

Results, run 2026-10-01 with the settings in `config.yaml`, prices through
2026-09-30. CAGR and maximum drawdown in percent; SPY includes dividends. The
$20M column is the same run at the research's floor, through 2026-09-25.

| Strategy | 2016–2026 CAGR | at $20M | Max drawdown | Sharpe | Trades | 2016–2021 CAGR | 2022–2026 CAGR |
|---|---:|---:|---:|---:|---:|---:|---:|
| CANSLIM | −0.1 | 0.2 | 13.2 | −0.59 | 113 | 0.3 | −0.3 |
| Trend Leaders | 19.1 | 4.5 | 68.9 | 0.58 | 135 | 26.7 | 8.3 |
| Qullamaggie Breakout | −3.3 | −3.9 | 44.5 | −0.49 | 1,143 | 0.7 | −7.6 |
| Minervini VCP | −36.6 | −32.3 | 99.3 | −2.37 | 12,378 | −30.6 | −43.2 |
| Episodic Pivot | 2.0 | 1.6 | 7.0 | −0.08 | 244 | 1.4 | 2.8 |
| Traveling Trader 9/21 EMA | −2.6 | −3.2 | 58.3 | −0.12 | 1,827 | 2.3 | −10.0 |
| Traveling Trader Checklist | 13.2 | 10.5 | 34.3 | 0.58 | 362 | 17.0 | 9.1 |
| Nash Quality Screen | 12.9 | 12.1 | 57.1 | 0.48 | 112 | 10.7 | 11.1 |
| MSCI GARP | 15.7 | | 33.5 | 0.71 | 857 | 18.8 | 11.9 |
| SPY | 15.1 | 15.2 | 33.7 | 0.75 | | 17.6 | 11.9 |

MSCI GARP was added on 2026-10-02 (it has no liquidity floor, so no $20M run). Its
trades are holding periods. It finished 0.6 points a year ahead of SPY, all of it
from 2016–2021, at about SPY's drawdown and a slightly lower Sharpe ratio. MSCI's
own index, which uses the forecasts the rebuild lacks, returned 17.4% a year after
the ETF's costs from December 2015 (the research report).

Only Trend Leaders beat SPY, over the whole period and in 2016–2021, and at a
cost: a 69% drawdown, a lower Sharpe ratio than SPY's, and 88% of its $553,000
profit from five trades (NVAX, WGS, ALPN, AU and TDW). From 2022 it trailed SPY.
No strategy beat SPY from 2022. The checklist and Nash came closest, with
drawdowns as deep as SPY's or deeper. Minervini's result matches its research
run (−32.3% at $20M, 12,006 trades): about three in four entries are sold at
the same day's close because the breakout volume fell short, and each round
trip pays slippage. CANSLIM and Episodic Pivot averaged 12% and 4% invested,
which kept their drawdowns small.

### Approximate and strict backtests

Open **Backtest** in the GUI. Below the comparable backtest, the latest saved result is shown, with
its equity curve against SPY, the confirmed-uptrend periods shaded, and tabs
for closed trades, positions open at the end and skipped entries. Below it,
choose a method, dates, starting capital and maximum holdings. Defaults are
the most recent cached year, $100,000 and ten holdings, set in the `backtest`
section of `config.yaml`. A run makes no API calls and uses the current YAML
thresholds, which are saved with its result.

There are two methods:

- **Approximate** (default) follows the backtest section of
  `canslim_prompt.md`. It works from data FMP serves today: five years of
  statements per stock, each row visible from its SEC acceptance or filing
  date (45 days after quarter end or 90 after year end when neither exists),
  plus the delisted-company directory, so failed and acquired stocks stay in
  the universe. It is available immediately.
- **Strict** uses only universe snapshots and statements recorded on each
  past day (below). Nothing can leak from hindsight, but its history begins
  when you start capturing.

### Leaders portfolio

A second strategy, on the approximate method's data. It is chosen on the
Backtest page as **Leaders portfolio**. This remains a research strategy;
the live Portfolio page supports CANSLIM and Trend Leaders:

- It holds the top-ranked stocks that pass the Screen (the price and RS
  filter plus the C/A/S/L rule). Stocks are ranked by RS, then by scored
  checks passed. No base or breakout is required.
- Positions are equal-weight, one slot per `backtest.max_holdings`. The
  market's allowed exposure sets how many slots may be filled: 40% allows
  4 of 10.
- A monthly rebalance (`backtest.leaders_rebalance`: monthly or weekly)
  sells holdings that no longer pass or rank below
  `backtest.leaders_rank_buffer` (20). Open slots fill from the top of the
  ranking at the next open. Raise cash sells the lowest-ranked holdings
  first.
- The stop loss applies to each holding, and there is no profit target. A
  stopped-out stock waits for the next rebalance.

Three-year result (2023-09-26 to 2026-09-25, approximate): +35.7% against
SPY's +79.8%, with a 16.9% maximum drawdown against 19.0%. On average it was
51% invested, and allowed 52%. Diagnostic reruns, not saved, compared the
same selection under other conditions:

| Condition | Return |
|---|---|
| Market rule, no stop | +50.8% |
| Fully invested, 7% stop | +60.2% |
| Fully invested, no stop | +95.6% (maximum drawdown 29.8%) |

In this mostly rising period the selection beat SPY, but the market rule and
the stop each cost more than that edge. Judging those safeguards needs a
period that includes a bear market.

Nine-year results (2017-09-26 to 2026-09-25, approximate). The period
includes the 2018 fourth-quarter sell-off, the 2020 crash and the 2022 bear
market. All five runs are saved and labelled "…, 9 years: …". The variants
use `backtest.stop_loss: false` and `market_index_rule: ignore`.

| Run | Return | Yearly | Max drawdown | 2018 Q4 | 2020 crash | 2022 bear |
|---|---|---|---|---|---|---|
| Leaders as designed | −4.8% | −0.5% | 50.3% | −10.0% | −6.2% | −12.6% |
| Market rule, no stop | +34.3% | +3.3% | 42.9% | −9.1% | −6.0% | −9.4% |
| Fully invested, 7% stop | −17.2% | −2.1% | 61.6% | −17.0% | −22.0% | −32.9% |
| Fully invested, no stop | +22.5% | +2.3% | 52.5% | −23.4% | −44.7% | −20.5% |
| Breakout strategy | −0.8% | −0.1% | 12.7% | −3.7% | −3.3% | −3.7% |
| SPY | +209.3% | +13.4% | 34.1% | −20.2% | −34.1% | −25.4% |
| SPY at the allowed exposure | +85.7% | +7.1% | 11.3% | −8.9% | −4.9% | −9.7% |

What the nine years show:

- **Selection.** Over the full period, the leaders selection trailed SPY even
  fully invested with no stop. The three-year edge was not durable.
- **Market rule.** It cut losses in all three crashes, but it allowed an
  average of only 51% exposure. That cost more in the recoveries than it
  saved: 2020 −9.4% and 2023 −15.8% as designed, against SPY's +16.2% and
  +24.3%.
- **7% stop.** It lowered the result in both setups (by 39 and 40 points).
  It softened only the fully invested 2018 and 2020 crashes, and it made the
  fully invested 2022 bear market worse.
- **Turnover.** Holdings lasted 12–29 days on average, and about 35% of
  trades were winners. Raise cash (306 sells), the stop (320) and
  "no longer passes" (139) cut winners short. FUTU, NVDA and PLTR were each
  sold near +100% when they stopped passing.

As a cross-check, the last three years of the as-designed nine-year run
returned +36.1%. That matches the standalone three-year run's +35.7%.

### Rule study

```powershell
.venv\Scripts\stratlib backtest-rule-study --start 2017-09-26 --end 2026-09-25
```

The study asks which screening rules actually predicted returns. It uses
the approximate method's prepared data and makes no API calls; nine years
take about ten minutes.

On the first session of each month, `rule_study.py` ranks the whole market
with the same code as the backtest (`rankings(detail_days=...)`). For every
liquid stock (Screen price and volume minimums) it records the inputs to
each rule. Price and RS leaders are also scored on C/A/S/L and checked for
bases.

Each stock's return runs from the next session's close over the following
1, 3, 6 and 12 months. It is compared with SPY over the same sessions. For each
rule, the stocks passing it are compared month by month with the stocks
failing it:

- where the Screen applies the rule: the technical rules among all liquid
  stocks, and the fundamental and base rules among leaders;
- each month counts equally, and a month counts only with at least 3 stocks
  on each side.

A verdict of **Helps** or **Hurts** needs three things:

- the average and median differences agree in direction;
- a t-statistic of at least 2, counting one independent observation per
  horizon length, because monthly windows overlap;
- the same direction in two-thirds of the years.

**Slight help** or **Slight harm** relaxes these to a t-statistic of 1 and
half the years.

Value tables split stocks by RS, distance from the high, size and each
fundamental's value. The market table shows SPY's later returns by the
exposure the market rule allowed that day. The result is saved as
`research:rule_study` and, per period, `research:rule_study:START:END`.

Nine-year result (2017-09-26 to 2026-09-25): 109 monthly rankings and
156,400 stock-months. Each month had on average 1,435 liquid stocks,
201 price and RS leaders, and 10 Screen passers.

| Next 6 months vs SPY | Liquid stocks | Leaders | Screen passers |
|---|---|---|---|
| Average excess | −2.2% | +0.2% | +1.4% |
| Months ahead of SPY | 26% | 47% | 52% |

Rules against the stocks failing them, next 6 months (difference in average
excess, median difference, t-statistic, years in the same direction):

| Rule (population) | Mean | Median | t | Years | Verdict |
|---|---|---|---|---|---|
| Above the 200-day line (liquid) | +3.1 | +2.8 | 2.2 | 9/10 | Helps |
| Industry group in the top 40 (liquid) | +3.1 | +2.0 | 2.3 | 9/10 | Helps |
| All price and RS filters (liquid) | +2.8 | +0.8 | 1.6 | 9/10 | Slight help |
| RS 80 or higher (liquid) | +3.2 | 0.0 | 1.8 | 9/10 | Mean only: a few big winners |
| Quarterly sales growth (leaders) | +2.3 | +0.4 | 1.2 | 7/10 | Slight help (Helps at 1 month) |
| After-tax margin near its high (leaders) | +2.6 | +1.9 | 1.4 | 6/10 | Slight help |
| Industry rank (leaders) | +2.6 | +1.5 | 1.3 | 7/10 | Slight help |
| Quarterly EPS growth (leaders) | +1.0 | −0.4 | 0.6 | 5/10 | No clear effect |
| EPS acceleration, annual EPS, cash flow, shares, debt (leaders) | −0.4 to +0.7 | | ≤ 0.3 | | No clear effect |
| Return on equity (leaders) | −1.8 | +0.6 | −1.1 | 7/10 | No clear effect (3 and 12 months: slight harm) |
| C/A/S/L rule as a whole (leaders) | +1.9 | +4.9 | 0.6 | 5/10 | No clear effect |
| In a base (leaders) | −1.7 | +0.3 | −1.1 | 8/10 | No clear effect |
| Buyable breakout (leaders) | −1.3 | −1.0 | −0.4 | 4/8 | No clear effect |

Value tables:

- **RS.** Excess rises steadily with RS, from −3.9% below RS 40 to +1.5% to
  +1.8% at RS 90 and above. Even the top band beat SPY in only 40–45% of
  stocks.
- **Size.** The largest fifth by trading value averaged −0.9%, against −2.8%
  for the smallest.
- **Sales growth.** Leaders growing sales 20% or more averaged about +3%,
  against −1.9% for flat or falling sales.
- **High-bar stocks.** Leaders with the highest return on equity and the
  fastest annual EPS growth did no better than the rest.

Market rule: SPY returned the most afterwards on days the rule allowed 0%.

| Allowed exposure | Days | SPY next 6 months | Share of days up |
|---|---|---|---|
| 0% | 232 | +11.7% | 80% |
| 20% | 485 | +7.3% | 81% |
| 40% | 319 | +4.9% | 73% |
| 60% | 570 | +6.0% | 77% |
| 80% | 294 | +5.1% | 72% |
| 100% | 341 | +6.3% | 73% |

Some caveats apply:

- About 20 rules were tested at four horizons, so a single t-statistic
  near 2 could be chance. The two "Helps" rules held in 9 of 10 years.
- This period had fast recoveries after the 2018, 2020 and 2022 declines,
  but no long bear market like 2008.

**Out of sample, 2007–2017.** The trend-leaders rules were chosen from the
nine-year study, so the decade before it tests them on data they were not
fitted to. It includes the 2008 bear market and the 2009 momentum crash.

```powershell
.venv\Scripts\stratlib backtest-rule-study --start 2007-01-03 --end 2017-09-25
```

The study covered 129 monthly rankings and 121,456 stock-months. Each month
had on average 942 liquid stocks, 124 price and RS leaders and 7 Screen
passers. The universe before 2016 is mostly today's survivors (see Trend
leaders, "Data before 2016").

| Next 6 months vs SPY | Liquid stocks | Leaders | Screen passers |
|---|---|---|---|
| Average excess | +0.3% | −0.2% | −0.2% |
| Months ahead of SPY | 52% | 50% | 52% |

Both periods side by side, next 6 months (difference in average excess,
median difference, t-statistic, verdict):

| Rule (population) | 2017–2026 | 2007–2017 |
|---|---|---|
| Above the 200-day line (liquid) | +3.1, +2.8, t 2.2, Helps | −0.4, +0.5, t −0.2, no clear effect |
| Industry group in the top 40 (liquid) | +3.1, +2.0, t 2.3, Helps | −0.7, −0.6, t −0.5, no clear effect |
| RS 80 or higher (liquid) | +3.2, 0.0, t 1.8, mean only | +0.2, −0.8, t 0.1, no clear effect |
| All price and RS filters (liquid) | +2.8, +0.8, t 1.6, slight help | −0.4, −0.3, t −0.2, no clear effect |
| Quarterly sales growth (leaders) | +2.3, +0.4, t 1.2, slight help | +0.5, 0.0, t 0.4, no clear effect (1 month: slight help) |
| After-tax margin near its high (leaders) | +2.6, +1.9, t 1.4, slight help | +0.5, 0.0, t 0.3, no clear effect |
| Industry rank (leaders) | +2.6, +1.5, t 1.3, slight help | −1.8, −1.8, t −0.9, no clear effect |
| Return on equity (leaders) | −1.8, +0.6, t −1.1, no clear effect | +1.9, +2.5, t 1.6, slight help (1 month: helps) |
| Quarterly EPS growth (leaders) | +1.0, −0.4, t 0.6, no clear effect | +0.8, +0.8, t 0.6, no clear effect |
| C/A/S/L rule as a whole (leaders) | +1.9, +4.9, t 0.6, no clear effect | +1.6, −0.2, t 0.6, no clear effect |
| In a base (leaders) | −1.7, +0.3, t −1.1, no clear effect | +1.2, +0.8, t 1.0, no clear effect |
| Buyable breakout (leaders) | −1.3, −1.0, t −0.4, no clear effect | +0.5, +2.4, t 0.2, no clear effect |

Value tables, 2007–2017:

- **RS.** No gradient. Liquid stocks at RS 90–95 averaged +0.8% against SPY,
  and those at RS 95 and above −1.3%. Leaders at RS 98 and above averaged
  −7.7%.
- **Sales growth.** Leaders growing sales 18–35% averaged +2.3%, against
  −2.0% for flat or falling sales. That is the same shape as in 2017–2026,
  but weaker (t 0.9).
- **Size.** Every fifth by trading value averaged +0.1% to +0.4%.

Market rule, 2007–2017: SPY's next 6 months barely depended on the exposure
the rule allowed.

| Allowed exposure | Days | SPY next 6 months | Share of days up |
|---|---|---|---|
| 0% | 229 | +3.5% | 64% |
| 20% | 759 | +3.3% | 68% |
| 40% | 458 | +4.3% | 72% |
| 60% | 522 | +1.5% | 67% |
| 80% | 349 | +4.0% | 79% |
| 100% | 364 | +4.2% | 77% |

What the comparison shows:

- **No rule replicated.** The 200-day line and the top-40 industry group
  were the only "Helps" rules in 2017–2026. In 2007–2017 both had no effect,
  and each pointed the same way in only 5–7 of 11 years. RS 80+ and 90+,
  sales growth and the margin rule also had no clear effect. Return on
  equity reversed, from a slight harm to a slight help.
- **The market rule does not forecast SPY** in either period. In the
  backtests it still helped trend leaders, by stopping new buys in 2008 and
  2015 (see Trend leaders).
- **Survivorship.** Most companies that delisted in 2007–2015 are missing,
  failures and takeovers alike. Missing failures make weak stocks (below the
  200-day line, low RS) look better than they were, which would shrink these
  differences. Missing takeovers work the other way. The net effect is
  unknown.

### Trend leaders

A third strategy on the Backtest page, built from the rule study.

**What it buys.** Price and RS leaders (the Screen's price, volume, high and
moving-average filters) that also have:

- RS of at least `backtest.trend_min_rs` (90);
- an industry group in the top `thresholds.industry_top` (40);
- latest quarterly sales growth of at least
  `backtest.trend_min_sales_growth_pct` (20%);
- optionally, average daily trading value of at least
  `backtest.trend_min_dollar_volume_m`.

Candidates are ranked by RS, then by sales growth. The C/A/S/L rule, bases
and breakouts are not required.

**How it holds.** Slots are equal-weight at entry and filled at the next
open. The market's allowed exposure limits new buys only; raise cash and
rebalances never sell.

**When it sells.** A holding is sold at the open after a close below its
200-day line, or at delisting. `backtest.trend_loss_cap_pct` adds a loss cap.
It is 0 (no cap) since 2026-09-29, because the nine-year runs did better
without one. The nine-year runs below used 15% unless marked "no loss cap".
The 2007 runs used no cap unless marked "15% loss cap".

```powershell
.venv\Scripts\stratlib backtest --approximate --strategy trend --start 2017-09-26 --end 2026-09-25 --label "My run"
```

`--strategy` also accepts `leaders` and `breakout` (the default). `--label`
names the saved result on the Backtest page.

With **Trend Leaders** selected, Portfolio uses its latest saved strategy screen:

- Recorded holdings include only lots assigned to this strategy and variant.
  Their alerts use their own saved trend window and optional loss cap.
- **Model changes** ranks qualifying stocks. After the proposed exits, open
  slots receive **Proposed buy**, subject to market exposure and current data.
  Missing or mismatched evidence produces **Review data** instead.
- **Strategy rules and research** links to the corresponding Backtest controls
  and the Reports library. Saved backtest evidence remains on Backtest.

Nine-year results (2017-09-26 to 2026-09-25, approximate). On average there
were 22.5 trend leaders a day, or 11.5 with a $100M trading-value floor. All
runs are saved as "Trend leaders, 9 years: …".

| Run | Return | Yearly | Max drawdown | Volatility | 2018 Q4 | 2020 crash | 2022 bear |
|---|---|---|---|---|---|---|---|
| 10 holdings, market ignored | +158.4% | +11.2% | 51.8% | 39% | −39.5% | −41.5% | −10.2% |
| 10 holdings, market ignored, no loss cap | +224.6% | +14.0% | 54.4% | 42% | −32.6% | −39.0% | −1.3% |
| 10 holdings, market limits buys | +167.6% | +11.6% | 68.9% | 39% | −27.1% | −23.8% | −9.0% |
| 10 holdings, market ignored, $100M+ a day | +45.2% | +4.2% | 74.7% | 38% | −27.8% | −27.1% | −15.2% |
| 20 holdings, market ignored | +131.2% | +9.8% | 52.5% | 36% | −32.2% | −35.2% | −7.1% |
| 20 holdings, market limits buys | +108.5% | +8.5% | 60.5% | 32% | −27.0% | −23.1% | −4.4% |
| SPY | +209.7% | +13.4% | 34.1% | 19% | −20.2% | −34.1% | −25.4% |
| **As configured**: 10 holdings, market limits buys, no loss cap | +209.0% | +13.4% | 68.0% | | | | |
| 70% SPY + 30% trend leaders, no loss cap (rebalanced yearly) | +230.6% | +14.3% | 35.9% | 23% | | | |

- **Against the old leaders portfolio.** With the same 10 slots, return rose
  from −4.8% to +158%. Median holding periods rose from 8 days to 57, or to
  175 without the loss cap.
- **Against SPY.** Only the no-loss-cap run beat SPY, by 0.6 points a year,
  with twice the volatility.
- **Drawdowns.** Every run lost 52–75% from the February 2021 peak, as
  high-growth stocks fell while SPY rose.
- **Concentration.** The five best trades made 73–144% of each run's total
  profit (NVAX 2020, TNDM 2018–19, WGS, AGX, SNDK and LITE). Small changes
  swing the result from +45% to +225%.
- **Settings.** Fewer holdings, no loss cap and no size floor did better. The
  market rule softened the 2018 and 2020 crashes, but it did not reduce the
  overall drawdown.

**Back to 2007 (approximate).** The rules above were chosen from 2017–2026
data, so 2007-01-03 to 2017-09-25 is an out-of-sample test. It includes the
2008 bear market and the 2009 momentum crash. Preparation on 2026-09-29 took
31,407 FMP calls and about an hour:

```powershell
.venv\Scripts\stratlib backtest-approx-prepare --start 2007-01-03 --end 2026-09-25
```

It gave 8,702 members: 5,564 current and 3,118 delisted. 3,392 stocks
passed the price and RS filters on some day, and all have statement
histories, 93 quarters and 25 years deep. No split history was
unavailable.

**Data before 2016.** Monthly averages, from the first session of each month:

| Year | Priced members | Price and RS survivors | Sales growth measurable | Trend leaders |
|---|---|---|---|---|
| 2007 | 2,819 | 143 | 92% | 19.6 |
| 2008 | 2,937 | 103 | 95% | 18.1 |
| 2009 | 2,996 | 37 | 92% | 2.5 |
| 2010 | 3,108 | 85 | 94% | 7.2 |
| 2011 | 3,229 | 114 | 94% | 13.3 |
| 2012 | 3,355 | 132 | 97% | 8.5 |
| 2017 | 4,293 | 173 | 96% | 16.2 |
| 2025 | 5,500 | 205 | 94% | 19.5 |

- **Survivorship.** FMP's delisted-company directory has only 60 rows for
  2007–2015, of which 18 became members, against thousands a year since 2022.
  So the early universe is mostly companies still listed in 2016 or later.
  The 2007 universe was about 2,800 stocks, against roughly 5,000 US
  listings at the time.
  - Lehman and Bear Stearns are not in the directory, and FMP returns no
    prices for LEH or BSC.
  - The tickers WM and WB now belong to Waste Management and Weibo, so
    Washington Mutual and Wachovia are absent too.
  - Merrill Lynch's row (MER) has only 2018 prices of another instrument.

  Results before 2016 are therefore flattered by an unknown amount.
- **Delistings.** A holding that delists is sold at its last close before
  delisting, because no documented proceeds were imported. No holding in
  any 2007–2017 run delisted. The 2007–2026 runs had 2–6 such exits, all in
  2019 or later and mostly takeovers, such as SendGrid, Continental
  Resources, ImmunoGen and Alpine Immune Sciences.
- **Statements.** In every year, including 2007, 92–97% of survivors had
  the five public quarters the sales-growth check needs. Most of the rest
  had no year-ago quarter yet.
- **Thin years.** 2009 (37 survivors and 2.5 trend leaders a month), 2010,
  2012 and 2016 averaged fewer trend leaders than the 10 slots.
- **A ranking quirk.** 22 delisted stocks, such as II-VI and Lawson
  Products, have directory IPO dates after their first price bar. The
  2007–2017 runs leave them out, and the 2007–2026 runs include them. That
  small difference changed later picks, so the first decade of each
  full-period run differs from the matching 2007–2017 run.

Results are saved as "Trend leaders, 2007–2017 (out of sample): …",
"Trend leaders, 2007–2026: …" and "Leaders, …: as designed (market rule +
7% stop), for reference". The full-period "as configured" run was saved
last and remains available on Backtest. Variants change one
setting from the configuration: 10 holdings, market limits buys, no loss
cap. Windows run from SPY's closing peak to its closing low:

- 2007–09 bear: 2007-10-09 to 2009-03-09;
- 2011: 2011-04-29 to 2011-10-03;
- 2015–16: 2015-05-21 to 2016-02-11;
- 2018 Q4: 2018-09-20 to 2018-12-24;
- 2020 crash: 2020-02-19 to 2020-03-23;
- 2022 bear: 2022-01-03 to 2022-10-12.

Out of sample, 2007-01-03 to 2017-09-25:

| Run | Return | Yearly | Max drawdown | Volatility | 2007–09 bear | 2011 | 2015–16 | Top 5 trades' share of profit |
|---|---|---|---|---|---|---|---|---|
| **As configured** | +137.4% | +8.4% | 55.0% | 26% | −50.5% | −29.0% | −18.8% | 104% |
| Market ignored | −16.8% | −1.7% | 66.9% | 29% | −64.9% | −27.5% | −36.4% | (a loss) |
| 15% loss cap | +71.7% | +5.2% | 57.5% | 24% | −52.0% | −32.1% | −25.0% | 151% |
| 20 holdings | +123.5% | +7.8% | 39.8% | 22% | −32.1% | −28.5% | −14.9% | 78% |
| Old leaders portfolio, as designed | +55.3% | +4.2% | 23.5% | 12% | +5.1% | −14.7% | −6.2% | 55% |
| SPY | +75.0% | +5.4% | 56.5% | 20% | −56.5% | −19.4% | −14.4% | |
| SPY with dividends reinvested | +118.5% | +7.6% | 55.2% | | | | | |

Full period, 2007-01-03 to 2026-09-25:

| Run | Return | Yearly | Max drawdown | Volatility | 2007–09 bear | 2011 | 2015–16 | 2018 Q4 | 2020 crash | 2022 bear | Top 5 share |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **As configured** | +639.4% | +10.7% | 68.0% | 34% | −50.5% | −29.0% | −21.0% | −30.3% | −22.3% | −5.2% | 92% |
| Market ignored | +332.2% | +7.7% | 67.2% | 36% | −65.2% | −29.9% | −40.4% | −32.1% | −38.3% | −1.3% | 67% |
| 15% loss cap | +269.6% | +6.9% | 68.9% | 32% | −52.0% | −32.2% | −27.3% | −27.9% | −21.7% | −9.0% | 124% |
| 20 holdings | +617.5% | +10.5% | 67.1% | 30% | −32.4% | −28.5% | −15.5% | −25.4% | −25.9% | −12.0% | 60% |
| Old leaders portfolio | +49.5% | +2.1% | 50.3% | 14% | +5.2% | −14.7% | −6.1% | −10.0% | −6.2% | −12.6% | 180% |
| SPY | +442.2% | +8.9% | 56.5% | 20% | −56.5% | −19.4% | −14.4% | −20.2% | −34.1% | −25.4% | |
| SPY with dividends reinvested | +677.7% | +11.0% | 55.2% | | | | | | | | |

70% SPY and 30% of each run, rebalanced at each year's first session
(price-only SPY):

| Mix | 2007–2017 | Max drawdown | 2007–2026 | Yearly | Max drawdown | Volatility |
|---|---|---|---|---|---|---|
| With as configured | +99.0% | 54.9% | +569.5% | +10.1% | 54.9% | 21% |
| With market ignored | +50.6% | 58.9% | +460.1% | +9.1% | 59.0% | 22% |
| With 15% loss cap | +82.0% | 55.4% | +439.7% | +8.9% | 55.4% | 20% |
| With 20 holdings | +95.5% | 49.9% | +552.4% | +10.0% | 50.0% | 20% |
| With the old leaders portfolio | +77.8% | 41.0% | +300.4% | +7.3% | 41.0% | 15% |
| SPY alone | +75.0% | 56.5% | +442.2% | +8.9% | 56.5% | 20% |

Calendar years of the full-period runs (2007 from January 3, 2026 to
September 25):

| Year | As configured | Market ignored | 15% loss cap | 20 holdings | Old leaders | SPY | 70/30 with as configured |
|---|---|---|---|---|---|---|---|
| 2007 | +31% | +12% | +24% | +35% | +34% | +3% | +11% |
| 2008 | −36% | −55% | −38% | −24% | +4% | −38% | −38% |
| 2009 | −6% | −9% | −11% | −5% | 0% | +23% | +15% |
| 2010 | +6% | +8% | +14% | +23% | +3% | +13% | +11% |
| 2011 | −15% | −7% | −22% | −19% | −15% | 0% | −5% |
| 2012 | +17% | +29% | +8% | +7% | +13% | +13% | +15% |
| 2013 | +50% | +45% | +48% | +48% | +13% | +30% | +36% |
| 2014 | +14% | +6% | +13% | +1% | −5% | +11% | +12% |
| 2015 | −14% | −26% | −24% | −2% | 0% | −1% | −5% |
| 2016 | +3% | −6% | +9% | −3% | −1% | +10% | +8% |
| 2017 | +67% | +92% | +66% | +53% | +18% | +19% | +34% |
| 2018 | +10% | −5% | −3% | +3% | −1% | −6% | −1% |
| 2019 | +40% | +26% | +27% | +33% | +7% | +29% | +32% |
| 2020 | +89% | +27% | +91% | +84% | −9% | +16% | +38% |
| 2021 | −27% | −20% | −24% | −24% | +5% | +27% | +11% |
| 2022 | −4% | 0% | −12% | −13% | −24% | −19% | −15% |
| 2023 | −2% | +36% | −6% | −4% | −16% | +24% | +16% |
| 2024 | +68% | +52% | +51% | +50% | +12% | +23% | +37% |
| 2025 | +11% | 0% | +9% | +31% | +6% | +16% | +15% |
| 2026 | −6% | +40% | 0% | +2% | +13% | +13% | +7% |

What the 2007 extension shows. SPY is price-only unless marked, and there
are no trading costs. Trend leaders traded about 13 times a year, with a
median hold of 150 days, so costs would be small.

- **Out of sample, as configured.**
  - It beat SPY's price return, +137% against +75% (8.4% a year against
    5.4%). SPY with dividends made 7.6% a year.
  - The lead came from 2007, 2013 and 2017. It trailed SPY in 2009–2011
    and 2015–16.
  - Its drawdown matched SPY's (55% against 56%) but lasted until
    September 2009, six months past SPY's low.
  - Four Chinese ADRs and Take-Two (VIPS, TAL, WB, BIDU, TTWO) made 104% of
    the profit.
- **Full period, as configured.**
  - +639% against SPY's +442% (10.7% a year against 8.9%). That is about
    even with SPY's +678% (11.0%) with dividends reinvested.
  - Its maximum drawdown was 68%, from February 2021 to November 2023,
    against SPY's 56%, with 34% volatility against 20%.
  - It beat SPY in 11 of 20 calendar years.
- **Market rule.** Limiting new buys mattered most in the out-of-sample
  decade. Ignoring the market turned +137% into −17%. It made 43 buys in
  2008 instead of 21, nearly all losers, and lost more in 2014–15. Over the
  full period the rule was worth +639% against +332%. In 2017–2026 it made
  little difference (+209% against +225%).
- **Loss cap.** A 15% cap was worse in both periods: +72% against +137%,
  and +270% against +639%.
- **20 holdings.** Return was close to 10 holdings (+124% against +137%,
  and +618% against +639%). The out-of-sample drawdown was much smaller
  (40% against 55%), volatility was lower, and the top five trades made less
  of the profit (60% against 92% over the full period). Its 67% drawdown
  also came in 2021–23.
- **70/30 mix.** It beat SPY in both periods at about SPY's risk: +99%
  against +75%, and +570% against +442%. With SPY's dividends included in
  both, it made 11.5% a year against 11.0% over the full period.
- **Fragility.** Nearly the same data gave +137% or +109% for 2007–2017
  (see the ranking quirk above). A handful of trades decides every result.

### Approximate method

```powershell
.venv\Scripts\stratlib backtest-approx-prepare --start 2025-09-26 --end 2026-09-25
.venv\Scripts\stratlib backtest --approximate --start 2025-09-26 --end 2026-09-25
```

Or click **Prepare data**, then **Run backtest**, in the GUI. Preparation
makes FMP calls; it is safe to repeat and reuses what it already has. It:

1. refreshes the delisted-company directory if it predates the end date;
2. fetches profiles for delisted NYSE, Nasdaq and AMEX listings from the
   period, dropping ETFs and funds, and their price histories;
3. reads the splits calendar in 60-day pages (FMP silently truncates longer
   ranges). On Premium the calendar reaches back only about five years
   (to 2021-10-06 as of 2026-09-29). For earlier periods, each member priced
   before that date gets one `splits?symbol=` call. That history is stored
   under `backtest:splits:SYMBOL` and never refetched, because past splits
   don't change. If a stock's history can't be fetched, it is counted as
   `split_histories_unavailable`, and its old prices keep today's split
   basis;
4. ranks every member on every session and fetches statement histories for
   each stock that passed the price and RS filters on any day.

The first one-year preparation took about 6,000 calls. A run ranks the whole
market with pandas and matches `price_metrics` exactly; the tests check this
against the live functions. Scoring, base detection, market direction and
execution are the same code the strict method and the live screen use.

A run spreads its work over every processor core: each year of the market
ranking, and batches of sessions for the daily screens, run in separate
processes and are merged back in session order. `--workers N` sets the number
(code that calls the functions itself uses one process; see the note under
Screening). Each statement history's filing dates are read once per run, not
once per session. The Leaders and Trend Leaders rankings are saved, and a later
run reuses them when only the portfolio settings or the market rule differ:
holdings, the loss cap, the stop, the exposure ladder or the index rule. Any
other threshold, a different period, or new data (a backfill, a preparation, a
universe change) makes a new ranking; the six most recent are kept. Measured
on 2026-10-01 on 16 cores, with results identical to the earlier code:

| Run | Before | After |
|---|---|---|
| Breakout, one year | 102 s | 29 s |
| Leaders, one year | 45 s | 20 s |
| Trend leaders, three years | 145 s | 36 s |
| Trend leaders, three years, a holdings variant after a first run | 145 s | 11 s |

The result is labelled approximate because:

- FMP's histories include later restatements, which can make past numbers
  look cleaner than investors saw them;
- industries and sectors are today's, including for delisted stocks;
- FMP's delisted-company directory has few delistings before 2016, so
  earlier periods are mostly today's survivors (see Trend leaders, "Data
  before 2016");
- a holding that delists is sold at its last close unless documented
  proceeds were imported (see Archive format);
- a listed stock with no bar on a day drops out of that day's ranking, and a
  holding is marked at its last close through the gap but is never bought at
  a carried-forward price.

Give a run a label to find it later. With two or more saved results, the
page shows a comparison table: market rule, positions, return, drawdown, win
rate and SPY. Click a row, or choose it under Show result, to see its detail. A result lists the
thresholds that differ from current Settings, so a run made with overrides
stays readable.

The saved coverage and counts list each of these, for example how many
delisted companies were included and how many survivor-days lacked
statements. Every approximate result also records a candidate funnel. Stock-days
passing price and RS lead to those passing every C/A/S/L check, then to
breakouts, then to breakouts blocked because the market was not in a
confirmed uptrend, and finally to positions. It also records how often each
check rejected a candidate, so a run with few or no trades shows which rules
were responsible. Changing price or RS thresholds after preparing can create
survivors without statements; prepare again to fetch them.

### Strict method

```powershell
.venv\Scripts\stratlib backtest --start 2025-09-29 --end 2026-09-25 --check
.venv\Scripts\stratlib backtest --start 2025-09-29 --end 2026-09-25 --max-holdings 10 --capital 100000 --output data/backtest.json
```

The chart compares daily portfolio equity with SPY bought at the first
session's open. The table reports CAGR, maximum drawdown, win rate, average
winning and losing percentage returns, and their ratio. Undefined trade
statistics display Unavailable. Open positions remain marked at the final
close and are excluded from closed-trade statistics. Tabs show closed trades,
positions still open and skipped entries. Trade CSV and full JSON downloads
include the recorded executions; the JSON also includes signal criteria,
base measurements, threshold settings, coverage and an input checksum.

### Historical data requirements

The existing SQLite cache has today's company classifications and standard
FMP financial histories. It does **not** have a historical record of which
companies and industries were visible on each past date. Those fields cannot
be backdated without violating the specification. FMP's
[company screener](https://site.financialmodelingprep.com/developer/docs/stable/company-screener)
is a current snapshot. Its
[delisted-company endpoint](https://site.financialmodelingprep.com/developer/docs/stable/delisted-companies)
provides `symbol`, `companyName`, `exchange`, `ipoDate` and `delistedDate`, but
no dated industry history or statement vintages.

The supported alternative on the existing plan is to import archives you
already have, or collect observations going forward. No additional data
source or paid service has been added. The backtester refuses to manufacture
a historical universe from today's survivors plus their current profiles.
The Premium key successfully accessed the directory on 2026-09-28.

```powershell
# Read all delisting pages through the shared cache and rate limiter.
.venv\Scripts\stratlib backtest-delistings
# Capture fresh metadata that morning, before 13:00 Eastern. Never backdated.
.venv\Scripts\stratlib backtest-capture
# Import existing dated observations, using the format described below.
.venv\Scripts\stratlib backtest-import path/to/archive.json
# Prices and split history for the archived universe, including former listings.
.venv\Scripts\stratlib backtest-prepare --start 2025-09-29 --end 2026-09-25 --sample 50
```

Remove `--sample` for full preparation. It limits prepared tickers and makes
repeat development requests cheap. The separate `backtest --sample 50` caps
fundamental evaluations among each day's price/RS survivors. Ranks still use
the whole historical universe, and sampled results carry a development label.
Preparation does not fetch today's statements and present them as historical
vintages. Subsequent online screens retain immutable observations when
fundamentals or their split reference changes, including existing cached
bundles first observed by this version of the app.

The coverage check first verifies daily universe snapshots and the delisting
directory. During execution, missing index evidence, price history, split
reference data, historical industries or survivor statement vintages stop
the run. Failed runs preserve the previous result. Short listing histories
have no RS rating; missing bars inside an available ranking history are an
error. A dated statement bundle can still yield Unavailable criteria, such
as absent continuing income, and such candidates do not pass. Their count is
included in the result.

### Archive format

Import a UTF-8 JSON object with `schema_version: 1`. The `universes` and
`fundamentals` arrays contain observations, not reconstructed current data.
At least one must be nonempty. Reimporting identical observations is safe;
changing the contents of an existing timestamp is rejected atomically.

Each `universes` item has these fields:

| Field | Meaning |
|---|---|
| `date` | Historical session, `YYYY-MM-DD`. A snapshot is required for every test session; no forward fill. |
| `observed_at` | ISO timestamp with time zone, on that date, by 13:00 Eastern. |
| `source` | Provenance of the dated observation. |
| `complete` | Must be `true`, asserting the archive covers all listings on the configured exchanges. |
| `stocks` | Full dated listing array. Each row needs `symbol`, `exchange`, `industry`, Boolean `isEtf` and `isFund`; include `companyName` and `sector` when available. |

Use historical classifications, including securities that later delisted.
The app applies common-stock exclusions to these dated rows. The full FMP
delisting directory checks for omitted former common stocks during their
listing lifetimes. Reused tickers with conflicting lifetimes require a stable
security identity and are rejected. A source's completeness assertion cannot
be independently proved by this JSON format.

Each `fundamentals` item contains `symbol`, `observed_at`, `source`, and
`bundle`. The bundle has `income_quarter`, `income_annual`, `balance_quarter`,
`balance_annual`, `cash_quarter`, `cash_annual`, `splits` and `share_basis_date`.
Statement rows use the existing FMP field names. Preserve the complete
statement history actually observed at that timestamp, including the 12
quarters and four annual periods needed by the scoring rules. The share basis
cannot be later than the observation. Later snapshots cannot alter past
evaluations. Financial statements captured today become useful for future
backtests, even if their fiscal periods are much older.

An optional `settlements` array records documented delisting proceeds with
`symbol`, `date`, `cash_per_share`, `basis_date`, and `source`. Values must be
on the same split-adjustment basis as cached execution prices. Zero is valid
when supported by evidence. Missing proceeds are not guessed from the last
quote. If a position reaches its delisting date without a settlement, the
run stops; a missing bar before that date also stops it.

### Timing and execution conventions

- Daily close signals use only that session's archived universe, prices and
  public statement observations. The market must allow new buying (see Market
  direction). With `thresholds.buy_zone_entry_weeks` at 0 (the brief), a base
  must have its **first** valid closing breakout that day. Above 0, any close
  within that many weeks of the breakout also signals, while the stock closes
  in the buy zone (0% to `buy_zone_max_pct` above the pivot) and the breakout
  met the volume rule. These are the stocks the Screen's N checks pass.
  `config.yaml` chose 3 weeks on 2026-09-29. Each breakout is bought at most
  once, so a stopped-out or cash-raised position is not rebought from the same
  base. The fast-gain and hold windows count from the breakout, not the entry.
  A later passing fundamental cannot retroactively create an entry.
- Accepted timestamps are interpreted in Eastern time unless they include
  an offset. Date-only filing dates become usable the following day, avoiding
  after-hours leakage. When neither filing nor acceptance is present, the
  fallback is 45 days after quarter-end or 90 after annual year-end.
- The information cutoff for statements and universe observations is
  **13:00 Eastern on every session**. This is conservative on regular days
  and protects half-day sessions because the existing SPY calendar does not
  record early closing times. Afternoon filings wait until the next session.
- Signals enter at the immediate next session's open. Skip opens over the
  configured buy-zone limit, missing opening bars, already-held stocks and
  orders with no equal-sized allocation available. Order candidates by
  signal-date RS descending, then ticker, without looking at future returns.
- Each new position receives opening equity / maximum holdings. Fractional
  shares are allowed; existing positions drift in weight and are not
  rebalanced. Opening exits can fund new entries that morning; intraday stop
  proceeds cannot.
- Daily lows trigger the stop, including on entry day. Fill at the stop or
  the lower opening price when a gap crosses it. Stop losses take priority
  over profit orders and the hold exception.
- Profit targets are confirmed on closes and exit at the next open. A
  completed close gaining at least 20% from the breakout pivot within three
  calendar weeks activates the eight-week hold. The three-week boundary is
  inclusive; the hold expires on the eight-week date. Later prices cannot
  change an earlier execution. These thresholds follow Settings.
- Prices and volume are restored to the signal-date split basis before
  absolute price, volume and pivot-offset checks. Executions, reported trade
  prices and share quantities use a consistent cached split-adjusted basis.
- Results exclude commissions, slippage, taxes, cash interest and dividends.
  SPY is a price-return comparison, not a dividend-reinvested total return.
  I remains manual and is not a historical entry filter. Today's manual
  sponsorship assessments are never reused as past observations.

## Market direction and bases

All calculations use completed daily bars and the existing SQLite cache.
Phase 3 adds no API endpoints, paid services or runtime dependencies. The
market state machine, weekly aggregation, pattern detectors and N/M scoring
are pure Python functions, separate from fetching and the GUI.

### Market direction

- Track `^GSPC` and `^IXIC`. A distribution day is a close down at least 0.2%
  with volume strictly higher than the previous session. Count the last 25
  sessions. Four days put an established uptrend under pressure; five end it.
- A rally counts from its intraday low as day one. A later intraday undercut
  restarts the count. Day four or later can confirm an uptrend when the index
  closes at least 1.25% higher on higher volume. A new low on that same day
  prevents confirmation. Five distribution days also prevent confirmation.
- The history starts in correction until a follow-through is observed. The
  state stays in correction until another follow-through, even after old
  distribution days expire. Pressure relaxes when the count falls below four.
  Breaking the rally low ends an established uptrend too.
- **Graded exposure.** Each index state maps to the share of the portfolio
  that may be invested, following IBD's practice of scaling exposure rather
  than switching it on and off. `exposure_confirmed_pct` applies in a
  confirmed uptrend, and `exposure_late_confirmed_pct` with one distribution
  day short of pressure. `exposure_new_uptrend_pct` caps the first
  `exposure_new_uptrend_sessions` after a follow-through. `exposure_pressure_pct`
  applies under pressure, `exposure_heavy_pressure_pct` from
  `distribution_heavy_count` distribution days, and a correction allows 0%.
  The defaults (100% confirmed, 0% otherwise) are the brief's gate. M passes,
  and a backtest may buy, only when exposure is above 0%. A backtest fills new
  positions up to that share of the maximum holdings; with
  `backtest.raise_cash`, holdings above it are sold at the next open, lowest
  return since entry first.
- **Correction.** With `correction_drawdown_pct` above 0, distribution days
  only reach "under pressure". A correction needs a close that far below the
  uptrend's peak close, or an undercut of the rally low. At the default, 0,
  `distribution_correction_count` distribution days end the uptrend, as in
  the brief.
- **Distribution expiry.** `distribution_expiry_gain_pct` adds IBD's rule that
  a distribution day stops counting once a later close is that far above its
  close. The default, 0, keeps the brief's plain 25-session window. Expiry uses
  only closes through each session, so earlier states never change.
- **Combining the indexes.** `market_index_rule` decides how the two indexes
  combine. `both` (the default) takes the weaker state and exposure, `either`
  the stronger, and `sp500` or `nasdaq` one index alone. `average` averages
  the two exposures, rounded down to 20% steps. Insufficient evidence in a
  deciding index is **Unavailable**, not an extra market regime.
- **Settings chosen 2026-09-29.** `config.yaml` uses `average`, a 10%
  correction, 5% expiry, and the ladder 100 / 80 / 60 for 10 sessions / 40 /
  20 at 6 days / 0, with raise cash on. Over 2025-09-26 to 2026-09-25 that
  allowed new buying on 243 of 251 sessions, averaging 47% exposure. The
  brief's rule confirmed an uptrend on 1 session.
- Index volume is used when both comparison days have positive finite volume.
  Five identical consecutive index volumes also trigger the ETF fallback.
  Fallback compares both days using SPY for the S&P 500 or QQQ for Nasdaq;
  it never mixes index and ETF volume in one comparison. Prices always come
  from the index. The page and saved report disclose proxy use.
- A missing index session or unusable volume makes M unavailable until 25
  consecutive valid comparisons exist again. States are computed in date
  order; adding later data cannot alter prior states. This is a price-history
  calculation, not a claim that the current cache preserves historical data
  vintages for a backtest.

### Weekly pattern rules

The brief leaves some geometry and candidate-selection choices open. These
are the deterministic definitions used by the app:

- Aggregate Monday-Friday OHLCV. Only weeks whose calendar Friday has elapsed
  can form a base. Holiday-shortened weeks retain their actual last session
  date. A missing expected stock session invalidates that week. Weekly chart
  candles may include an unfinished week, but the detector does not use it.
- Cup with handle: the cup alone lasts at least seven weeks. Its left rim is
  the cup's high; the trough is at least two weeks from either rim. The right
  rim closes within 5% of the left rim. Depth is 12-33%, or up to 50% if a
  known combined-market correction occurred during the base. The additional
  one- or two-week handle stays entirely in the upper half, ends below its
  opening price and the right-rim close, and corrects at most 12%. This cap
  can be tightened to 8% in YAML. Its mean daily volume must be below the
  cup's, avoiding holiday-week volume distortion. Pivot is handle high + $0.10.
- Cup without handle (O'Neil's variant, off by default): set
  `cup_without_handle_min_weeks` above 0 to allow it. It uses the cup rules
  above (length, trough position, 5% right-rim recovery, depth, the correction
  allowance) with no handle, and its pivot is the left rim's high + $0.10. A
  cup with a handle is preferred when both fit. On 2026-09-29 `config.yaml`
  enabled it at seven weeks. A diagnostic over 2023-2026 candidates found that
  73% of genuine cup shapes failed the handle rule because the stock rallied
  through the left rim (median 9% above) rather than pausing in a handle.
  Cups deeper than 15% without a handle were previously undetectable. On the
  3-year backtest this pattern added 10 trades, half of them winners.
- Double bottom: at least seven weeks, two local troughs separated by a
  middle peak above both trough bars, and the second trough undercuts the
  first. The second trough is the base low. The middle peak does not exceed
  the left rim. The right side closes within 5% below the middle peak without
  already closing above it. Pivot is the middle peak.
- Flat base: at least five weeks, depth at most 15%, with the high preceding
  the low. A steady advance is not a flat base. Pivot is the base high.
  With `flat_prior_breakout_weeks` above 0 (O'Neil's second-stage base), a
  flat base also needs an earlier base of any pattern. That base must end
  within those weeks before the flat base starts, close above its pivot
  before the flat base begins, and sit at least `flat_prior_advance_pct`
  below the flat base's high. The detector then reads that much more history.
  On 2026-09-29 `config.yaml` chose 26 weeks and 20%. That removed about a
  fifth of the flat bases found among 2023-2026 candidates, leaving cups and
  double bottoms unchanged. Base detection takes about twice as long.
- Search the most recent 78 completed weeks, with bases at most 65 weeks.
  Prefer the latest end, then cup with handle, cup without handle, double
  bottom, flat, then longest candidate.
  A base ending in the preceding three weeks is retained only when a later
  closing breakout exists. A later low below the base invalidates it. These
  windows and the additional shape tolerances are configurable.
- N price passes from the pivot through 5% above it, inclusively. Below the
  pivot fails. N volume measures the **first daily close at or above the
  pivot after formation**, requiring 40% above the preceding 50 sessions'
  average. The breakout session is excluded from that average. Later volume
  cannot turn an earlier low-volume breakout into a pass. No breakout fails
  this check; missing breakout-volume evidence is unavailable.

These detectors identify candidates using the stated geometry. They do not
assess prior uptrend quality, wedging, or discretionary chart interpretation.
The optional Jev layer is described below. It does not change these rules.

## Optional Jev base reviews

Set `jev.enabled: true` in `config.yaml` and add your Vercel key as
`AI_GATEWAY_API_KEY` in `.env`. Environment values take precedence over `.env`.
There is no TypeSafe key or additional SDK dependency.

On **Stock detail**, find **Jev base review** below the base and market checks.
Press **Review with Jev** to send that stock's detected base to Vercel AI Gateway.
This is an on-demand, paid Gateway request. Opening pages, running a screen,
or running a backtest never calls Jev. If no completed base exists, the page
explains why a review cannot run. Disabled mode never loads the Gateway key.

Jev receives a JSON object with completed weekly OHLCV bars inside the base,
its measured depth, duration, pivot and handle measurements, the dated rule
verdict and relevant thresholds. Python also supplies weekly price changes
and mean-daily-volume changes, accounting for shortened trading weeks.
No future or partial weeks, fundamentals, position data or research notes
are included. Questions ask only for qualitative judgments:

- Whether the recovery shows upward wedging on weakening volume support.
- For a cup with handle, whether its downward drift looks orderly.
- For a cup with handle, whether the volume sequence supports drying volume.

Each named question uses TypeSafe's `choice` primitive, including an uncertain
option. Each judgment is shown as a full sentence alongside the measured
rule condition, its saved threshold, answer probability and the provider's
separate confidence value. For example, "orderly" is displayed as "The handle
shows a controlled downward drift." The handle rule itself checks
that its final close is below both its opening price and the cup's last close.
The volume rule compares average daily volume in the handle with the cup;
Jev judges whether the weekly sequence looks consistently contracting.
Wedging has no configured numeric rule and is labeled as judgment-only.
Existing saved reviews get these explanations without a new API request. The model is
explicitly told not to calculate measurements or recommend trades. Answers
never feed into screening verdicts, position alerts or historical trades.

### Gateway contract and version limits

Documentation and the public model catalog were checked on **2026-09-29**:

- [Vercel's Jev model page](https://vercel.com/ai-gateway/models/jev)
- [Gateway TypeSafe-compatible HTTP API](https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe)
- [Gateway evaluation API](https://vercel.com/docs/ai-gateway/modalities/evaluation)
- [TypeSafe state](https://docs.typesafe.ai/concepts/state),
  [typed question and answer schema](https://docs.typesafe.ai/api), and
  [model versions](https://docs.typesafe.ai/models)

The client posts to
`https://ai-gateway.vercel.sh/typesafe/v1/systemone` with a Bearer Gateway key
and `model`, `state` and `questions` fields. This documented Gateway endpoint
preserves TypeSafe's Choice confidence and probability fields. It does not
call TypeSafe directly or use Chat Completions. Redirects are disabled, and
the application requests no model fallback or alternate paid service.

The public Gateway catalog lists **`typesafe-ai/jev` only**. It does not list
a pinned version, and its public endpoint lookup for `typesafe-ai/jev-1.13.0`
returned HTTP 404, so the alias is the default. TypeSafe's own `jev-1.13.0`
availability does not establish that Gateway accepts it. `jev.model` permits
a versioned `typesafe-ai/jev-X.Y.Z` ID for a future Gateway-supported pin;
an unavailable ID causes an explicit error, not a fallback to the alias.

Every HTTP response records its returned model and available Gateway routing
metadata. The Gateway documentation says the top-level model can echo the
requested alias. When it does, the UI says **underlying version not reported**.
The app cannot identify a hidden model update from an alias alone. It stores
the response metadata and generation ID, when supplied, for investigation.
It never substitutes TypeSafe's current documented release as the version
that served a response. This is the Phase 6 version-tracking limitation.

### Cache and review history

Successful reviews persist in the existing SQLite database. The cache key
covers the model, prompt version, full state and all questions. A new date,
restated bars, changed base, changed relevant thresholds or changed questions
cannot reuse an old review as current. Exact matches display without a key
or network access. **Review again with Jev** explicitly makes a fresh request
for the same evidence, useful for comparing results behind a moving alias.

The `runs` table appends attempts under `jev:TICKER`, with the request,
rule verdict, response identity, answers, probabilities, confidence and call
count. The GUI exposes the most recent 20 attempts for the stock and provides
JSON downloads of successful reviews. Older attempts remain in SQLite.
Failed re-reviews preserve the last successful cache entry. A changed setup
shows old reviews only as dated history. Disabling Jev still allows reading
that history.

Timeouts and 429/5xx failures get bounded retries, defaulting to two retries
after the first attempt. Retry-After is honored up to 60 seconds; longer
requested waits end the attempt so it can be retried later. Authentication,
credit, redirect and validation errors are not retried. Invalid or incomplete
answer distributions are recorded as failed attempts and never cached as
valid advice. Credentials are sent only in headers; error messages omit
response bodies and stored response evidence redacts an echoed Gateway key.
The top bar's shared rate-limit count is explicitly FMP-only. Jev records its
own HTTP call count per review.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

Tests use recorded FMP responses in `tests/fixtures/fmp` plus explicitly
synthetic boundary cases. The parity tests import the research modules (they skip
if the checklist module's scratch-folder dependency is missing). They never call the live API; external socket
connections are blocked. Local socket pairs stay allowed, because Windows
asyncio uses them internally. The GUI tests drive the web app with NiceGUI's
simulated user, with no browser.

## FMP plan notes

Checked against FMP's documentation and pricing page on 2026-09-28, and
confirmed with a Premium key:

- The exchange-wide quote endpoint (`/stable/batch-exchange-quote`) and the
  bulk end-of-day endpoint (`/stable/eod-bulk`) require the **Ultimate** plan.
  On Premium they return HTTP 402. The universe comes from
  `/stable/company-screener` instead, and the 52-week high, moving averages,
  and average volume for the price prefilter will be computed from the stored
  daily history.
- `/stable/historical-price-eod/full` is split-adjusted (volume too) and
  returns an in-progress bar for the current session during market hours.

Phase 2 endpoints were rechecked against the current
[FMP API documentation](https://site.financialmodelingprep.com/developer/docs)
and [Premium plan](https://site.financialmodelingprep.com/developer/docs/pricing),
and exercised with the configured key on 2026-09-28:

| Stable endpoint | Parameters | Fields used |
|---|---|---|
| `income-statement` | `symbol`, `period=quarter/annual`, `limit=12/4` | `fiscalYear`, `period`, `date`, `filingDate`, `acceptedDate`, `reportedCurrency`, `revenue`, `epsDiluted`, `netIncome`, `netIncomeFromContinuingOperations`, `weightedAverageShsOutDil`, identifiable adjustment fields |
| `balance-sheet-statement` | `symbol`, `period=quarter/annual`, `limit=4` | fiscal metadata, `totalDebt`, `totalStockholdersEquity` |
| `cash-flow-statement` | `symbol`, `period=quarter/annual`, `limit=4` | fiscal metadata, `operatingCashFlow` |
| `earnings-calendar` | `from`, `to` | `symbol`, `date` |
| `splits` | `symbol` | `date`, `numerator`, `denominator` |

Premium includes full fundamentals and corporate calendars, but the
`splits-calendar` `from` date is limited to about the last five years. The
approximate backtest works around this with per-symbol `splits`. The live phase 2
sample confirmed access to all seven per-symbol requests and the calendar.
No Ultimate-only endpoint or additional paid service is used.

### Phase 2 verification, 2026-09-28

The initial full run ranked 5,061 of 5,618 stored common stocks, formed 152 industry
groups, and evaluated all 203 price/RS survivors. It used 1,383 calls in
139.8 seconds after the five-stock development sample had populated part of
the cache. The immediate full repeat used **zero calls** and took 4.1 seconds.
No stock passed every Phase 2 criterion at the default thresholds.

After correcting the stock-history query to cover SPY's full date range,
a cache-only repeat ranked **5,064** stocks in 4.0 seconds with zero API calls.
BEP, MF and NXH regained RS ratings of 46, 89 and 10. The run still evaluated
203 survivors with no stock passing every Phase 2 check. Lilly now passes
the share-count check, with a three-year change of -5.49% and latest annual
change of -0.67%; it still fails debt/equity and other checks.

Prices were through 2026-09-25. Thirteen stocks lacked that session's bar;
the remaining exclusions from RS had short or incomplete histories.
All exclusions are surfaced in data coverage. The automated suite passed
148 tests, including the extra-date RS regression, share/debt period rules,
numeric table data and saved price-filter explanations. Browser checks
confirmed ascending and descending criterion-header sorting and opening
the correct stock after sorting.

### Phase 3 verification, 2026-09-28

The completed cache-only screen ranked 5,064 of 5,618 stocks, evaluated the
same 203 fundamental survivors, and searched every stock for weekly bases.
It found 72 cups with handles, 154 double bottoms and 1,788 flat bases. These
are pattern candidates, including stocks rejected by the price or RS filters.
No stock passed all C/A/N/S/L/M checks. Prices were through 2026-09-25.

The initial implementation took 85.8 seconds. After removing repeated
candidate-date validation and limiting aggregation to the configured search
window, the full repeat took **39.3 seconds with zero API calls**, returning
the same 2,014 candidates. Both indexes were in correction in this cached
snapshot; their 25-session distribution counts were five and four. Current
index volumes were usable, so no SPY/QQQ fallback was needed in that window.

The suite passed **208 offline tests**. Added coverage includes recorded
patterns, synthetic rule boundaries, market transitions, proxy comparisons,
partial and missing weeks, breakout-volume baselines, future-bar exclusion,
chart overlays, old saved screens, and repeated stock selection. Browser
checks verified the Market page and APA's weekly cup/handle/pivot overlays
at desktop and mobile widths. The stock-selector regression found during
that check is fixed. Pandas/NumPy emit non-failing deprecation warnings during
weekly chart resampling; the arithmetic tests and chart checks pass.

### Phase 4 verification, 2026-09-28

The full suite passes **253 offline tests**. The five existing Pandas/NumPy
deprecation warnings remain confined to chart resampling.

Added offline tests for stop/target boundaries, the fast-gain window, hold
expiry, stop priority, future-bar exclusion, missing breakout evidence,
recorded FMP prices, split restatements, persistent lots, and reversible
close/reopen. Streamlit tests exercise add/edit validation, every threshold
field, saves, default loading and stale-form conflicts. No live API calls
were needed for Phase 4.

Browser checks used an isolated database of illustrative positions and
recorded prices. Positions and Settings rendered on desktop; Settings also
rendered at 390px. Final mobile Positions inspection found no viewport
overflow or application error. The shared browser's screenshot tool then
failed repeatedly, so the final visual signoff remains pending recaptures
of mobile Positions and the updated Settings layout. This limitation does
not affect the application's automated tests or local launch.

### Phase 5 verification, 2026-09-28

The suite passes **332 offline tests**, with the same five chart-resampling
deprecation warnings. New tests cover recorded FMP acceptance dates, split
history, delisting pagination and SPY bars, plus synthetic entry/exit timing,
allocation, fast-gain holds, delisting proceeds and a complete historical
screen-to-trade path. Future statement revisions and current universe edits
do not change prior trades. Failed backtests preserve the saved result.

The full delisting refresh cached **15,669 records over 158 pages**. Including
the initial endpoint verification, Phase 5 used **158 API calls**. The real
one-year coverage check, 2025-09-29 through 2026-09-25, found **250 sessions
without dated universe snapshots**. No historical performance claim was
produced from current metadata.

Browser interaction verified the Backtest page and its coverage error. DOM
checks found no app exception or viewport overflow at 1280px and 390px.
The T3 screenshot tool failed repeatedly at both sizes, so the independent
visual review requires desktop and mobile recaptures. The successful-result
view is covered by Streamlit AppTest using clearly synthetic archives.

### Phase 6 verification, 2026-09-29

The full suite passes **385 offline tests**, with the same five chart-resampling
deprecation warnings. New tests cover the Gateway HTTP contract, secret handling,
disabled mode, dated weekly evidence, typed response validation, retries, cache
invalidation, persistent review history, preserved rule verdicts and Streamlit's
on-demand controls. Jev responses are explicitly labeled synthetic contract
fixtures; weekly input tests also use recorded FMP bars. No paid Jev inference
or FMP requests were made during this initial implementation check. Live
Gateway inference had not yet been tested at that stage; see the follow-up below.

The desktop browser check exercised the actual review component against a
mocked Gateway and isolated SQLite database. It displayed disagreements beside
the passing handle rules, confidence, probabilities and the missing-version
label, with no app exception or viewport overflow at 1280px. The UI review
approved that desktop/offline scope. The browser tool rejected mobile resizing
because of its argument-schema mismatch, so mobile visual verification remains
pending. The extension uses the existing native Streamlit layout and table
scrolling rather than new CSS or breakpoints.

### Phase 6 live verification, 2026-09-29

The user enabled Jev and authorized a live test. Pressing **Review with Jev**
on APA's Stock detail page succeeded with HTTP 200 in **one application
request**, with no application retries or FMP calls. The test used cached
prices through 2026-09-25 and the 23-week cup-with-handle base ending 2026-09-04.
Gateway reported 7,288 input tokens, 128 output tokens and **$0.000306096** cost.

| Judgment | Answer | Answer probability | Confidence |
|---|---|---|---|
| Upward wedging | Uncertain | 0.48 | 0.22 |
| Handle drift | Orderly | 0.66 | 0.48 |
| Handle volume | Drying | 0.69 | 0.54 |

The GUI displayed the saved comparison and model identity. A fresh Store
instance returned the same review from SQLite with credentials and network
access explicitly blocked. The rule verdict was unchanged, and the saved
record contained no Gateway key. Navigating away and reopening Stock detail
also reused the saved result.

Gateway's routing metadata recorded an internal DigitalOcean 503 followed by
a successful TypeSafe response for the same Jev model, all behind the one
Gateway HTTP call. The returned model was `typesafe-ai/jev`; the underlying
version remains unreported. A recorded response fixture now covers this
provider-retry response and all three answers in the offline test suite.
All **54 Jev client and UI tests** pass with that recorded response included.

### Phase 6 descriptive judgments, 2026-09-29

Completed the interrupted copy update. Saved and historical Jev answers now
use full sentences, with each rule's measured prices or volume ratio below
the assessment. The original Choice values and probability distributions
remain in the review JSON. Display wording does not change the request or
invalidate saved reviews.

The full offline suite passed **386 tests**, with the same five chart-resampling
deprecation warnings. All **54 Jev tests** passed again after the final copy
edits, including reading descriptive history while Jev is disabled and
credential loading is blocked. APA's existing saved review was checked in the
browser at 1280px and 390px widths, with wrapping text, no page overflow and
no app exceptions. The UI detector reported no findings. This check made no
paid Jev requests or FMP calls.
