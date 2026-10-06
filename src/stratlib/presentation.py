"""Wording, ordering and formatting for the web app's pages, kept apart from the UI framework so they can be
tested without a browser and reused by any page."""

from __future__ import annotations

import math

from .scanning import SCANNERS
from .strategies import STRATEGIES

# Screen rows ---------------------------------------------------------------------------------------------------

LABELS = {
    "c_eps": "C · EPS growth", "c_acceleration": "C · Acceleration", "c_sales": "C · Sales growth",
    "c_margin": "C · Margin", "a_eps": "A · EPS growth", "a_roe": "A · ROE",
    "a_cash": "A · Cash flow", "s_shares": "S · Shares", "s_debt": "S · Debt/equity",
    "l_rs": "L · RS", "l_industry": "L · Industry",
    "n_position": "N · Pivot distance", "n_volume": "N · Breakout volume", "m_direction": "M · Market",
}
# Tile and pill names for each check.
SHORT = {
    "c_eps": "EPS growth", "c_acceleration": "Acceleration", "c_sales": "Sales growth", "c_margin": "Margin",
    "a_eps": "Annual EPS, low", "a_roe": "ROE", "a_cash": "Cash flow", "s_shares": "Shares, 3 yr",
    "s_debt": "Debt/equity", "l_rs": "RS", "l_industry": "Industry rank", "n_position": "From pivot",
    "n_volume": "Breakout volume", "m_direction": "Market",
}
# The acronym's own order.
CANSLIM = [("C", "Current earnings"), ("A", "Annual earnings"), ("N", "New high"), ("S", "Supply"),
           ("L", "Leader"), ("M", "Market")]
STAGES = ["Pass", "Fail", "Unavailable", "Price filter", "RS filter"]
FILTERED = {"Price filter", "RS filter"}


def format_value(key, value):
    if value is None:
        return "Unavailable"
    if key == "m_direction":
        return str(value)
    if isinstance(value, list):
        return " / ".join(f"{v:.2f}" if key == "s_debt" else f"{v:+.1f}%" for v in value)
    if key in {"c_acceleration", "l_rs", "l_industry"}:
        return f"{value:g}"
    if key == "s_debt":
        return f"{value:.2f}"
    return f"{value:.1f}%"


def verdict(passed):
    return "Pass" if passed is True else "Fail" if passed is False else "Unavailable"


def tone(word: str) -> str:
    return {"Pass": "pass", "Fail": "fail"}.get(word, "none")


def criterion_number(item):
    """Scalar measurement shared by the table headings, the Sort control and the scorecard."""
    value = item["value"]
    if item["key"] == "m_direction":
        return {"correction": 0, "uptrend under pressure": 1, "confirmed uptrend": 2}.get(value)
    if isinstance(value, list):
        if not value:
            return None
        if item["key"] == "a_eps":
            return min(value)
        if item["key"] == "s_shares":
            # Screens saved before the three-year rule stored three annual rates.
            return 100 * (math.prod(1 + v / 100 for v in value) - 1)
        return value[0]
    return value


def short_value(item) -> str:
    """A check's measurement as a tile shows it."""
    key, value = item["key"], item["value"]
    if key == "m_direction":
        return str(value).capitalize() if value else "No data"
    number = criterion_number(item)
    if number is None:
        return "No data"
    if key in {"c_acceleration", "l_rs", "l_industry"}:
        return f"{number:g}"
    if key == "s_debt":
        return f"{number:.2f}"
    return f"{number:+.1f}%"


def sort_screen_rows(rows, field, descending):
    """Numeric criterion sorting, with unavailable values always last."""
    def value(row):
        if field == "Price":
            return row["prices"]["close"]
        if field == "RS":
            return row["rs"]
        item = next((c for c in row["criteria"] if LABELS[c["key"]] == field),
                    {"key": "", "value": None})
        return criterion_number(item)
    return sorted(rows, key=lambda row: (value(row) is None,
                  -(value(row) or 0) if descending else (value(row) or 0), row["symbol"]))


def scope_rows(report, scope):
    candidates = {r["symbol"] for r in report.get("candidates", [])}
    for row in report["rows"]:
        if scope == "survivors" and row["stage"] in FILTERED:
            continue
        if scope == "casl" and not row["phase2_pass"]:
            continue
        if scope == "all_rules" and not row.get("phase3_pass"):
            continue
        if scope == "candidates" and row["symbol"] not in candidates:
            continue
        yield row


def funnel_steps(report) -> list[tuple[str, str, int]]:
    """The screen's funnel from the universe to entry candidates, as (scope, label, count)."""
    steps = [("all", "All stocks", report["universe_count"]), ("survivors", "Survivors", report["survivor_count"]),
             ("casl", "Pass C/A/S/L", report["pass_count"])]
    if report.get("phase") == 3:
        steps += [("all_rules", "Pass C/A/N/S/L/M", report["phase3_pass_count"]),
                  ("candidates", "Entry candidates", len(report.get("candidates", [])))]
    return steps


