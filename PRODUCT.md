# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The owner of this local Python workspace, researching US-listed common stocks.

## Product Purpose

Research stock strategies, review backtest evidence, screen current candidates,
and track model allocations and actual holdings in one local workspace.
CANSLIM remains a supported strategy alongside Trend Leaders and the six research
strategies described below.

## Brand Commitments

Recorded 2026-09-30, after a NiceGUI prototype round:
- **Direction:** the owner wants the category standard, a TradingView-grade dark trading dashboard, executed with full craft. Craft references are TradingView and Robinhood/Public.
- **Theme:** dark first.
- **Rejected looks:**
  - The broadsheet look: too plain and old-fashioned.
  - The Metro look: too loud. A giant headline, solid colour blocks and neon saturation are out.
- **Kept:** CANSLIM letter marks on result rows, in calm colours.
- **Adopted:** the owner approved the dashboard and had every page moved to it; on 2026-10-01 the Streamlit app was retired and the web app (`stratlib web`) became the only interface.

## Operating Context

Python 3.11+, a NiceGUI web app with ECharts charts, FMP Premium, and a local SQLite cache.
Daily prices are updated separately with the backfill command. The app reads
saved results without making API requests until Run screen is pressed.

## Capabilities and Constraints

Phase 5 adds daily backtesting, portfolio accounting, a SPY comparison and
the Backtest page. Strict runs require dated universe and statement archives;
current metadata cannot be backdated, and the local archive starts on
2026-09-29. On 2026-09-29 the user chose to add the specification's
filing-date method as a second, clearly labelled approximate mode: today's
FMP statement histories dated by filing, today's industries, and delisted
companies from FMP's directory. It is the default on the Backtest page.
Later on 2026-09-29 the user adopted three rule changes. Each is a setting;
the code defaults remain the specification's reading.

- C/A/S/L passes when quarterly EPS, sales and RS pass plus 5 of the other 8
  checks (unmeasurable checks left out).
- A graded market rule: IBD-style exposure from 0% to 100%, both indexes
  averaged. Distribution days reach only "under pressure"; a correction needs
  a 10% drop from the uptrend's peak. Distribution days expire after a 5% rally.
- Backtests raise cash when exposure falls.

Live position alerts use daily closes; backtest stops use lows and opening
gaps. The user selected a 7% stop and 20% profit target. Stop at Phase 5 for review before optional Jev work.
No extra data source or paid service is authorized.

On 2026-09-30 the user approved generalizing Screen, Portfolio, Market and
Positions around a shared strategy and variant. The first supported variants
are CANSLIM / Breakout trades and Trend Leaders / Long trend. Screen snapshots
are retained separately by strategy, variant and date. Market observations are
shared; exposure and liquidation policy are identified as strategy rules.
Portfolio shows a model plan alongside the selected strategy's recorded holdings
and manually recorded cash. On 2026-10-01 the owner asked for it to open with a hypothetical portfolio: what the strategy says to buy today, from an editable starting amount ($100,000 by default), following the market rule, rebuilt on every visit and never saved. Missing quantities or cash never imply zero.
Positions save their own rule settings; switching the workspace or changing
Settings does not change them. Existing entries remain explicit legacy CANSLIM
positions until the owner assigns saved rules.

On 2026-09-30 the owner asked that the other strategies be selectable too. Qullamaggie
Breakout, Minervini VCP, Episodic Pivot, the Traveling Trader 9/21 EMA trade and
checklist, and the Nash quality screen now have live screens, model plans, market
filters, saved-rule positions with exit alerts, and a Backtest view of their saved
research runs. Their rules are ports of the research code, checked against it by
parity tests and against the published trades. Each strategy's research evidence
(mostly trailing SPY) is shown beside its rules. The index timing rules and the put
hedge pick no stocks and stay research-only.

Reports are navigable research posts. Major experiments have separate posts;
minor variations are compared within the same report. A report becomes a supported
live strategy only when its rules are implemented and registered in strategies.py.

On 2026-10-08 the owner asked for TradeTest, so visitors can do what the owner did
on the supply/demand marking page: ten random daily charts (random instrument and
period from the data on hand) to mark up with channels, trendlines, rays, price and
time levels and labelled zones, and to trade bar by bar, every trade with an entry,
a stop and a target; the instrument and dates stay hidden until a chart is finished,
there is no rewinding, and a report can be downloaded at the end. The owner was away
while it was built; the fill and scoring rules, the window bank's filters and the
extras (guesses, running score, zone presets) were the builder's choices, awaiting
the owner's review.

## Evidence on Hand

The local cache contains real FMP prices. Recorded FMP fixtures exercise the
scoring code. Synthetic boundary cases in tests are labeled as such.
