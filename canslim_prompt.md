# Build a CANSLIM stock screener with a GUI

I want a local Python application that screens US stocks using William O'Neill's CANSLIM method, shows the results in a GUI, and later backtests the rules. Build it in phases and stop at the end of each phase so I can review before you continue.

## Data source

- Financial Modeling Prep (FMP), Premium plan. The API key is in the environment variable `FMP_API_KEY`. Load it from a `.env` file, add `.env` to `.gitignore`, and never write the key into code, logs, or test fixtures.
- Before writing the client, read FMP's current API documentation and confirm the exact endpoints and field names. Do not rely on memory of older FMP endpoints. Confirm that each endpoint you plan to use is available on the Premium plan. If something I describe below is not available on Premium, tell me and propose an alternative instead of working around it silently.
- Premium allows 750 calls per minute. The client needs a rate limiter that stays under that, retries with backoff on 429 and 5xx responses, and logs a running count of calls made per session.
- Stay on Premium. FMP's exchange-wide quote endpoint (`batch-exchange-quote`) and bulk end-of-day endpoint (`eod-bulk`) require the Ultimate plan, so don't use them. The pipeline below uses Premium endpoints instead.
- Keep the rate limit in mind during development and testing too, since we'll be running the code repeatedly. Serve repeat requests from the cache, add a development flag that limits runs to a small sample of tickers (for example `--sample 50`), and make every script and the GUI share the same rate limiter so parallel runs can't exceed the limit together.
- Cache everything in a local SQLite or DuckDB database. Fundamentals only change when a company reports, so after the first full pull, refresh fundamentals only for companies on the current week's earnings calendar. Price history is backfilled once, then extended daily by fetching each stock's new bars (one call per stock, about 5,600 calls or 8 minutes a day).

## Screening pipeline

Run the screen in stages so the expensive per-ticker calls only happen for stocks that pass the cheap filters.

1. **Universe and price prefilter.** Use FMP's company screener (`company-screener`, one call per exchange: NYSE, NASDAQ, AMEX) to get the universe with price, average volume, sector, and industry. Compute the 52-week high and the 50-day and 200-day averages from the stored daily price history. Exclude ETFs, funds, and non-common shares; ADRs and other foreign companies listed on these exchanges stay in. Keep stocks priced at $15 or more, with enough average volume to trade, within 15% of their 52-week high, and above both moving averages.
2. **Relative Strength rating.** RS has to be ranked against the whole market, not against the prefiltered list. Compute a 1 to 99 percentile rank of 12-month price performance across the full universe, with the most recent quarter weighted double. This requires daily price history for every stock, so do a one-time backfill and then extend it daily with each stock's new bars.
3. **Fundamentals for survivors.** Pull quarterly and annual income statements, balance sheets, and cash flow statements only for stocks that pass stages 1 and 2 (RS 80 or higher).
4. **Scoring.** Evaluate every CANSLIM criterion and record the measured value and pass or fail for each one, so the GUI can show why a stock passed or failed.

## Thresholds

Put every threshold in a single config file (YAML) with these defaults, and make them editable in the GUI.

| Letter | Check | Default |
|---|---|---|
| C | Latest quarter EPS vs. same quarter prior year | Up 25% or more |
| C | EPS acceleration | Growth rate rising in at least 2 of the last 3 quarters |
| C | Latest quarter sales vs. same quarter prior year | Up 25% or more, or accelerating for the last 3 quarters |
| C | After-tax profit margin | At or near its highest level in the last 3 years |
| A | Annual EPS growth | Up 25% or more in each of the last 3 years |
| A | Return on equity | 17% or higher |
| A | Cash flow per share | At least 20% above EPS |
| N | Price position | Within 5% above a valid base's pivot point to count as buyable |
| N | Breakout volume | At least 40% above the 50-day average volume |
| S | Share count trend | Diluted weighted-average shares flat or falling over 3 years, adjusted for splits |
| S | Debt-to-equity | Flat or falling over 3 years |
| L | RS rating | 80 or higher |
| L | Industry group | Group ranks in the top 40 by 6-month performance |
| I | Institutional sponsorship | Not automated on Premium. Show a manual field I can set per stock. |
| M | Market direction | Confirmed uptrend (see below) |

Notes on specific criteria:

- Use diluted EPS from continuing operations. Flag quarters with large one-time items when the data makes them identifiable.
- For S, use diluted weighted-average shares from the income statement, not basic shares outstanding, and adjust for splits using FMP's split history.
- For L, build industry groups from FMP's industry classification, compute each group's 6-month performance from its member stocks, and rank the groups.

## Market direction (M)

- Track the S&P 500 and Nasdaq Composite. If FMP's index data lacks reliable volume, use SPY and QQQ volume as the proxy and tell me you did.
- A distribution day is an index decline of 0.2% or more on higher volume than the prior day. Count them over the last 25 sessions. Four or five within that window marks the uptrend as under pressure or ended.
- A follow-through day is day 4 or later of a rally attempt (counting from the low), with the index up at least 1.25% on higher volume than the prior day. It confirms a new uptrend.
- Output one of three states: confirmed uptrend, uptrend under pressure, correction.