def letter_states(row) -> list[tuple[str, str]]:
    """Each CANSLIM letter's state for a row: fail when any check fails; pass when every check passes;
    partial when the measured checks pass but some had no data; none when nothing was measured."""
    states = []
    for letter, _ in CANSLIM:
        items = [c for c in row["criteria"] if c["letter"] == letter]
        if any(c["passed"] is False for c in items):
            states.append((letter, "fail"))
        elif items and all(c["passed"] is True for c in items):
            states.append((letter, "pass"))
        elif any(c["passed"] is True for c in items):
            states.append((letter, "partial"))
        else:
            states.append((letter, "none"))
    return states


def change_pct(series: list[float]) -> float | None:
    return 100 * (series[-1] / series[0] - 1) if len(series) > 1 and series[0] else None


# Scan screens --------------------------------------------------------------------------------------------------

COUNT_LABELS = {"trend_template": ("Pass trend template", "Stage 2 uptrend, RS and price"),
                "setups": ("Setups", "on the last close"),
                "live_orders": ("Live buy stops", "placed in the last sessions"),
                "gap_and_volume": ("Gap and volume", "on the last close"),
                "strong_close_and_liquid": ("Strong close, liquid", "before the signal day"),
                "liquid": ("Non-financial", "of the liquid stocks"),
                "with_statements": ("With statements", "cached and recent"),
                "evaluable": ("Evaluable", "every test measurable"),
                "pass_checklist": ("Pass the checklist", "all five tests"),
                "pass_rules": ("Pass every rule", "cash, growth, margin, expenses"),
                "parent": ("S&P 500 companies", "scored at the review"),
                "selected": ("Selected", "at the review"),
                "coverage_pct": ("Coverage, %", "of the S&P 500's market cap"),
                "held": ("Held now", "after any that left the S&P 500"),
                "index_held": ("Index holdings", "as iShares holds them"),
                "rebuild_held": ("In the rebuild", "this app's S&P 500 rebuild"),
                "both": ("Held by both", "the index and the rebuild"),
                "rebuild_overlap_pct": ("Rebuild match, %", "of the index's weight")}
MONEY_KEYS = {"pivot", "stop", "ema_fast", "ema_slow", "close", "entry_price"}
WEIGHT_KEYS = {"weight_pct", "target_pct", "parent_weight_pct", "rebuild_pct"}
ENTRY_NOTES = {"stop": "Buy stops rest at the pivot for the next session or sessions left; the research fills at the pivot, or at "
                       "the open on a gap above it. The stop shown is for a fill at the next session.",
               "close": "The research buys at the signal session's close. A screen run after the close can only act on the next "
                        "session, so a late buy pays the overnight move."}
REBALANCE_NOTE = ("The research rebalances on the first session of each calendar quarter, buying at that close. A screen run "
                  "between rebalances shows today's selection; holdings are compared with it in Positions.")
TABLE_NOTES = {
    "nash_quality": "Ranked by Rule of 40, highest first; holdings that still pass are kept. Weight is an equal slot.",
    "tt_checklist": "The passers with the best 63-session return; the rest are listed below. Weight is an equal slot.",
    "msci_garp": "Every holding of the index, by today's weight.",
}
TABLE_NOTE = ("Ranked by 63-session return, the research's tie-break when signals outnumber open slots. "
              "Weight is the research's risk-based size.")


# MSCI GARP's results come from a rebuild of the index, not from MSCI: shown with its backtest everywhere.
INDEX_DATA_TITLE = "Not MSCI's own results: a rebuild of its rules on the data available here."
INDEX_DATA_NOTE = ("MSCI's methodology is followed rule by rule, but some of its inputs are not available: analysts' "
                   "forecasts (half of the growth score), the MSCI USA universe with free-float weights, GICS sectors "
                   "and MSCI's own company data. From December 2015 the rebuild returned 16.0% a year against 17.6% for "
                   "MSCI's official index, with 3% tracking error, and at the August 2026 review its holdings matched the "
                   "real index's for about two-thirds of their weight. The research report's ETF results use MSCI's "
                   "official index levels instead.")
INDEX_DATA_SHORT = ("A rebuild of MSCI's rules without MSCI's forecast and universe data: it trailed MSCI's official index "
                    "by about 1.6 points a year from December 2015.")
INDEX_NOTE = ("The index changes only at its quarterly reviews, which take effect at the last session of February, May, "
              "August and November: copying it means rebalancing every holding to the new weights at that close. Between "
              "reviews a stock that leaves the S&P 500 is sold and nothing is bought.")


def index_source(report: dict | None) -> tuple[str, str, list[str]]:
    """Where an index strategy's holdings come from: (title, tone, lines) for the Screen's card."""
    summary = (report or {}).get("summary") or {}
    if summary.get("source") == "ishares":
        return ("Live index holdings", "up",
                [f"MSCI USA Quality GARP Select as the iShares GARP ETF held it on {summary.get('as_of')}",
                 "From iShares' daily holdings file. Scores and tilts: this app's rebuild."])
    return ("Rebuilt holdings", "none",
            ["The index's published holdings were not available",
             "These come from this app's rebuild of MSCI's rules, not from MSCI"])


