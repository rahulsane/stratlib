"""Pure C/A/S/L criteria, with measurements and explicit unavailable states."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from .config import BacktestSettings, Thresholds


@dataclass(frozen=True)
class Criterion:
    key: str
    letter: str
    label: str
    value: Any
    passed: bool | None
    rule: str
    detail: str = ""

    def to_dict(self):
        return asdict(self)


CRITERIA = [
    ("c_eps", "C", "Quarterly EPS growth"),
    ("c_acceleration", "C", "EPS acceleration"),
    ("c_sales", "C", "Quarterly sales growth"),
    ("c_margin", "C", "After-tax margin"),
    ("a_eps", "A", "Annual EPS growth"),
    ("a_roe", "A", "Return on equity"),
    ("a_cash", "A", "Cash flow above EPS"),
    ("s_shares", "S", "Diluted share trend"),
    ("s_debt", "S", "Debt-to-equity trend"),
    ("l_rs", "L", "Relative strength"),
    ("l_industry", "L", "Industry rank"),
]


def number(value) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def public_date(row: dict) -> date:
    for key in ("acceptedDate", "filingDate", "fillingDate"):
        try:
            return date.fromisoformat(str(row.get(key))[:10])
        except ValueError:
            pass
    return date.fromisoformat(row["date"]) + timedelta(days=90 if row.get("period") == "FY" else 45)


def period_key(row: dict) -> tuple[int, int] | None:
    try:
        year = int(row["fiscalYear"])
        period = row["period"]
        quarter = 0 if period == "FY" else int(period[1:]) if period.startswith("Q") else -1
        return (year, quarter) if 0 <= quarter <= 4 else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def ordered_statements(rows: list[dict], as_of: date, annual: bool) -> list[dict]:
    unique = {}
    for row in rows:
        key = period_key(row)
        if key is None or (key[1] == 0) != annual:
            continue
        try:
            if date.fromisoformat(row["date"]) > as_of or public_date(row) > as_of:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        previous = unique.get(key)
        if previous is None or public_date(row) > public_date(previous):
            unique[key] = row
    return [unique[key] for key in sorted(unique, reverse=True)]


def consecutive(rows: list[dict], n: int, annual: bool) -> list[dict]:
    selected = rows[:n]
    if len(selected) != n:
        return []
    keys = [period_key(row) for row in selected]
    ordinals = [y if annual else y * 4 + q for y, q in keys]
    currencies = {row.get("reportedCurrency") for row in selected}
    if len(currencies) != 1 or None in currencies or "" in currencies:
        return []
    if any(a - b != 1 for a, b in zip(ordinals, ordinals[1:])):
        return []
    return selected


def split_factor(splits: list[dict], basis_date: date, as_of: date) -> float | None:
    """FMP standardized EPS/shares already include splits through fetch date.

    Only apply actions after that basis date. This also supports explicitly
    unadjusted inputs by passing their known share-basis date. Never infer the
    basis from fiscal year-end: standardized past years can be restated.
    """
    factor = 1.0
    for split in splits:
        try:
            day = date.fromisoformat(split["date"])
        except (KeyError, TypeError, ValueError):
            return None
        if basis_date < day <= as_of:
            numerator, denominator = number(split.get("numerator")), number(split.get("denominator"))
            if numerator is None or denominator is None or numerator <= 0 or denominator <= 0:
                return None
            factor *= numerator / denominator
    return factor


def continuing_eps(row: dict, factor: float = 1.0) -> float | None:
    shares = number(row.get("weightedAverageShsOutDil"))
    continuing = number(row.get("netIncomeFromContinuingOperations"))
    reported = number(row.get("epsDiluted"))
    total = number(row.get("netIncome"))
    if shares is None or shares <= 0 or continuing is None or not factor:
        return None
    # Retain the provider's diluted EPS/common-income adjustments, excluding
    # the part of net income outside continuing operations.
    if reported is not None and total is not None:
        return (reported - (total - continuing) / shares) / factor
    return continuing / shares / factor


def growth(current: float | None, prior: float | None) -> float | None:
    """Signed percentage change relative to the magnitude of the prior value.

    A smaller loss is an improvement; crossing from loss to profit can exceed
    100%. Zero has no percentage baseline, including when both values are zero.
    """
    if (current is None or prior is None or prior == 0
            or not math.isfinite(current) or not math.isfinite(prior)):
        return None
    result = 100 * ((current - prior) / abs(prior))
    return result if math.isfinite(result) else None


def _growths(rows: list[dict], field, count: int, annual: bool) -> list[float] | None:
    offset = 1 if annual else 4
    selected = consecutive(rows, count + offset, annual)
    if not selected:
        return None
    result = [growth(field(selected[i]), field(selected[i + offset])) for i in range(count)]
    return result if all(v is not None for v in result) else None


def _period_label(row: dict) -> str:
    return f"{row['fiscalYear']} {row['period']}"


def _eps_growth_detail(rows: list[dict], factor: float | None, count: int, annual: bool) -> str:
    """Keep the observed EPS visible even when a percentage cannot be used."""
    offset = 1 if annual else 4
    selected = consecutive(rows, count + offset, annual)
    if not selected:
        periods = "fiscal years" if annual else "quarters"
        return (f"Unavailable: requires {count + offset} current, consecutive {periods} "
                "in one reported currency.")
    if factor is None:
        return "Unavailable: split history or the statement share basis is missing."
    comparisons, missing, zero_base, invalid_rate = [], [], False, False
    for i in range(count):
        current_row, prior_row = selected[i], selected[i + offset]
        current, prior = continuing_eps(current_row, factor), continuing_eps(prior_row, factor)
        for row, value in ((current_row, current), (prior_row, prior)):
            if value is None:
                missing.append(_period_label(row))
        current_text = f"{current:.2f}" if current is not None else "missing"
        prior_text = f"{prior:.2f}" if prior is not None else "missing"
        comparison = (f"{_period_label(current_row)} EPS {current_text} vs "
                      f"{_period_label(prior_row)} EPS {prior_text}")
        rate = growth(current, prior)
        if rate is not None:
            comparison += f", {rate:+.2f}% YoY"
            if prior < 0 and current < 0:
                comparison += ", loss " + ("narrowed" if current > prior else "widened" if current < prior else "unchanged")
            elif prior < 0:
                comparison += ", turned profitable" if current > 0 else ", reached breakeven"
            elif current < 0:
                comparison += ", turned loss-making"
        elif current is not None and prior is not None and prior != 0:
            invalid_rate = True
        zero_base |= prior == 0
        comparisons.append(comparison)
    evidence = ("; ".join(comparisons) + ". EPS uses diluted continuing operations. "
                "YoY change = 100 * (current - prior) / abs(prior); positive means improvement, including a smaller loss.")
    if missing:
        periods = ", ".join(dict.fromkeys(missing))
        return f"Unavailable: continuing EPS is missing for {periods}. " + evidence
    if zero_base:
        return "Not meaningful: prior EPS is zero in one or more comparisons, so percentage change is undefined. " + evidence
    if invalid_rate:
        return "Unavailable: the EPS change exceeds the supported numeric range. " + evidence
    return evidence


def _matched(row: dict, candidates: list[dict]) -> dict:
    return next((other for other in candidates if period_key(other) == period_key(row)
                 and other.get("date") == row.get("date")
                 and other.get("reportedCurrency") == row.get("reportedCurrency")), {})


def score_fundamentals(bundle: dict, t: Thresholds, as_of: date) -> tuple[list[Criterion], list[str]]:
    """Evaluate statements supplied by the caller. Inputs are never mutated.

    Filing dates filter observations, but today's restated FMP history is not
    point-in-time data. The backtester supplies a dated archived vintage.
    """
    q = ordered_statements(bundle.get("income_quarter", []), as_of, False)
    a = ordered_statements(bundle.get("income_annual", []), as_of, True)
    balance = ordered_statements(bundle.get("balance_annual", []), as_of, True)
    cash = ordered_statements(bundle.get("cash_annual", []), as_of, True)
    warnings = []
    if q and (as_of - date.fromisoformat(q[0]["date"])).days > t.max_quarter_age_days:
        warnings.append("Latest quarterly statement is stale; C is unavailable.")
        q = []
    if a and (as_of - date.fromisoformat(a[0]["date"])).days > t.max_annual_age_days:
        warnings.append("Latest annual statement is stale; A and S are unavailable.")
        a = []
    try:
        basis = date.fromisoformat(bundle["share_basis_date"])
        factor = split_factor(bundle["splits"], basis, as_of)
    except (KeyError, ValueError, TypeError):
        factor = None
    if factor is None:
        warnings.append("Split history or the statement share basis is unavailable.")
    eps = lambda row: continuing_eps(row, factor) if factor is not None else None
    values = {}

    quarterly_growth = _growths(q, eps, 1, False)
    value = quarterly_growth[0] if quarterly_growth else None
    values["c_eps"] = (value, value >= t.eps_growth_pct if value is not None else None,
                        f">= {t.eps_growth_pct:g}% YoY", _eps_growth_detail(q, factor, 1, False))
    rates = _growths(q, eps, 4, False)
    rises = sum(rates[i] > rates[i + 1] for i in range(3)) if rates is not None else None
    values["c_acceleration"] = (rises, rises >= t.eps_acceleration_min if rises is not None else None,
                                 f">= {t.eps_acceleration_min} of 3 increases", _eps_growth_detail(q, factor, 4, False))
    sales = _growths(q, lambda row: number(row.get("revenue")), 3, False)
    latest_sales = _growths(q, lambda row: number(row.get("revenue")), 1, False)
    value = latest_sales[0] if latest_sales else None
    sales_pass = True if value is not None and value >= t.sales_growth_pct else (
        sales[0] > sales[1] > sales[2] if sales is not None else None)
    values["c_sales"] = (value, sales_pass, f">= {t.sales_growth_pct:g}% YoY or 3 rising YoY rates",
                          f"Sales growth, newest first: {sales}")
    margins = []
    for row in consecutive(q, 12, False):
        revenue = number(row.get("revenue"))
        income = number(row.get("netIncomeFromContinuingOperations"))
        if revenue is None or revenue <= 0 or income is None:
            margins = []
            break
        margins.append(100 * income / revenue)
    value = margins[0] if margins else None
    margin_pass = value > 0 and value >= max(margins) * (1 - t.margin_near_high_pct / 100) if margins else None
    values["c_margin"] = (value, margin_pass, f"Within {t.margin_near_high_pct:g}% of 12-quarter high",
                           f"12-quarter peak: {max(margins):.2f}%" if margins else "Requires 12 consecutive quarters in one currency.")
    annual_rates = _growths(a, eps, 3, True)
    values["a_eps"] = (annual_rates, all(v >= t.annual_eps_growth_pct for v in annual_rates) if annual_rates else None,
                        f">= {t.annual_eps_growth_pct:g}% in each of 3 years", _eps_growth_detail(a, factor, 3, True))
    annual = consecutive(a, 4, True)
    pair = consecutive(a, 2, True)
    roe = None
    if pair:
        equities = [number(_matched(row, balance).get("totalStockholdersEquity")) for row in pair]
        income = number(pair[0].get("netIncomeFromContinuingOperations"))
        if income is not None and all(v is not None and v > 0 for v in equities):
            roe = 100 * income / (sum(equities) / 2)
    values["a_roe"] = (roe, roe >= t.roe_min_pct if roe is not None else None,
                        f">= {t.roe_min_pct:g}%", "Latest annual continuing income / average opening and closing common equity.")
    premium = None
    cfps = None
    cash_detail = "Unavailable: no current eligible annual income statement."
    if a and factor is not None:
        latest = a[0]
        matching_cash = _matched(latest, cash)
        flow = number(matching_cash.get("operatingCashFlow"))
        shares = number(latest.get("weightedAverageShsOutDil"))
        latest_eps = eps(latest)
        if not latest.get("reportedCurrency"):
            cash_detail = f"Unavailable: reported currency is missing for {_period_label(latest)}."
        elif not matching_cash:
            cash_detail = f"Unavailable: no matching cash flow statement for {_period_label(latest)}, date and currency."
        elif flow is None:
            cash_detail = f"Unavailable: operating cash flow is missing for {_period_label(latest)}."
        elif shares is None or shares <= 0:
            cash_detail = f"Unavailable: positive diluted weighted-average shares are required for {_period_label(latest)}."
        else:
            cfps = flow / shares / factor
            eps_text = f"{latest_eps:.2f}" if latest_eps is not None else "missing"
            cash_detail = (f"{_period_label(latest)} operating cash flow per diluted share: {cfps:.2f}; "
                           f"continuing EPS: {eps_text}, {latest['reportedCurrency']} per share. "
                           "Both use the same fiscal year and currency.")
            if latest_eps is None:
                cash_detail = "Unavailable: continuing EPS is missing. " + cash_detail
            elif latest_eps <= 0:
                cash_detail = "Not meaningful: the percentage above EPS requires positive EPS. " + cash_detail
            else:
                premium = 100 * (cfps / latest_eps - 1)
    elif a:
        cash_detail = "Unavailable: split history or the statement share basis is missing."
    values["a_cash"] = (premium, premium >= t.cash_flow_above_eps_pct - 1e-9 if premium is not None else None,
                         f">= {t.cash_flow_above_eps_pct:g}% above EPS", cash_detail)
    shares = [number(row.get("weightedAverageShsOutDil")) for row in annual]
    share_change = latest_share_change = None
    if shares and factor is not None and all(v is not None and v > 0 for v in shares):
        shares = [v * factor for v in shares]
        share_change = growth(shares[0], shares[-1])
        latest_share_change = growth(shares[0], shares[1])
    share_pass = (share_change <= t.shares_max_growth_pct + 1e-9
                  and latest_share_change <= t.shares_latest_max_growth_pct + 1e-9) if share_change is not None else None
    latest_text = f"{latest_share_change:+.2f}%" if latest_share_change is not None else "Unavailable"
    values["s_shares"] = (share_change, share_pass,
                           f"Three-year change <= {t.shares_max_growth_pct:g}%; latest annual change <= {t.shares_latest_max_growth_pct:g}%",
                           f"Latest annual change: {latest_text}. Split-adjusted diluted weighted-average shares, newest first: {shares}")
    ratios = []
    for row in annual:
        matched = _matched(row, balance)
        equity, debt = number(matched.get("totalStockholdersEquity")), number(matched.get("totalDebt"))
        if equity is None or equity <= 0 or debt is None or debt < 0:
            ratios = []
            break
        ratios.append(debt / equity)
    debt_pass = ratios[0] <= ratios[-1] * (1 + t.debt_equity_max_growth_pct / 100) + 1e-12 if len(ratios) == 4 else None
    values["s_debt"] = (ratios or None, debt_pass, f"Three-year change <= {t.debt_equity_max_growth_pct:g}%",
                         "Total debt / positive stockholders' equity, newest first; compare latest with oldest of four matched fiscal years.")
    for row in q:
        income = number(row.get("netIncomeFromContinuingOperations"))
        if income is None:
            continue
        for field in ("otherAdjustmentsToNetIncome", "netIncomeFromDiscontinuedOperations"):
            item = number(row.get(field))
            if item and (income == 0 or abs(item) >= abs(income) * t.one_time_item_pct / 100):
                warnings.append(f"{row['date']}: review {field} ({item:,.0f}); material relative to continuing income.")
    result = []
    for key, letter, label in CRITERIA[:9]:
        value, passed, rule, detail = values[key]
        if passed is None and not detail.startswith(("Unavailable:", "Not meaningful:")):
            detail = "Unavailable: missing, stale, nonpositive comparison, unmatched period or currency. " + detail
        result.append(Criterion(key, letter, label, value, passed, rule, detail))
    return result, warnings


def leadership_criteria(rs: int | None, industry: dict | None, t: Thresholds) -> list[Criterion]:
    rank = industry["rank"] if industry else None
    return [
        Criterion("l_rs", "L", "Relative strength", rs, rs >= t.rs_min if rs is not None else None,
                  f">= {t.rs_min}", "Market-wide weighted 12-month return percentile."),
        Criterion("l_industry", "L", "Industry rank", rank, rank <= t.industry_top if rank is not None else None,
                  f"Top {t.industry_top}", f"Equal-weight 6-month member return; group: {industry}"),
    ]


def unscored_fundamentals(reason: str) -> list[Criterion]:
    return [Criterion(key, letter, label, None, None, "Not evaluated", reason)
            for key, letter, label in CRITERIA[:9]]


MUST_PASS = ("c_eps", "c_sales", "l_rs")


def meets_rule(criteria: list[Criterion], t: Thresholds) -> bool:
    """Quarterly EPS, quarterly sales and RS must pass; then enough of the other eight.

    Unmeasurable checks are left out when enough can be measured, and the
    requirement shrinks in proportion. The defaults demand every check.
    """
    passed = {c.key: c.passed for c in criteria if c.key in {key for key, _, _ in CRITERIA}}
    if any(passed.get(key) is not True for key in MUST_PASS):
        return False
    scored = [value for key, value in passed.items() if key not in MUST_PASS]
    measurable = [value for value in scored if value is not None]
    if len(measurable) < t.min_measurable_scored_checks:
        return False
    required = math.ceil(t.scored_checks_required * len(measurable) / max(len(scored), 1) - 1e-9)
    return sum(measurable) >= required


def leader_order(rs: int | None, criteria: list[Criterion], symbol: str) -> tuple:
    """Sort key for the leaders portfolio: RS first, then scored checks passed, then ticker."""
    return (-(rs or 0), -scored_summary(criteria)[0], symbol)


def trend_order(symbol: str, rs: int, industry: dict | None, criteria, dollar_volume: float | None,
                t: Thresholds, portfolio: BacktestSettings) -> tuple[tuple | None, str | None]:
    """Sort key for a trend leader, or None with the first rule it misses."""
    sales = next((c.value for c in criteria if c.key == "c_sales"), None)
    rank = industry["rank"] if industry else None
    if rs < portfolio.trend_min_rs:
        return None, "trend_rs"
    if rank is None or rank > t.industry_top:
        return None, "trend_industry"
    if sales is None or sales < portfolio.trend_min_sales_growth_pct:
        return None, "trend_sales"
    if (dollar_volume or 0) < portfolio.trend_min_dollar_volume_m * 1e6:
        return None, "trend_dollar_volume"
    return (-rs, -sales, symbol), None


def scored_summary(criteria: list[Criterion]) -> tuple[int, int]:
    """Passed and measurable counts among the eight scored checks."""
    scored = [c.passed for c in criteria if c.key in {key for key, _, _ in CRITERIA} and c.key not in MUST_PASS]
    return sum(p is True for p in scored), sum(p is not None for p in scored)
