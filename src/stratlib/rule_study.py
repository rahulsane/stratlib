"""Which screening rules predicted returns? A monthly study on the approximate method's data.

On the first session of each month the whole market is ranked exactly as the
Screen ranks it, and each stock's inputs to every rule are recorded. Each
stock's return from the next session's close over the following 1, 3, 6 and
12 months is compared with SPY's over the same sessions. A rule helps if the
stocks passing it went on to beat the stocks failing it, consistently across
months and years. No API calls.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median, stdev

import numpy as np
import pandas as pd

from .backtest import BacktestError
from .backtest_approx import StatementCache, check, members, rankings, split_rows, warmup
from .backtest_data import ET, restore_price_basis
from .bases import detect_base, history_weeks
from .config import Settings
from .market_direction import CORRECTION, market_direction
from .scoring import CRITERIA, leadership_criteria, meets_rule, score_fundamentals, scored_summary
from .technical import buyable

STUDY = "research:rule_study"
HORIZONS = {"1 month": 21, "3 months": 63, "6 months": 126, "12 months": 252}
MAIN = "6 months"
MIN_GROUP = 3         # stocks needed on each side of a rule for a month to count
MONTH = 21            # sessions between cohorts, for the overlap adjustment

LIMITATIONS = [
    "Uses the approximate method's data: today's restated statement histories dated by filing, today's industries, "
    "and delisted stocks valued at their last close.",
    "Returns run from the close after each monthly ranking, before trading costs, with equal weight per stock.",
    "Months overlap for horizons longer than one month; the t-statistic counts one independent observation per "
    "horizon length, so it is conservative but still approximate.",
    "Fundamental and base rules are measured only among price and RS leaders, where the Screen applies them.",
]


def cohort_days(days: list[str]) -> list[str]:
    """The first session of each month, leaving at least one session to enter."""
    return [d for i, d in enumerate(days[:-1]) if i == 0 or d[:7] != days[i - 1][:7]]


def close_matrix(store, symbols: list[str], sessions: list[str]) -> np.ndarray:
    """Closes by session and symbol; after a delisting the last close carries forward."""
    index = {d: i for i, d in enumerate(sessions)}
    matrix = np.full((len(sessions), len(symbols)), np.nan)
    for j, symbol in enumerate(symbols):
        for day, _high, close, _volume in store.price_rows(symbol, sessions[0], sessions[-1]):
            i = index.get(day)
            if i is not None and close is not None and close > 0:
                matrix[i, j] = close
    return pd.DataFrame(matrix).ffill().to_numpy()


def _numeric(key: str, value):
    """One number per check for bucketing; lists become their deciding figure."""
    if key == "a_eps":
        return min(value) if isinstance(value, list) and value else None
    if key == "s_debt":
        return 100 * (value[0] / value[-1] - 1) if isinstance(value, list) and len(value) > 1 and value[-1] > 0 else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _plain(value):
    """JSON-safe copy: numpy scalars become Python numbers and NaN becomes None."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _flag(value) -> float:
    return math.nan if value is None else float(bool(value))