def index_table_note(report: dict | None) -> str:
    """An index strategy's table note: what the rows are, then how the index changes."""
    summary = (report or {}).get("summary") or {}
    if summary.get("source") == "ishares":
        head = (f"The index's actual holdings and weights, from the iShares GARP ETF's published holdings of "
                f"{summary.get('as_of')}. Scores, tilts and the rebuild's weights come from this app's rebuild of MSCI's "
                "rules; stocks outside the S&P 500 have none.")
    else:
        head = ("Rebuilt holdings: the index's published holdings were not available, so these come from this app's "
                "rebuild of MSCI's rules, which matched about two-thirds of the real index's weight in August 2026.")
    return f"{head} {INDEX_NOTE}"


def scan_note(spec) -> str:
    """When and how the research would act on this scan's candidates."""
    if spec.id == "msci_garp":
        return INDEX_NOTE
    return REBALANCE_NOTE if spec.id in TABLE_NOTES else ENTRY_NOTES[SCANNERS[spec.id].entry]


def shown(key, value):
    """A scan field as written in a table or tile."""
    if value is None:
        return None
    if key in MONEY_KEYS:
        return f"${value:,.2f}"
    if key in WEIGHT_KEYS:
        return f"{value:.2f}%"
    if key.endswith("_pct"):
        return f"{value:.1f}%"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return f"{value:,.0f}" if isinstance(value, (int, float)) and key == "volume_needed" else str(value)


# Strategy rules ------------------------------------------------------------------------------------------------

UNSUPPORTED = ("The index dip ladder, VIX and seasonal timing rules and the put hedge stay research-only: they pick no "
               "stocks, so they have no screen, positions or exit alerts to show.")


def _qullamaggie(p, dollar):
    return (f"Price leaders that rose at least {p.prior_move_pct:g}% within {p.lookback} sessions, then consolidated for "
            f"{p.consolidation_min} to {p.consolidation_max} sessions without falling more than {p.max_depth_pct:g}% from the high. "
            f"The last {p.tight_sessions} sessions' range is under {p.tight_range_pct:g}% of the close, the close is above the "
            f"{p.sma_fast}- and {p.sma_slow}-day averages with the {p.sma_slow}-day rising, and the {p.adr_sessions}-day average "
            f"daily range is at least {p.min_adr_pct:g}%. A buy stop sits at the highest high since the high close for "
            f"{p.order_life} sessions (a newer setup replaces it). The stop is the prior session's low; the trade is skipped when "
            f"that is more than {p.max_stop_adr:g} average daily ranges below the entry. Sell {p.partial_fraction:.0%} at the close "
            f"of session {p.partial_session} and raise the stop to the entry; sell the rest at the first close under the "
            f"{p.trail_sma}-day average.")


def _minervini(p, dollar):
    exit_text = ("Sell a third at the close of session 3 and raise the stop to the entry; sell the rest at the first close under "
                 "the 10-day average." if p.exit == "c10" else
                 "Sell half at +20%; sell the rest at the first close under the 50-day average.")
    return (f"Stage 2 trend template (close above the 50-, 150- and 200-day averages in order, the 200-day rising, at least 30% "
            f"above the 52-week low and within 25% of the high) with a relative-strength percentile of at least {p.rs_min_pct:g}. "
            f"A volatility contraction pattern: at least {p.min_pullbacks} pullbacks, each shallower than the last (swings confirmed "
            f"after a {p.swing_pct:g}% reversal), the last no deeper than {p.final_max_pct:g}% and on volume below "
            f"{p.volume_dry:g} x the 50-day average. A buy stop sits at the last swing high for {p.order_life} sessions; the "
            f"breakout day must trade at least {p.breakout_volume:g} x the 50-day average volume or the research sells it at that "
            f"close. The stop is the low of the last pullback, at most {p.max_stop_pct:g}% below the entry. {exit_text}")


def _episodic(p, dollar):
    exits = {"c10": "Sell a third at the close of session 3 if it is above the entry and raise the stop to the entry; sell the rest "
                    "at the first close under the 10-day average.",
             "time20": "Sell at the close of session 20.",
             "trail10": "Hold until session 20, then sell at the first close under the 10-day average."}
    return (f"A stock that opens at least {p.gap_pct:g}% above the prior close on at least {p.volume_mult:g} x its "
            f"{p.volume_sessions}-day average volume"
            + (", the first session after an earnings release" if p.earnings else "")
            + (f", having risen under {p.neglect_max_pct:g}% in the prior {p.neglect_sessions} sessions" if p.neglected else "")
            + f", and closes above its open in the upper {100 - 100 * p.close_in_range:g}% of the day's range. Liquidity is measured "
              f"before the signal day. Buy the close; the stop is that day's low. {exits[p.exit]}")


def _ema(p, dollar):
    return (f"The {p.ema_fast}-day EMA above the {p.ema_slow}-day EMA, the {p.ema_slow}-day EMA above the {p.sma_mid}-day average "
            f"and the close above the {p.sma_long}-day average; the highest close of the last {p.high_recent} sessions is also the "
            f"highest of the last {p.high_lookback}. A fresh pullback: the low touches the {p.ema_fast}-day EMA, the close holds the "
            f"{p.ema_slow}-day EMA, and the previous {p.fresh_sessions} lows were above the {p.ema_fast}-day EMA. Buy the close; the "
            f"stop is {p.stop_pct:g}% below the {p.ema_slow}-day EMA. Sell at the "
            + ("first" if p.exit_rule == "first" else "second") + f" close below the {p.ema_slow}-day EMA.")


