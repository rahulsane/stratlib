"""MSCI GARP's comparable backtest: the index rebuilt review by review (msci_garp.py) and copied by a self-managed
account, measured like every other strategy (engine.metrics, the research periods, SPY with dividends).

An index cannot run on the trade-by-trade engine, which buys and sells whole positions: copying it means topping up
and trimming every holding to its weight at each review. So this module keeps its own account, a port of
research/garp_backtest.py's self_managed, under the same ground rules: $100,000 per period, whole shares, slippage of
0.10% a side (0.25% under $20), no commissions, dividends and the proceeds of stocks leaving the index held in cash
until the next review. A period that starts between reviews buys the index's weights of that day. Each holding
period of a stock (from the purchase that opens it to the sale that empties it) counts as one trade, its return
measured on all the money put into it; what is held at the end is sold at the last close, as the engine does.

The data are the research's (research/garp_data.py and research/mscigarp_data.py prepare them): the S&P 500 since
2004 from FMP's change log, each member mapped to a price series (``research:garp:resolved2``), statements, cash
flow, dividends and splits. They are read from the store; nothing here calls the API.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

import numpy as np

from .. import msci_garp as MG
from ..presentation import INDEX_DATA_NOTE, INDEX_DATA_TITLE, rules_text
from ..strategies import INDEX_HOLDINGS, STRATEGIES, current_rules
from .engine import PERIODS, Rules, Trade, metrics, slippage
from .panel import _factor_column, clean_bad_bars

SINCE = "2004-01-01"
FIRST_REVIEW = "2007-11"               # the research's rebuild starts here; the buffer carries over between reviews
RESOLVED_KEY = "research:garp:resolved2"
PX_KEY = "research:garp:px:"
STATEMENT_KEY = "research:garp:stmts:"
EXTRA_KEY = "research:mscigarp:extra:"
DIVIDEND_KEY = "research:nash:div:"
SPLIT_KEYS = ("backtest:splits:", "research:garp:splits:")
FIELDS = ("open", "high", "low", "close", "volume")
CASH_BUFFER = 0.002                    # each target is bought at 99.8% of its value, leaving cash for slippage
PREPARE = ("MSCI GARP's backtest reads the S&P 500 history the research prepared. Run research/garp_data.py and "
           "research/mscigarp_data.py first.")


@dataclass
class IndexData:
    dates: np.ndarray
    tickers: list
    close: np.ndarray          # split-adjusted, NaN where there is no bar
    px: np.ndarray             # close carried forward (valuation of halted or delisted names), 0 before the first bar
    volume: np.ndarray
    factor: np.ndarray         # as-traded = adjusted * factor
    div: np.ndarray            # dividend per adjusted share on the ex-date row
    member: np.ndarray         # in the S&P 500 that session
    last: np.ndarray           # last row with a bar
    sector: list
    industry: list
    name: list
    funds: dict
    extra: dict
    notes: dict = field(default_factory=dict)


def load(store, through: str, progress=None) -> IndexData:
    """The research's S&P 500 panel (research/garp_index.py load_data), from the store."""
    note = progress or (lambda message, done, total: None)
    resolved = store.document(RESOLVED_KEY)
    if not resolved:
        raise ValueError(PREPARE)
    series = resolved["series"]
    tickers = sorted(series)
    calendar = [b.date for b in store.price_history("SPY", since=SINCE, through=through)]
    dates = np.array(calendar)
    n, m = len(dates), len(tickers)
    row = {d: i for i, d in enumerate(calendar)}
    arrays = {f: np.full((n, m), np.nan) for f in FIELDS}
    factor, div = np.ones((n, m)), np.zeros((n, m))
    note("Loading the S&P 500 history", 0, m)
    from_db = [t for t in tickers if series[t]["source"] == "db"]
    column = {t: j for j, t in enumerate(tickers)}
    for symbol, day, *bar in store.price_panel_rows(from_db, "2002-01-01", through):
        i = row.get(day)
        if i is not None:
            for f, v in zip(FIELDS, bar):
                arrays[f][i, column[symbol]] = np.nan if v is None else float(v)
    for j, t in enumerate(tickers):
        if series[t]["source"] != "db":
            for day, bar in ((store.document(PX_KEY + t) or {}).get("bars") or {}).items():
                i = row.get(day)
                if i is not None:
                    for f, v in zip(FIELDS, bar):
                        arrays[f][i, j] = np.nan if v is None else float(v)
        splits = {}
        for key in SPLIT_KEYS:
            doc = store.document(key + t)
            if doc and not doc.get("error"):
                splits = {r.get("date"): r for r in doc.get("rows", []) if r.get("date")}
                break
        factor[:, j] = _factor_column(dates, splits, "9999-12-31")
        for day, amount in ((store.document(DIVIDEND_KEY + t) or {}).get("rows") or {}).items():
            i = row.get(day)
            if i is not None and amount:
                div[i, j] += float(amount)
        if j % 100 == 0:
            note("Loading the S&P 500 history", j, m)
    until = []
    for j in range(m):
        ok = np.flatnonzero(np.isfinite(arrays["close"][:, j]))
        until.append("" if len(ok) == 0 or ok[-1] >= n - 5 else str(dates[ok[-1]]))
    cleaning = clean_bad_bars(arrays, np.array(until), factor)
    close = arrays["close"]
    valid = np.isfinite(close) & (close > 0)
    close = np.where(valid, close, np.nan)
    px = close.copy()
    for i in range(1, n):
        miss = np.isnan(px[i])
        px[i, miss] = px[i - 1, miss]
    px = np.nan_to_num(px)
    last = np.where(valid.any(axis=0), n - 1 - valid[::-1].argmax(axis=0), -1)
    member = np.zeros((n, m), dtype=bool)
    for j, t in enumerate(tickers):
        for a, b in series[t]["spans"]:
            member[bisect_left(calendar, a):bisect_left(calendar, b) if b else n, j] = True
    note("Reading statements", 0, m)
    funds, extra = {}, {}
    for t in tickers:
        doc = store.document(STATEMENT_KEY + t)
        financial = (series[t].get("sector") or "") == "Financial Services"
        funds[t] = MG.fundamentals(doc, financial) if doc else MG.Fund([], [], [])
        extra[t] = MG.extra_series(store.document(EXTRA_KEY + t))
    return IndexData(dates, tickers, close, px, np.nan_to_num(arrays["volume"]), factor, div, member, last,
                     [series[t].get("sector") or "Unknown" for t in tickers],
                     [series[t].get("industry") or "" for t in tickers], [series[t].get("name") or "" for t in tickers],
                     funds, extra, {"cleaning": {k: v for k, v in cleaning.items() if k != "instrument_changes"},
                                    "missing_spans": len(resolved.get("missing", [])), "resolved_on": resolved.get("made_on")})