def observations(store, settings: Settings, start: str, end: str, *, progress=None) -> tuple[pd.DataFrame, dict]:
    """One row per liquid stock per monthly ranking, with rule inputs and forward excess returns."""
    t = settings.thresholds
    note = progress or (lambda message, done, total: None)
    status = check(store, settings, start, end)
    if not status["ready"]:
        raise BacktestError("\n".join(status["errors"]))
    all_sessions = [b.date for b in store.price_history("SPY", through=end)]
    days = [d for d in all_sessions if start <= d <= end]
    cohorts = cohort_days(days)
    member_map, _ = members(store, settings, start, end)
    ranked = rankings(store, settings, start, end, member_map, progress=note, detail_days=set(cohorts))
    market_histories = {s: store.price_history(s, through=end) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}
    market = market_direction(market_histories, t, as_of=end)
    corrections = sorted(row["date"] for row in market["history"] if row["state"] == CORRECTION)
    symbols = sorted(member_map)
    column = {s: j for j, s in enumerate(symbols)}
    note("Loading closes", 0, len(symbols))
    closes = close_matrix(store, [*symbols, "SPY"], days)
    spy = closes[:, -1]
    splits = split_rows(store)
    basis = {s: p.checked_at.astimezone(ET).date().isoformat()
             for s, p in store.price_states(symbols).items() if p.checked_at}
    statements = StatementCache(store)
    span = max(warmup(t), 5 * (history_weeks(t) + 2)) + 10
    rows = []
    for number, day in enumerate(cohorts):
        info, i = ranked["days"][day], days.index(day)
        forward = {}
        for name, n in HORIZONS.items():
            if i + 1 + n < len(days):
                with np.errstate(divide="ignore", invalid="ignore"):
                    stock = 100 * (closes[i + 1 + n] / closes[i + 1] - 1)
                forward[name] = stock - 100 * (spy[i + 1 + n] / spy[i + 1] - 1)
        sessions = all_sessions[:bisect_right(all_sessions, day)]
        seen_corrections = frozenset(corrections[:bisect_right(corrections, day)])
        for symbol, f in info["features"].items():
            liquid = f["price"] >= t.min_price and f["avg_volume"] >= t.min_avg_volume
            if not liquid:
                continue
            group = info["groups"].get(f["industry"])
            row = {"day": day, "symbol": symbol, "rs": f["rs"], "below_high_pct": f["below_high_pct"],
                   "above_short_ma": float(f["above_short_ma"]), "above_long_ma": float(f["above_long_ma"]),
                   "short_ma_gap_pct": f["short_ma_gap_pct"], "dollar_volume": f["price"] * f["avg_volume"],
                   "industry_rank": group["rank"] if group else math.nan,
                   "leader": float(symbol in info["survivors"])}
            for name, values in forward.items():
                value = values[column[symbol]]
                row[name] = float(value) if np.isfinite(value) else math.nan
            if symbol in info["survivors"]:
                doc = statements.get(symbol)
                if doc and "error" not in doc:
                    criteria, _ = score_fundamentals(statements.public(symbol, day), t, date.fromisoformat(day))
                    criteria += leadership_criteria(f["rs"], group, t)
                    for c in criteria:
                        row[f"pass_{c.key}"] = _flag(c.passed)
                        row[f"value_{c.key}"] = _numeric(c.key, c.value)
                    row["scored_passed"] = scored_summary(criteria)[0]
                    row["passes_screen"] = float(meets_rule(criteria, t))
                bars = [b for b in store.price_history(symbol, limit=span, through=day)]
                cutoff = (date.fromisoformat(day) - timedelta(weeks=history_weeks(t) + 1)).isoformat()
                cutoff = min(cutoff, sessions[max(0, len(sessions) - warmup(t))])
                adjusted, _ = restore_price_basis([b for b in bars if b.date >= cutoff], splits.get(symbol, []),
                                                  basis.get(symbol, end), day)
                base = detect_base(adjusted, day, t, correction_dates=seen_corrections, sessions=sessions)
                row["in_base"] = float(base["pattern"] is not None)
                row["buyable"] = float(buyable(base, day, t))
                row["pivot_distance_pct"] = base["distance_pct"] if base["distance_pct"] is not None else math.nan
            rows.append(row)
        note(f"Studying {day}", number + 1, len(cohorts))
    frame = pd.DataFrame(rows)
    for name in HORIZONS:          # horizons longer than the period stay empty
        if name not in frame:
            frame[name] = math.nan
    exposures = [(row["date"], row["exposure"], row["state"]) for row in market["history"] if start <= row["date"] <= end]
    spy_close = {d: spy[k] for k, d in enumerate(days)}
    return frame, {"cohorts": cohorts, "days": days, "exposures": exposures, "spy": spy_close}


def _t_stat(values: list[float], sessions: int) -> float | None:
    if len(values) < 3 or stdev(values) == 0:
        return None
    effective = len(values) * min(1.0, MONTH / sessions)
    return mean(values) / (stdev(values) / math.sqrt(effective))