def _checklist(p, dollar):
    gates = {"none": "", "stock": f" Entries also need the stock above its {p.trend_sessions}-day average.",
             "market": f" Entries also need SPY above its {p.trend_sessions}-day average.",
             "both": f" Entries also need the stock and SPY above their {p.trend_sessions}-day averages."}[p.trend_gate]
    return (f"Non-financial stocks whose trailing P/E is under their own median over the last twelve quarters, whose trailing PEG "
            f"(P/E over net-income growth) is at most {p.peg_max:g}, with ROIC of at least {p.roic_min_pct:g}%, debt/equity under "
            f"{p.debt_equity_max:g} and free cash flow above its level a year ago, which was above its level two years ago. "
            f"Statements count from their filing date and go stale after {p.stale_days} days. The {p.top_n} passers with the best "
            f"63-session return are selected at each quarterly rebalance; a holding outside the selection is sold then. "
            f"The stop is {p.stop_pct:g}% below entry and is not trailed.{gates}")


def _nash(p, dollar):
    margin = "free-cash-flow" if p.margin == "FCF" else "operating"
    guards = (f" Data guards from the research's cleanup: one reporting currency across the eight quarters, free-cash-flow margin "
              f"within ±100% and prior-year revenue of at least {dollar}{p.min_prior_revenue_m:g}M." if p.data_guards else "")
    return (f"Non-financial companies with more cash than debt (leases excluded), trailing revenue growth of at least "
            f"{p.min_revenue_growth_pct:g}%, {margin} margin of at least {p.min_margin_pct:g}% and revenue growing faster than "
            f"operating expenses" + (", outside cyclical industries" if p.exclude_cyclicals else "") + f".{guards} Up to {p.slots} "
            "holdings, ranked by Rule of 40 (revenue growth plus free-cash-flow margin). A holding that still passes is kept at "
            "each quarterly rebalance. There is no price stop.")


def _garp(p, dollar):
    growth = ("trailing-year EPS growth (standing in for analysts' short-term forecast; the long-term forecast is "
              "unavailable and left out, as MSCI's rule for missing data does), the internal growth rate "
              "(ROE x (1 - payout)) and the five-year trends of EPS and sales per share"
              if p.growth_variant == "proxy" else
              "the internal growth rate (ROE x (1 - payout)) and the five-year trends of EPS and sales per share (both "
              "analysts' forecasts are unavailable and left out, as MSCI's rule for missing data does)")
    return (f"Each quarter the S&P 500's companies are scored on growth: the average of market-cap-weighted z-scores of "
            f"{growth}. They are taken in growth order until they cover {p.coverage_pct:g}% of the S&P 500's market cap; "
            f"past {p.buffer_low_pct:g}%, current holdings ranked up to {p.buffer_high_pct:g}% come first. Value (earnings, "
            f"book and cash-flow yields) and quality (return on equity, low debt and steady earnings), each ranked within "
            f"its sector, set a tilt: 3.5, 2.5, 1.5 or 0.5 by quality quarter, halved outside the better half by value and "
            f"doubled outside the largest names. Weight is the company's share of the S&P 500's market cap times its "
            f"tilt, capped at {p.max_issuer_pct:g}% per company with each sector within {p.sector_band_pct:g} points of its "
            f"share of the selected stocks. Reviews take effect at the last session of February, May, August and "
            f"November on data as of the month before. Between reviews a stock that leaves the S&P 500 is sold and "
            f"nothing is bought. There is no price stop.")


SCAN_TEXT = {"qullamaggie": _qullamaggie, "minervini": _minervini, "episodic_pivot": _episodic, "ema_pullback": _ema,
             "tt_checklist": _checklist, "nash_quality": _nash, "msci_garp": _garp}


def rules_text(spec, settings, *, dollar: str = "$") -> str:
    """The strategy's rules in one paragraph. Markdown that typesets math needs dollar="\\$"."""
    t, p = settings.thresholds, settings.backtest
    if spec.id == "trend":
        return (f"Price filters, RS ≥ {max(t.rs_min, p.trend_min_rs)}, industry rank ≤ {t.industry_top}, "
                f"quarterly sales growth ≥ {p.trend_min_sales_growth_pct:g}%, "
                f"daily trading value ≥ {dollar}{p.trend_min_dollar_volume_m:g}M. Rank by RS, sales growth, then ticker. "
                f"Exit below the {t.long_ma_sessions}-session moving average" +
                (f" or at a {p.trend_loss_cap_pct:g}% loss." if p.trend_loss_cap_pct else ". No fixed loss cap."))
    if spec.id == "msci_garp":
        return (_garp(settings.strategies[spec.id], dollar) + " Rebuilt from MSCI's rules (research/mscigarp_index.py) on "
                "the S&P 500, which stands in for the MSCI USA Index, with a substitute sector classification in place of "
                "GICS.")
    if spec.scan:
        params = settings.strategies[spec.id]
        return (SCAN_TEXT[spec.id](params, dollar) + f" Universe: US common stocks with a close of at least "
                f"{dollar}{params.min_price:g} and a 20-session average dollar volume of at least "
                f"{dollar}{params.min_dollar_volume_m:g}M. Signals rank by 63-session return.")
    return (f"Pass the price and C/A/S/L rules, with a volume-confirmed breakout in its buy zone. "
            f"Entries remain eligible for {t.buy_zone_entry_weeks} weeks after breakout. "
            f"Rank by RS, then ticker. Close-based alerts: {t.stop_loss_pct:g}% loss limit, "
            f"{t.profit_target_pct:g}% profit target and the fast-gain hold exception.")