# ----------------------------------------------------------------------------------------------
# The index

def stocks_at(data: IndexData, reb: dict) -> list[dict]:
    """The review's parent: S&P 500 members with a close on the data date, one listing per company."""
    ref = reb["reference"]
    cand = [j for j in range(len(data.tickers)) if data.member[ref, j] and np.isfinite(data.close[ref, j])]
    groups = {}
    lo = max(0, ref - 19)
    for j in cand:
        key = MG.tokens(data.name[j]) or frozenset([data.tickers[j]])
        traded = data.close[lo:ref + 1, j] * data.volume[lo:ref + 1, j]
        dv = float(np.nanmean(traded)) if np.isfinite(traded).any() else float("nan")
        if key not in groups or dv > groups[key][1]:
            groups[key] = (j, dv)
    out = []
    for j in sorted(j for j, _ in groups.values()):
        t = data.tickers[j]
        out.append({"symbol": t, "sector": data.sector[j], "industry": data.industry[j], "price": float(data.close[ref, j]),
                    "dps": float(data.div[max(0, ref - 251):ref + 1, j].sum()),
                    "fund": MG.fundamentals_at(data.funds[t], data.extra[t], reb["cutoff"])})
    return out


def build(data: IndexData, p, start_label: str = FIRST_REVIEW, keep: set[int] = frozenset()) -> dict:
    """The index from ``start_label``: each review's target weights, the weights at each effective close, and the
    index's shares at the rows in ``keep`` (research/mscigarp_index.py build)."""
    sched = [r for r in MG.schedule(data.dates) if r["label"] >= start_label]
    n, m = data.px.shape
    col = {t: j for j, t in enumerate(data.tickers)}
    current: set[str] = set()
    shares = np.zeros(m)
    tr = np.full(n, np.nan)
    s0 = sched[0]["effective"]
    tr[s0] = 100.0
    by_row = {r["effective"]: r for r in sched}
    weights_eff, plan, kept, turnover = {}, [], {}, []
    for i in range(s0, n):
        if i > s0:
            prev = float(shares @ data.px[i - 1])
            tr[i] = tr[i - 1] * float(shares @ data.px[i] + shares @ data.div[i]) / prev
        if i in by_row:
            reb = by_row[i]
            sc = MG.score(stocks_at(data, reb), p.growth_variant)
            chosen = MG.select(sc, current, p.coverage_pct / 100, p.buffer_low_pct / 100, p.buffer_high_pct / 100)
            alive = np.array([(data.member[min(i + 1, n - 1), col[sc["symbols"][k]]] or i == n - 1)
                              and data.last[col[sc["symbols"][k]]] >= reb["weights"] for k in chosen], dtype=bool)
            chosen = chosen[alive]
            w = MG.tilt_weights(sc, chosen, p.max_issuer_pct / 100, p.sector_band_pct / 100)
            cols = np.array([col[sc["symbols"][k]] for k in chosen], dtype=int)
            new = np.zeros(m)
            new[cols] = w / data.px[reb["weights"], cols]
            new *= (float(shares @ data.px[i]) if i > s0 else 100.0) / float(new @ data.px[i])
            if i > s0:
                before = shares * data.px[i] / float(shares @ data.px[i])
                after = new * data.px[i] / float(new @ data.px[i])
                turnover.append(0.5 * float(np.abs(after - before).sum()))
            shares = new
            current = {sc["symbols"][k] for k in chosen}
            weights_eff[i] = {int(j): float(shares[j] * data.px[i, j] / (shares @ data.px[i])) for j in cols}
            plan.append({"label": reb["label"], "effective": str(data.dates[i]), "held": len(cols),
                         "target": {sc["symbols"][k]: float(x) for k, x in zip(chosen, w)}})
        elif i < n - 1:
            held = np.flatnonzero(shares > 0)
            gone = [j for j in held if (data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i]
            if gone:
                total = float(shares @ data.px[i])
                shares[gone] = 0.0
                shares *= total / float(shares @ data.px[i])
        if i in keep:
            kept[i] = shares.copy()
    return {"plan": plan, "weights": weights_eff, "shares": kept, "tr": tr, "start": s0, "turnover": turnover}


# ----------------------------------------------------------------------------------------------
# The self-managed account


@dataclass
class Spell:
    """One stock's holding period: from the purchase that opens it to the sale that empties it."""
    j: int
    entry: int
    first_price: float          # as traded, before slippage
    first_shares: float         # as traded
    cost: float = 0.0           # paid, including slippage
    proceeds: float = 0.0       # received, after slippage
    sold_as_traded: float = 0.0
    sold_shares: float = 0.0    # as traded


def simulate(data: IndexData, idx: dict, start: int, end: int, rules: Rules) -> dict:
    """Copy the index from ``start`` to ``end``: buy its weights of the first session, rebalance at every review,
    sell stocks leaving the S&P 500. Returns the engine's run record (metrics() reads it)."""
    dates, px, factor = data.dates, data.px, data.factor
    m = px.shape[1]
    reviews = {r: w for r, w in idx["weights"].items() if start < r <= end}
    index_shares = idx["shares"][start]
    value = index_shares * px[start]
    reviews[start] = {int(j): float(value[j] / value.sum()) for j in np.flatnonzero(index_shares > 0)
                      if data.last[j] >= start}
    shares, cash = np.zeros(m), rules.capital
    spells: dict[int, Spell] = {}
    trades: list[Trade] = []
    equity, invested, held = [], [], []
    counts = {"reviews": 0, "orders": 0, "slippage": 0.0, "traded": 0.0, "dividends": 0.0, "left_the_index": 0}

    def fill(j, i):
        as_traded = px[i, j] * factor[i, j]
        return as_traded, slippage(rules, as_traded)

    def close_spell(j, i, reason):
        s = spells.pop(j)
        d_in, d_out = date.fromisoformat(str(dates[s.entry])), date.fromisoformat(str(dates[i]))
        pnl = s.proceeds - s.cost
        trades.append(Trade(
            ticker=data.tickers[j], kind="stock", signal_date=str(dates[s.entry]), entry_date=str(dates[s.entry]),
            exit_date=str(dates[i]), entry_price=s.first_price / factor[s.entry, j], stop=0.0,
            exit_price=s.sold_as_traded / s.sold_shares / factor[i, j] if s.sold_shares else float("nan"),
            entry_price_as_traded=s.first_price, exit_price_as_traded=s.sold_as_traded / s.sold_shares if s.sold_shares else float("nan"),
            exit_reason=reason, shares=s.first_shares, position_value=s.cost, return_pct=100 * pnl / s.cost if s.cost else 0.0,
            r=float("nan"), pnl=pnl, holding_days=(d_out - d_in).days, holding_sessions=i - s.entry, cash_limited=False))

    def sell(j, amount, i, reason):
        nonlocal cash
        as_traded, slip = fill(j, i)
        gross = amount * px[i, j]
        cash += gross * (1 - slip)
        counts["slippage"] += gross * slip
        counts["traded"] += gross
        counts["orders"] += 1
        s = spells[j]
        s.proceeds += gross * (1 - slip)
        s.sold_as_traded += amount / factor[i, j] * as_traded
        s.sold_shares += amount / factor[i, j]
        shares[j] -= amount
        if shares[j] <= 1e-9:
            shares[j] = 0.0
            close_spell(j, i, reason)

    def buy(j, amount, i):
        nonlocal cash
        as_traded, slip = fill(j, i)
        gross = amount * px[i, j]
        cash -= gross * (1 + slip)
        counts["slippage"] += gross * slip
        counts["traded"] += gross
        counts["orders"] += 1
        if j not in spells:
            spells[j] = Spell(j, i, as_traded, amount / factor[i, j])
        spells[j].cost += gross * (1 + slip)
        shares[j] += amount

    for i in range(start, end + 1):
        if i > start:
            paid = float(shares @ data.div[i])
            cash += paid
            counts["dividends"] += paid
        if i in reviews:
            counts["reviews"] += i > start
            total = cash + float(shares @ px[i])
            want = np.zeros(m)
            for j, w in reviews[i].items():
                as_traded = px[i, j] * factor[i, j]
                want[j] = round(w * total * (1 - CASH_BUFFER) / as_traded) * factor[i, j]
            for j in np.flatnonzero(shares > want + 1e-9):
                sell(j, shares[j] - want[j], i, "below one share at a review" if j in reviews[i] else
                     "left the index at a review")
            for j in np.flatnonzero(want > shares + 1e-9):
                buy(j, want[j] - shares[j], i)
            while cash < 0:                          # whole-share rounding overspent: trim the largest holding
                j = int(np.argmax(shares * px[i]))
                sell(j, min(factor[i, j], shares[j]), i, "sold to raise cash")
        elif i < len(dates) - 1:
            for j in np.flatnonzero(shares > 0):
                if (data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i:
                    counts["left_the_index"] += 1
                    sell(j, shares[j], i, "left the S&P 500")
        if i == end:
            for j in np.flatnonzero(shares > 0):
                sell(j, shares[j], i, "end of test")
        value_held = float(shares @ px[i])
        equity.append(cash + value_held)
        invested.append(value_held)
        held.append(int((shares > 0).sum()))
    years = max((date.fromisoformat(str(dates[end])) - date.fromisoformat(str(dates[start]))).days / 365.25, 1 / 365.25)
    average = float(np.mean(equity))
    counts = {"reviews": counts["reviews"], "orders": counts["orders"], "slippage_usd": round(counts["slippage"], 2),
              "traded_pct_per_year": round(100 * counts["traded"] / average / years, 1),
              "dividends_usd": round(counts["dividends"], 2), "sales_on_leaving_the_s_and_p_500": counts["left_the_index"],
              "average_holdings": round(float(np.mean(held)), 1)}
    return {"start": str(dates[start]), "end": str(dates[end]), "dates": dates[start:end + 1],
            "equity": np.array(equity), "invested": np.array(invested), "held": np.array(held), "trades": trades,
            "counts": counts, "rules": asdict(rules)}


def spy(store, dates: np.ndarray, dividends: dict) -> dict:
    """SPY bought at the first close, price only and with dividends reinvested (engine.spy_series)."""
    bars = {b.date: b.close for b in store.price_history("SPY", since=str(dates[0]), through=str(dates[-1]))}
    close = np.array([bars.get(str(d), np.nan) for d in dates], dtype=float)
    for k in range(1, len(close)):
        if not np.isfinite(close[k]):
            close[k] = close[k - 1]
    price = close / close[0]
    total = np.ones_like(price)
    for k in range(1, len(close)):
        total[k] = total[k - 1] * (close[k] + dividends.get(str(dates[k]), 0.0)) / close[k - 1]
    return {"price": price, "total": total}


def run_backtest(store, settings, through: str, *, progress=None) -> dict:
    """All three research periods, saved as the strategy's comparable backtest and returned."""
    from .data import benchmarks
    from .runs import json_safe, results_key, trade_table
    started = time.monotonic()
    note = progress or (lambda message, done, total: None)
    spec, p = STRATEGIES["msci_garp"], settings.strategies["msci_garp"]
    bench = benchmarks(store)
    data = load(store, through, note)
    first = next((r["effective"] for r in MG.schedule(data.dates) if r["label"] >= FIRST_REVIEW), None)
    if first is None:
        raise ValueError(PREPARE)
    rows = {}
    for period, (start, end) in PERIODS.items():
        a = max(int(np.searchsorted(data.dates, start)), first)       # no earlier than the index's first review
        b = len(data.dates) - 1 if end is None else int(np.searchsorted(data.dates, end, side="right")) - 1
        rows[period] = (a, b)
    note("Rebuilding the index review by review", 0, 1)
    idx = build(data, p, keep={a for a, _ in rows.values()})
    rules = Rules(max_positions=INDEX_HOLDINGS, max_position_pct=100.0)
    results, trades = {}, {}
    for k, (period, (a, b)) in enumerate(rows.items()):
        note(f"Testing {spec.name}, {period.replace('_', ' ')}", k, len(rows))
        run = simulate(data, idx, a, b, rules)
        results[period] = metrics(run, spy(store, data.dates[a:b + 1], bench["spy_dividends"]), bench["tbill3m"])
        trades[period] = trade_table(run["trades"])
    created = datetime.now(timezone.utc).isoformat()
    turnover = 4 * float(np.mean([t for t, plan in zip(idx["turnover"], idx["plan"][1:]) if plan["effective"] >= "2016"]))
    doc = json_safe({
        "name": spec.id, "strategy_id": spec.id, "variant_id": spec.variant, "strategy": spec.name,
        "label": spec.variant_name, "description": (f"**{spec.name} / {spec.variant_name}.** {rules_text(spec, settings)}"
                                                    f"\n\n**{INDEX_DATA_TITLE}** {INDEX_DATA_NOTE}"),
        "params": {**asdict(p), "first_review": FIRST_REVIEW, "index_turnover_pct_per_year": round(100 * turnover, 1)},
        "rules": asdict(rules), "rule_snapshot": current_rules(settings, spec.id),
        "liquidity": {"min_price": 0.0, "min_dollar_volume": 0.0}, "data_through": str(data.dates[-1]),
        "universe": ("The S&P 500 at each review, including companies since removed (the research's "
                     f"price series, mapped {data.notes['resolved_on']})"),
        "periods": {k: list(v) for k, v in PERIODS.items()}, "results": results, "trades": trades,
        "created_at": created, "elapsed_seconds": time.monotonic() - started,
    })
    store.save_document(results_key(spec.id) + created, doc)
    return doc