def verdict(spread: float, median_spread: float, t: float | None, years_same: int, years: int) -> str:
    if t is None or years == 0:
        return "Too little data"
    agree = (spread > 0) == (median_spread > 0)
    if abs(t) >= 2 and agree and years_same >= math.ceil(2 * years / 3):
        return "Helps" if spread > 0 else "Hurts"
    if abs(t) >= 1 and agree and years_same >= math.ceil(years / 2):
        return "Slight help" if spread > 0 else "Slight harm"
    return "No clear effect"


def effect(frame: pd.DataFrame, population: pd.Series, rule: str, horizon: str) -> dict:
    """Stocks passing a rule against those failing it, month by month."""
    data = frame.loc[population & frame[rule].notna() & frame[horizon].notna(), ["day", rule, horizon]]
    months = []
    for day, group in data.groupby("day"):
        passed, failed = group.loc[group[rule] == 1, horizon], group.loc[group[rule] == 0, horizon]
        if len(passed) >= MIN_GROUP and len(failed) >= MIN_GROUP:
            months.append((day, passed.mean(), failed.mean(), passed.median() - failed.median(),
                           len(passed) / len(group), len(passed)))
    if not months:
        return {"rule": rule, "horizon": horizon, "months": 0, "verdict": "Too little data"}
    spreads = [a - b for _, a, b, _, _, _ in months]
    medians = [m for _, _, _, m, _, _ in months]
    by_year = defaultdict(list)
    for (day, *_), value in zip(months, spreads):
        by_year[day[:4]].append(value)
    years = {y: mean(v) for y, v in by_year.items() if len(v) >= 3}
    spread, median_spread = mean(spreads), median(medians)
    same = sum((v > 0) == (spread > 0) for v in years.values())
    t = _t_stat(spreads, HORIZONS[horizon])
    return {"rule": rule, "horizon": horizon, "months": len(months),
            "pass_excess": mean(a for _, a, _, _, _, _ in months), "fail_excess": mean(b for _, _, b, _, _, _ in months),
            "spread": spread, "median_spread": median_spread,
            "months_better_pct": 100 * sum(v > 0 for v in spreads) / len(spreads),
            "years_same": same, "years": len(years), "by_year": years, "t": t,
            "pass_share_pct": 100 * mean(s for _, _, _, _, s, _ in months),
            "passing_per_month": mean(n for *_, n in months),
            "verdict": verdict(spread, median_spread, t, same, len(years))}


def buckets(frame: pd.DataFrame, population: pd.Series, column: str, edges: list[float], horizon: str) -> list[dict]:
    """Average excess return by value range; each month counts equally."""
    data = frame.loc[population & frame[column].notna() & frame[horizon].notna(), ["day", column, horizon]]
    result = []
    for low, high in zip(edges, edges[1:]):
        chosen = data[(data[column] >= low) & (data[column] < high)]
        if chosen.empty:
            continue
        monthly = chosen.groupby("day")[horizon].mean()
        result.append({"low": low, "high": high, "excess": float(monthly.mean()),
                       "beat_spy_pct": float(100 * (chosen[horizon] > 0).mean()),
                       "per_month": float(chosen.groupby("day").size().mean()), "months": int(len(monthly)),
                       "t": _t_stat(list(monthly), HORIZONS[horizon])})
    return result


def _quantile_edges(values: pd.Series, parts: int = 5) -> list[float]:
    edges = sorted(set(float(v) for v in values.dropna().quantile([k / parts for k in range(parts + 1)])))
    return [*edges[:-1], edges[-1] + 1e-9] if len(edges) > 1 else []


RULES = [
    # (key, label, population, column)
    ("rs", "RS rating 80 or higher", "liquid", "rule_rs"),
    ("near_high", "Within 15% of the 52-week high", "liquid", "rule_near_high"),
    ("short_ma", "Above the 50-day line", "liquid", "above_short_ma"),
    ("long_ma", "Above the 200-day line", "liquid", "above_long_ma"),
    ("industry_liquid", "Industry group in the top 40", "liquid", "rule_industry"),
    ("leader", "Passes every price and RS filter", "liquid", "leader"),
    *[(key, label, "leaders", f"pass_{key}") for key, _, label in CRITERIA if key != "l_rs"],
    ("screen", "Passes the Screen's C/A/S/L rule", "leaders", "passes_screen"),
    ("in_base", "In a base (any pattern)", "leaders", "in_base"),
    ("buyable", "Buyable breakout (within 3 weeks, in the buy zone)", "leaders", "buyable"),
    ("buyable_passers", "Buyable breakout, among Screen passers", "passers", "buyable"),
]