def sizing_text(spec, settings) -> str:
    p = settings.backtest
    if spec.id == "msci_garp":
        params = settings.strategies[spec.id]
        return (f"Sizing follows the index: each holding's share of the S&P 500's market cap times its tilt, capped at "
                f"{params.max_issuer_pct:g}% per company. The rebuild has held 89 to 232 stocks at its reviews since 2015.")
    if spec.scan:
        params = settings.strategies[spec.id]
        if spec.id in ("tt_checklist", "nash_quality"):
            slots = params.top_n if spec.id == "tt_checklist" else params.slots
            return (f"Sizing follows the research: equal weights of {100 / slots:g}%, up to {slots} holdings "
                    "(the research splits cash equally among new entries).")
        return (f"Sizing follows the research: each trade risks {params.risk_pct:g}% of equity between entry and stop, capped at "
                f"{params.max_position_pct:g}% of equity in one position, with up to {p.max_holdings} holdings.")
    return f"Equal initial slots of {100 / p.max_holdings:g}%, up to {p.max_holdings} holdings."


NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")


def strategy_count(offset: int = 0) -> str:
    """How many workspace strategies there are (less ``offset``), as a word."""
    n = len(STRATEGIES) - offset
    return NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)


def supported_names() -> str:
    return ", ".join(f"{s.name} / {s.variant_name}" for s in STRATEGIES.values())


# Stock detail ---------------------------------------------------------------------------------------------------

STATEMENT_LABELS = {
    "date": "Period end", "fiscalYear": "Fiscal year", "period": "Period", "reportedCurrency": "Currency",
    "revenue": "Revenue, millions", "epsDiluted": "Reported diluted EPS", "netIncomeFromContinuingOperations": "Continuing income, millions",
    "weightedAverageShsOutDil": "Diluted shares, millions", "otherAdjustmentsToNetIncome": "Other income adjustments, millions",
    "filingDate": "Filed", "totalDebt": "Total debt, millions", "totalStockholdersEquity": "Equity, millions",
    "operatingCashFlow": "Operating cash flow, millions", "freeCashFlow": "Free cash flow, millions",
}
# Each statement's document prefix, heading and columns.
STATEMENTS = [
    ("income", "Income statement", ["date", "fiscalYear", "period", "reportedCurrency", "revenue", "epsDiluted",
                                    "netIncomeFromContinuingOperations", "weightedAverageShsOutDil",
                                    "otherAdjustmentsToNetIncome", "filingDate"]),
    ("balance", "Balance sheet", ["date", "reportedCurrency", "totalDebt", "totalStockholdersEquity"]),
    ("cash", "Cash flow", ["date", "reportedCurrency", "operatingCashFlow", "freeCashFlow"]),
]


def statement_value(field, value):
    """A statement figure as stored, with totals in millions."""
    label = STATEMENT_LABELS[field]
    return value / 1_000_000 if label.endswith(", millions") and isinstance(value, (float, int)) else value


# Jev reviews ----------------------------------------------------------------------------------------------------

JEV_LABELS = {"wedging": "Price rising on weakening volume", "handle_drift": "Downward handle pullback",
              "handle_volume": "Lower trading volume in the handle"}

# Expand the saved Choice values for display without changing the request or cache key.
JEV_ANSWERS = {
    "wedging": {
        "present": "The recovery appears to rise on weakening volume, a potential warning sign.",
        "absent": "No clear upward recovery on weakening volume is evident.",
        "uncertain": "Cannot determine whether the recovery rises on weakening volume.",
    },
    "handle_drift": {
        "orderly": "The handle shows a controlled downward drift.",
        "erratic": "The handle shows erratic selling.",
        "uncertain": "Cannot determine whether the handle's pullback is controlled or erratic.",
    },
    "handle_volume": {
        "drying": "Trading volume appears to contract consistently across the handle.",
        "mixed": "The volume sequence does not convincingly show consistent contraction.",
        "uncertain": "Cannot determine whether the handle's volume contraction is consistent.",
    },
}