## Base detection

Work on weekly bars and detect three patterns. Implement these as deterministic rules in plain Python.

- **Cup with handle.** Cup at least 7 weeks long and 12% to 33% deep (allow up to 50% when the market was in a correction during the base). Handle 1 to 2 weeks, declining 8% to 12% at most, sitting in the upper half of the cup, with volume drying up. The pivot is the handle's high plus 10 cents.
- **Double bottom.** At least 7 weeks, with the second low undercutting the first. The pivot is the high of the middle peak.
- **Flat base.** At least 5 weeks, correcting no more than 15%. The pivot is the base's high.

For each stock, output the pattern found (or none), base length, depth, pivot price, and current distance from the pivot.

## Sell rules

For positions I enter in the GUI (ticker, entry date, entry price), flag:

- Price 7% to 8% below entry: sell.
- Gain of 20% to 25%: take profits.
- Exception: if the stock gained 20% within 3 weeks of its breakout, hold for at least 8 weeks from the breakout before applying the profit rule.

## GUI

Use Streamlit with Plotly charts unless you see a strong reason for something else, in which case explain it before starting.

Pages:

1. **Market.** Current M state, distribution day count, date of the last follow-through day, and index charts with distribution days marked.
2. **Screen.** Table of results with one column per criterion showing the value and a pass or fail marker. Sortable and filterable. A button to rerun the screen, with progress and the API call count shown while it runs.
3. **Stock detail.** Daily and weekly price charts with volume, the detected base and pivot drawn on the chart, the quarterly and annual fundamentals table behind C and A, and the manual I field.
4. **Positions.** My open positions with sell-rule alerts.
5. **Settings.** Edit thresholds and save them back to the config file.
6. **Backtest.** Added in phase 5.

## Build phases

1. FMP client, rate limiter, cache, and a script that backfills the universe and price history. Report how many calls the backfill used and how long it took.
2. Screening engine for C, A, S, L, and the RS rating, plus the Screen and Stock detail pages.
3. Market direction and base detection, plus the Market page and chart overlays.
4. Sell rules and the Positions and Settings pages.
5. Backtester.
6. Optional Jev integration (described below).

Stop after each phase, summarize what you built and anything that didn't match this spec, and wait for me.

## Backtest (phase 5)

- Look-ahead bias is the main risk. Use each financial statement's filing or SEC acceptance date as the date it became public. If a record has no such date, assume quarterly results become available 45 days after quarter end and annual results 90 days after fiscal year end.
- Include delisted companies in the universe so results aren't inflated by survivorship bias. Use FMP's delisted companies endpoint.
- Build the universe and apply every filter as of each historical date. Never use current data to choose which stocks the backtest considers.
- Entry: the backtest runs on daily bars, so a breakout confirmed at the close is entered at the next day's open, and the trade is skipped if that open is more than 5% above the pivot. Only enter when the market state is confirmed uptrend.
- Exits: apply the sell rules above, using daily lows to check the stop.
- Portfolio: equal-weight positions with a configurable maximum number of holdings.
- Report CAGR, maximum drawdown, win rate, average gain, average loss, the gain-to-loss ratio, and comparison against SPY buy-and-hold over the same period. Show the equity curve and the trade list in the GUI.

## Optional Jev layer (phase 6)

TypeSafe AI's Jev model returns typed decisions with calibrated probabilities. It takes JSON state and a set of named questions. I access Jev through Vercel AI Gateway, not TypeSafe's API directly. Read the current Vercel AI Gateway documentation for the Jev model ID and request format before implementing, and read TypeSafe's documentation for how Jev expects state and questions to be structured. Use it only for judgment calls the base rules can't settle, such as whether a handle drifted down on drying volume or whether a base shows wedging. Send it the weekly bars and the computed base measurements as JSON. Do not ask it to do arithmetic or measure the chart.

- Keep it disabled by default behind a config flag, with my Vercel key in `AI_GATEWAY_API_KEY`. There is no TypeSafe key.
- Pin a specific model version rather than `jev-latest` if the gateway allows it. If it doesn't, log the model version returned with every response so I can tell when results shift.
- Log its answers and confidence next to the rule-based verdict so I can compare them. It should never override the rules on its own.

## Engineering requirements

- Python 3.11 or later, with dependencies in `pyproject.toml`.
- Keep the scoring, base detection, and market direction logic as pure functions separate from the data client and the GUI, so they can be tested and reused by the backtester.
- Write pytest tests for every criterion and pattern detector using recorded API responses as fixtures. Tests must never call the live API.
- Include a README covering setup, the `.env` file, the first backfill, and how to run the GUI.
- Ask me before adding any other paid service or data source.