def run_study(store, settings: Settings, start: str, end: str, *, progress=None) -> dict:
    t = settings.thresholds
    frame, context = observations(store, settings, start, end, progress=progress)
    frame["rule_rs"] = (frame["rs"] >= t.rs_min).astype(float)
    frame["rule_near_high"] = (frame["below_high_pct"] <= t.max_below_high_pct).astype(float)
    frame["rule_industry"] = np.where(frame["industry_rank"].notna(), (frame["industry_rank"] <= t.industry_top).astype(float),
                                      np.nan)
    for column in ("passes_screen", "in_base", "buyable", *[f"pass_{k}" for k, _, _ in CRITERIA]):
        if column not in frame:
            frame[column] = np.nan
    liquid = pd.Series(True, index=frame.index)
    leaders = frame["leader"] == 1
    populations = {"liquid": liquid, "leaders": leaders, "passers": frame["passes_screen"] == 1}
    rules = []
    for key, label, population, column in RULES:
        rules.append({"key": key, "label": label, "population": population,
                      **{name: effect(frame, populations[population], column, name) for name in HORIZONS}})
    main = MAIN
    tables = {
        "RS rating (all liquid stocks)": buckets(frame, liquid, "rs", [1, 40, 60, 80, 90, 95, 100], main),
        "Distance below the 52-week high, % (all liquid stocks)":
            buckets(frame, liquid, "below_high_pct", [0, 5, 10, 15, 25, 40, 100], main),
        "Trading value, $ millions a day (all liquid stocks)":
            buckets(frame.assign(dollar_m=frame["dollar_volume"] / 1e6), liquid, "dollar_m",
                    _quantile_edges(frame["dollar_volume"] / 1e6), main),
        "RS rating (leaders)": buckets(frame, leaders, "rs", [80, 85, 90, 95, 98, 100], main),
        "Distance above the 50-day line, % (leaders)":
            buckets(frame, leaders, "short_ma_gap_pct", _quantile_edges(frame.loc[leaders, "short_ma_gap_pct"]), main),
        "Scored checks passed of 8 (leaders)": buckets(frame, leaders, "scored_passed", [0, 3, 5, 6, 7, 8, 9], main),
    }
    for key, label in (("c_eps", "Quarterly EPS growth, %"), ("c_sales", "Quarterly sales growth, %"),
                       ("a_eps", "Slowest of 3 annual EPS growth rates, %"), ("a_roe", "Return on equity, %"),
                       ("c_margin", "After-tax margin, %")):
        column = f"value_{key}"
        if column in frame:
            tables[f"{label} (leaders)"] = buckets(frame, leaders, column, _quantile_edges(frame.loc[leaders, column]), main)
    market = _market_table(context)
    sizes = {name: float(frame.loc[mask].groupby("day").size().mean()) for name, mask in populations.items()}
    # Each group's own average against SPY: the typical stock, not just passers against failers.
    baseline = {name: {h: _average_excess(frame, mask, h) for h in HORIZONS} for name, mask in populations.items()}
    study = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "start": start, "end": end,
             "mode": "approximate", "cohorts": len(context["cohorts"]), "horizons": HORIZONS, "main_horizon": main,
             "population_per_month": sizes, "population_excess": baseline, "rules": rules, "tables": tables, "market": market,
             "observations": int(len(frame)), "limitations": LIMITATIONS}
    study = _plain(study)
    store.save_document(STUDY, study)                      # the latest study
    store.save_document(f"{STUDY}:{start}:{end}", study)   # kept per period for comparison
    return study