def comparison_rows(result):
    """Each Jev judgment beside the numeric rule evidence saved with the same review."""
    state = result["request"]["state"]
    base = state["base_measurements"]
    weeks = state.get("weekly_bars", [])
    handle = [w for w in weeks if w.get("segment") == "handle"]
    cup = [w for w in weeks if w.get("segment") == "base"]
    drift = "Unavailable: this saved review lacks the handle prices needed for comparison."
    if handle and cup:
        closing, opening, rim = handle[-1]["close"], handle[0]["open"], cup[-1]["close"]
        status = "Pass" if closing < opening and closing < rim else "Fail"
        drift = (f"{status}: the handle's final close was USD {closing:,.2f}; it must be below "
                 f"both its opening price of USD {opening:,.2f} and the cup's last close of USD {rim:,.2f}.")
    ratio = base.get("handle_volume_ratio")
    limit = state.get("rule_thresholds", {}).get("handle_volume_max_ratio")
    volume = "Unavailable: this saved review lacks the volume ratio or its threshold."
    if ratio is not None and limit is not None:
        status = "Pass" if ratio < limit else "Fail"
        volume = (f"{status}: the handle's average daily volume was {ratio:.1%} of the cup's average. "
                  f"The saved rule requires less than {limit:.1%}.")
    evidence = {
        "wedging": "Not automated: no numeric wedging rule is configured, so this is Jev's judgment only.",
        "handle_drift": drift + " This rule checks the net decline, not how smooth the pullback is.",
        "handle_volume": volume + " This rule compares averages; it does not require volume to fall every week.",
    }
    return [{"Judgment": JEV_LABELS[name], "Rule evidence": evidence[name],
             "Jev answer": JEV_ANSWERS[name][answer["choice"]],
             "Confidence": answer["confidence"],
             "Answer probability": answer["probabilities"][answer["choice"]]}
            for name, answer in result["answers"].items()]


# Positions -------------------------------------------------------------------------------------------------------

# Sell alerts, most urgent first.
ALERTS = ["Sell", "Take profits", "Review breakout", "Unavailable", "Hold exception", "Hold"]
STOP_HELP = {
    "qullamaggie": "Leave empty to use the low of the session before entry, the research's stop.",
    "minervini": "Leave empty to use the widest stop the rules allow, below entry. The pattern's own low is not stored, "
                 "so record it for an exact alert.",
    "episodic_pivot": "Leave empty to use the low of the entry session, the research's stop.",
    "ema_pullback": "Leave empty to use the research stop, below the 21-day EMA at entry.",
    "tt_checklist": "Leave empty to use the research stop, below the entry price and not trailed.",
}


def entry_defaults(spec, settings) -> list[tuple[str, str, str | None]]:
    """Headline exit settings for new entries under a strategy, as (label, value, note)."""
    t = settings.thresholds
    if spec.id == "canslim":
        return [("Default stop loss", f"{t.stop_loss_pct:g}%", "new CANSLIM entries"),
                ("Default profit target", f"{t.profit_target_pct:g}%", "new CANSLIM entries"),
                ("Fast gain", f"{t.fast_gain_pct:g}%", f"in {t.fast_gain_weeks} weeks"),
                ("Hold", f"{t.minimum_hold_weeks}", "weeks from breakout")]
    if not spec.scan:
        cap = settings.backtest.trend_loss_cap_pct
        return [("Default trend line", f"{t.long_ma_sessions}", "sessions; new Trend Leaders entries"),
                ("Default loss cap", f"{cap:g}%" if cap else "None", None)]
    from .exits import exit_rule
    params = settings.strategies[spec.id]
    if spec.id == "tt_checklist":
        return [("Initial stop", f"{params.stop_pct:g}%", "below entry, not trailed"),
                ("Quarterly rebalance", f"Top {params.top_n}", "sold when outside the selection")]
    if spec.id == "nash_quality":
        return [("Stop", "None", "the strategy has no price stop"),
                ("Quarterly rebalance", "Every rule", "sold when it fails a rule")]
    if spec.id == "msci_garp":
        return [("Stop", "None", "the index has no price stop"),
                ("Quarterly review", "Feb, May, Aug, Nov", "sold when it leaves the index")]
    rule = exit_rule(spec.id, params)
    kind = rule["kind"]
    if kind == "ema":
        return [("Exit line", f"{rule['n']}-day EMA", f"{rule['exit_rule']} close below it"),
                ("Initial stop", f"{params.stop_pct:g}%", f"below the {rule['n']}-day EMA at entry")]
    partial = (f"{rule['fraction']:.0%} at session {rule['day']}" if kind == "partial_day" else
               f"{rule['fraction']:.0%} at +{rule['pct']:g}%" if kind == "partial_pct" else f"Session {rule['days']}")
    line = f"{rule['n']}-day average" if "n" in rule else "None"
    stop = (f"{params.max_stop_pct:g}% at most below entry" if spec.id == "minervini" else
            "Entry session's low" if spec.id == "episodic_pivot" else "Prior session's low")
    return [("Partial or time exit", partial, "new entries"), ("Trailing line", line, "a close under it sells the rest"),
            ("Initial stop", stop, "unless you record one")]


# Settings ----------------------------------------------------------------------------------------------------------