def _average_excess(frame: pd.DataFrame, population: pd.Series, horizon: str) -> dict | None:
    monthly = frame.loc[population & frame[horizon].notna()].groupby("day")[horizon].mean()
    if monthly.empty:
        return None
    return {"excess": float(monthly.mean()), "months_ahead_pct": float(100 * (monthly > 0).mean()),
            "t": _t_stat(list(monthly), HORIZONS[horizon])}


def _market_table(context: dict) -> list[dict]:
    """SPY's next 1, 3 and 6 months by the exposure the market rule allowed that day."""
    days, spy = context["days"], context["spy"]
    index = {d: i for i, d in enumerate(days)}
    groups = defaultdict(lambda: defaultdict(list))
    for day, exposure, _state in context["exposures"]:
        i = index.get(day)
        if i is None or exposure is None:
            continue
        for name in ("1 month", "3 months", "6 months"):
            n = HORIZONS[name]
            if i + n < len(days):
                groups[exposure][name].append(100 * (spy[days[i + n]] / spy[day] - 1))
    return [{"exposure": exposure, "days": len(values["1 month"]),
             **{name: {"mean": mean(v), "positive_pct": 100 * sum(x > 0 for x in v) / len(v)}
                for name, v in values.items() if v}}
            for exposure, values in sorted(groups.items())]


def format_study(study: dict) -> str:
    """Plain-text summary for the command line."""
    main = study["main_horizon"]
    lines = [f"Rule study {study['start']} to {study['end']} ({study['mode']}): {study['cohorts']} monthly rankings, "
             f"{study['observations']:,} stock-months.",
             "Average per month: " + ", ".join(f"{k} {v:,.0f}" for k, v in study["population_per_month"].items()), "",
             "Each group's average excess over SPY (equal weight; share of months ahead of SPY):"]
    for name, horizons in study["population_excess"].items():
        cells = [f"{h} {v['excess']:+5.1f}% ({v['months_ahead_pct']:.0f}%)" for h, v in horizons.items() if v]
        lines.append(f"  {name:9} " + "  ".join(cells))
    lines += ["",
             f"Each rule: stocks passing vs failing it, next {main}, excess return over SPY (equal weight).",
             f"{'Rule':52}{'among':>9}{'pass':>8}{'fail':>8}{'diff':>8}{'t':>6}{'months':>8}{'years':>7}  verdict"]
    for rule in study["rules"]:
        e = rule[main]
        if not e["months"]:
            lines.append(f"{rule['label']:52}{rule['population']:>9}  too little data")
            continue
        t = f"{e['t']:.1f}" if e["t"] is not None else "-"
        lines.append(f"{rule['label']:52}{rule['population']:>9}{e['pass_excess']:+7.1f}%{e['fail_excess']:+7.1f}%"
                     f"{e['spread']:+7.1f}%{t:>6}{e['months_better_pct']:7.0f}%{e['years_same']:>4}/{e['years']:<2}  "
                     f"{e['verdict']}")
    lines += ["", "Same comparison at other horizons (difference, verdict):"]
    for rule in study["rules"]:
        cells = [f"{h}: {rule[h]['spread']:+.1f}% {rule[h]['verdict']}" if rule[h]["months"] else f"{h}: -"
                 for h in study["horizons"] if h != main]
        lines.append(f"  {rule['label']:52}" + " | ".join(cells))
    for title, rows in study["tables"].items():
        lines += ["", f"{title}, next {main}:"]
        for row in rows:
            t = f"{row['t']:.1f}" if row["t"] is not None else "-"
            lines.append(f"  {row['low']:>10,.1f} to {row['high']:<10,.1f} excess {row['excess']:+6.1f}%  "
                         f"beat SPY {row['beat_spy_pct']:3.0f}%  t {t:>5}  stocks/month {row['per_month']:7.1f}")
    lines += ["", "SPY afterwards, by the exposure the market rule allowed that day:"]
    for row in study["market"]:
        cells = [f"{h} {row[h]['mean']:+5.1f}% (up {row[h]['positive_pct']:.0f}%)" for h in ("1 month", "3 months", "6 months")
                 if h in row]
        lines.append(f"  {row['exposure']:>5.0f}%  {row['days']:5} days  " + "  ".join(cells))
    return "\n".join(lines)