# Every threshold, grouped by the rule it changes, with its form label.
THRESHOLD_GROUPS = {
    "Sell rules": [
        ("stop_loss_pct", "Stop loss, % below entry"),
        ("profit_target_pct", "Profit target, % above entry"),
        ("fast_gain_pct", "Fast gain, % above breakout reference"),
        ("fast_gain_weeks", "Fast-gain window, calendar weeks"),
        ("minimum_hold_weeks", "Minimum hold from breakout, calendar weeks"),
    ],
    "Price and liquidity": [
        ("min_price", "Minimum close, USD"), ("min_avg_volume", "Minimum average volume, shares/day"),
        ("max_below_high_pct", "Maximum distance below high, %"), ("high_sessions", "High lookback, sessions"),
        ("short_ma_sessions", "Short moving average, sessions"), ("long_ma_sessions", "Long moving average, sessions"),
        ("volume_sessions", "Average volume lookback, sessions"),
    ],
    "C · Quarterly earnings": [
        ("eps_growth_pct", "Minimum quarterly EPS growth, %"),
        ("eps_acceleration_min", "Required EPS accelerations out of 3"),
        ("sales_growth_pct", "Minimum quarterly sales growth, %"),
        ("margin_near_high_pct", "Margin tolerance below its high, % of high"),
        ("one_time_item_pct", "One-time item flag, % of continuing income"),
        ("max_quarter_age_days", "Maximum quarterly statement age, days"),
    ],
    "A · Annual earnings": [
        ("annual_eps_growth_pct", "Minimum annual EPS growth, %"), ("roe_min_pct", "Minimum return on equity, %"),
        ("cash_flow_above_eps_pct", "Minimum cash flow per share above EPS, %"),
        ("max_annual_age_days", "Maximum annual statement age, days"),
    ],
    "S · Supply and debt": [
        ("shares_max_growth_pct", "Maximum three-year diluted share growth, %"),
        ("shares_latest_max_growth_pct", "Maximum latest annual diluted share growth, %"),
        ("debt_equity_max_growth_pct", "Maximum three-year debt/equity growth, %"),
    ],
    "C/A/S/L · Passing rule": [
        ("scored_checks_required", "Scored checks required, of 8 (EPS, sales and RS always required)"),
        ("min_measurable_scored_checks", "Minimum measurable scored checks"),
    ],
    "L · Relative strength and industry": [
        ("rs_min", "Minimum RS rating, 1 to 99"), ("rs_quarter_sessions", "RS quarter length, sessions"),
        ("rs_recent_weight", "Most recent quarter weight"),
        ("industry_sessions", "Industry performance lookback, sessions"),
        ("industry_top", "Highest passing industry rank"),
    ],
    "M · Market direction": [
        ("distribution_decline_pct", "Minimum distribution decline, %"),
        ("distribution_window_sessions", "Distribution window, sessions"),
        ("distribution_pressure_count", "Distribution days for pressure"),
        ("distribution_correction_count", "Distribution days for correction"),
        ("follow_through_gain_pct", "Minimum follow-through gain, %"),
        ("follow_through_min_day", "Earliest follow-through rally day"),
        ("market_volume_stale_sessions", "Stale volume window, sessions"),
        ("distribution_expiry_gain_pct", "Distribution day expires after a rally of, % (0 never)"),
        ("market_index_rule", "Index that determines M"),
        ("correction_drawdown_pct", "Correction: close below the uptrend's peak, % (0 uses the distribution count)"),
        ("distribution_heavy_count", "Distribution days for heavy pressure"),
    ],
    "M · Exposure ladder": [
        ("exposure_confirmed_pct", "Confirmed uptrend, % invested"),
        ("exposure_late_confirmed_pct", "Confirmed, one distribution day short of pressure, % invested"),
        ("exposure_new_uptrend_pct", "Cap after a follow-through, % invested"),
        ("exposure_new_uptrend_sessions", "Follow-through cap lasts, sessions"),
        ("exposure_pressure_pct", "Under pressure, % invested"),
        ("exposure_heavy_pressure_pct", "Heavy pressure, % invested"),
    ],
    "N · Bases and breakouts": [
        ("base_lookback_weeks", "Base search lookback, weeks"), ("base_max_weeks", "Maximum base length, weeks"),
        ("base_recent_weeks", "Retain recent breakouts, weeks"), ("base_rim_tolerance_pct", "Rim recovery tolerance, %"),
        ("buy_zone_max_pct", "Maximum buy zone above pivot, %"),
        ("buy_zone_entry_weeks", "Backtest buys in the buy zone for, weeks after breakout (0 = breakout day only)"),
        ("breakout_volume_pct", "Minimum breakout volume increase, %"),
        ("breakout_volume_sessions", "Breakout volume average, prior sessions"),
    ],
    "N · Pattern geometry": [
        ("cup_min_weeks", "Minimum cup length, weeks"), ("cup_min_depth_pct", "Minimum cup depth, %"),
        ("cup_max_depth_pct", "Maximum cup depth, %"),
        ("cup_correction_max_depth_pct", "Maximum cup depth during a correction, %"),
        ("cup_side_min_weeks", "Minimum weeks on each side of cup trough"),
        ("handle_min_weeks", "Minimum handle length, weeks"), ("handle_max_weeks", "Maximum handle length, weeks"),
        ("handle_max_depth_pct", "Maximum handle depth, %"),
        ("handle_volume_max_ratio", "Maximum handle/cup daily volume ratio"),
        ("cup_pivot_offset", "Cup pivot offset, USD"),
        ("cup_without_handle_min_weeks", "Minimum cup-without-handle length, weeks (0 = pattern off)"),
        ("double_bottom_min_weeks", "Minimum double-bottom length, weeks"),
        ("flat_min_weeks", "Minimum flat-base length, weeks"), ("flat_max_depth_pct", "Maximum flat-base depth, %"),
        ("flat_prior_breakout_weeks", "Flat base needs an earlier base's breakout within, weeks (0 = off)"),
        ("flat_prior_advance_pct", "Flat base high above that earlier pivot, at least %"),
    ],
}


CHOICES = {"market_index_rule": {"both": "Both indexes must confirm (weaker decides)",
                                  "either": "Either index confirms (stronger decides)",
                                  "sp500": "S&P 500 alone", "nasdaq": "Nasdaq Composite alone",
                                  "average": "Average of both indexes",
                                  "ignore": "Ignore the market (always 100%, research only)"}}


# Backtests -------------------------------------------------------------------------------------------------------

APPROXIMATE = "approximate"     # backtest_approx.MODE, the method a result was run with
BACKTEST_MODES = {
    "Approximate": "Uses today's statement histories, each dated by its filing, and adds back companies "
                   "that delisted. Available now; results can be slightly flattering.",
    "Strict": "Uses only universe snapshots and statements saved on each past day. Nothing leaks from "
              "hindsight, but history starts when you begin capturing.",
}
BACKTEST_STRATEGIES = {
    "Breakout trades": "O'Neil's method: buy qualifying breakouts in the buy zone, sell by the stop and profit rules.",
    "Leaders portfolio": "Hold the top-ranked stocks passing the Screen, rebalanced on a schedule; the stop applies, "
                         "no profit target. Approximate method only.",
    "Trend leaders": "Hold high-RS leaders in strong groups with fast sales growth; sell only on a close below the "
                     "200-day line (or at the loss cap, if set). The market limits new buys but never forces sales. "
                     "Approximate method only.",
}
BACKTEST_KEYS = {"Leaders portfolio": "leaders", "Trend leaders": "trend"}
BACKTEST_NAMES = {"leaders": "Leaders", "trend": "Trend leaders"}
TRADE_COLUMNS = {"symbol": "Symbol", "pattern": "Base", "signal_date": "Signal", "entry_date": "Entry",
                 "entry_price": "Entry, USD", "exit_date": "Exit", "exit_price": "Exit, USD",
                 "return_pct": "Return, %", "pnl": "Profit, USD", "reason": "Exit reason", "pivot": "Pivot, USD",
                 "shares": "Shares", "fast_gain_date": "Fast gain"}
OPEN_COLUMNS = {"symbol": "Symbol", "entry_date": "Entry", "entry_price": "Entry, USD", "last_close": "Last close, USD",
                "value": "Value, USD", "unrealized_pnl": "Unrealized, USD", "shares": "Shares",
                "fast_gain_date": "Fast gain", "profit_order_pending": "Sell at next open"}
SKIPPED_COLUMNS = {"symbol": "Symbol", "signal_date": "Signal", "entry_date": "Planned entry", "reason": "Reason"}
RESULTS = "backtest:result:"
RULE_NAMES = {"both": "Both indexes", "either": "Either index", "sp500": "S&P 500", "nasdaq": "Nasdaq",
              "average": "Both averaged", "ignore": "Ignored (100%)"}


def exposure_at(point) -> float:
    """Allowed exposure at a close; results saved before exposure existed used the confirmed-uptrend gate."""
    value = point.get("exposure")
    return value if value is not None else 100.0 if point.get("market_state") == "confirmed uptrend" else 0.0


def result_thresholds(report) -> dict:
    """A result's rules; results saved before a threshold existed used its default."""
    from dataclasses import asdict
    from .config import Thresholds
    return {**asdict(Thresholds()), **report["thresholds"]}


def result_name(report) -> str:
    method = "Approximate" if report.get("mode") == APPROXIMATE else "Strict"
    return report.get("label") or f"{method} · {report['start']} to {report['end']}"


def result_market_rule(report) -> str:
    t = result_thresholds(report)
    expiry, drawdown = t["distribution_expiry_gain_pct"], t["correction_drawdown_pct"]
    graded = t["exposure_pressure_pct"] > 0 or t["exposure_late_confirmed_pct"] < 100
    return (RULE_NAMES[t["market_index_rule"]] + (f", {drawdown:g}% correction" if drawdown else "")
            + (f", {expiry:g}% expiry" if expiry else "") + (", graded exposure" if graded else ""))


def average_exposure(report) -> float:
    points = report["equity_curve"]
    return sum(exposure_at(p) for p in points) / max(len(points), 1)


def result_strategy(report) -> str:
    return BACKTEST_NAMES.get(report.get("strategy"), "Breakout")


def rule_differences(report, current) -> list[str]:
    """The saved result's thresholds that differ from current Settings, as readable lines."""
    from dataclasses import asdict
    labels = {key: label for entries in THRESHOLD_GROUPS.values() for key, label in entries}
    saved, now = result_thresholds(report), asdict(current)
    shown = lambda key, value: CHOICES[key][value] if key in CHOICES else f"{value:g}"
    return [f"{labels.get(key, key)}: {shown(key, saved[key])} (Settings {shown(key, now[key])})"
            for key in now if saved.get(key) != now[key]]


def without_provider(text: str) -> str:
    """Saved text with the data provider's name taken out: results saved before 2026-10-01 named it."""
    return text.replace("come from FMP's directory", "come from the delisted-company directory")


def figure(value, suffix="%"):
    return "Unavailable" if value is None else f"{value:,.2f}{suffix}"
