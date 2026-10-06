"""Final Nash portfolio, run once after nash_rules.py (rules in that file's docstring).

Reads the surviving rules from output/nash_rules/scan.json. Always also runs "mechanics only" (no new
rules: base filter, composite of revenue growth and FCF margin, two-strike exits, sector cap).
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np

import benchmarks
import nash_panels
import nash_screen as N
import panel as P
from stratlib.app import open_context
from nash_rules import CONTINUOUS, OUT, PriceTools, load_series
from nash_rules import characteristics as _characteristics

_CACHE: dict = {}


def characteristics(p, tools, series, classes, i):
    key = (id(p), i)
    if key not in _CACHE:
        _CACHE[key] = _characteristics(p, tools, series, classes, i)
    return _CACHE[key]

FORMS = {"blend 10": (10, 0.05), "sleeve 10": (10, 0.10), "sleeve 30": (30, 1 / 30)}
PERIODS = {"holdout 2011-15": ("2011-01-01", "2015-12-31"), "in-sample 2016-21": ("2016-01-01", "2021-12-31"),
           "2022 on": ("2022-01-01", "9999-12-31"), "2016 on": ("2016-01-01", "9999-12-31")}
TBILL_KEY = "research:tbill3m_2010"


def composite(cands: dict, continuous: list[str]) -> dict[str, float]:
    """Mean percentile (0-1, higher better) of revenue growth, FCF margin and each continuous survivor."""
    names = list(cands)
    cols = [("rev_g", "high"), ("fcfm", "high")] + [(t, CONTINUOUS[t]) for t in continuous]
    score = np.zeros(len(names))
    for key, direction in cols:
        v = np.array([cands[s][key] for s in names], dtype=float)
        fill = np.nanmedian(v) if np.isfinite(v).any() else 0.0
        v = np.where(np.isfinite(v), v, fill)
        r = v.argsort().argsort() / max(len(v) - 1, 1)
        score += r if direction == "high" else 1 - r
    return dict(zip(names, score / len(cols)))


def select(p, tools, series, classes, rows_i, binary, continuous, slots):
    held, strikes, out = [], {}, {}
    cap = int(round(0.3 * slots))
    for i in rows_i:
        chars = characteristics(p, tools, series, classes, i)
        ok = {s: r for s, r in chars.items() if r["base"] and all(r.get(t) == 1 for t in binary)}
        keep = []
        for s in held:
            strikes[s] = 0 if s in ok else strikes.get(s, 0) + 1
            if strikes[s] < 2 and s in chars:          # a stock that left the universe is sold
                keep.append(s)
        score = composite(ok, continuous) if ok else {}
        per_sector = {}
        for s in keep:
            sec = classes.get(s, ("", ""))[0]
            per_sector[sec] = per_sector.get(sec, 0) + 1
        for s in sorted(score, key=lambda x: -score[x]):
            if len(keep) >= slots:
                break
            sec = classes.get(s, ("", ""))[0]
            if s in keep or per_sector.get(sec, 0) >= cap:
                continue
            keep.append(s)
            per_sector[sec] = per_sector.get(sec, 0) + 1
        held = keep
        strikes = {s: strikes.get(s, 0) for s in held}
        out[i] = {"day": str(p.dates[i]), "held": list(held), "candidates": len(ok)}
    return out


def ew_universe(p, tools, series, classes, rows_i, last_row):
    """Equal-weighted universe, rebalanced each quarter, price only: curve of quarter-end values."""
    value, curve = 100_000.0, []
    for n, i in enumerate(rows_i):
        nxt = rows_i[n + 1] if n + 1 < len(rows_i) else last_row
        chars = characteristics(p, tools, series, classes, i)
        rets = [tools.forward(r["j"], i, nxt) for r in chars.values()]
        curve.append((str(p.dates[i]), value))
        value *= 1 + float(np.mean(rets))
    curve.append((str(p.dates[last_row]), value))
    return curve


def tbill_history(ctx) -> dict:
    doc = ctx.store.document(TBILL_KEY)
    if doc:
        return doc["rows"]
    rates, day, end = {}, date(2010, 12, 1), date(2016, 1, 31)
    while day <= end:
        stop = min(day + timedelta(days=84), end)
        for r in ctx.client.get("treasury-rates", {"from": day, "to": stop}) or []:
            if r.get("date") and r.get("month3") is not None:
                rates[r["date"]] = float(r["month3"])
        day = stop + timedelta(days=1)
    ctx.store.save_document(TBILL_KEY, {"rows": rates})
    return rates


def cagr(curve):
    years = (date.fromisoformat(curve[-1][0]) - date.fromisoformat(curve[0][0])).days / 365.25
    return 100 * ((curve[-1][1] / curve[0][1]) ** (1 / years) - 1)


def main() -> None:
    scan = json.load(open(OUT / "scan.json"))
    survivors = scan["survivors"]
    binary = [t for t in survivors if t not in CONTINUOUS]
    continuous = [t for t in survivors if t in CONTINUOUS]
    configs = {"mechanics only": ([], [])}
    if survivors:
        configs["with surviving rules"] = (binary, continuous)
    series, classes = load_series()
    bench = benchmarks.load()
    ctx = open_context()
    try:
        tbill = {**tbill_history(ctx), **bench["tbill3m"]}
    finally:
        ctx.close()
    panels = {"holdout": nash_panels.load_holdout(), "main": P.load()}
    tools = {k: PriceTools(p) for k, p in panels.items()}
    selections, held_all = {}, set()
    for cname, (b, c) in configs.items():
        for form, (slots, _) in FORMS.items():
            for pk, (start, end) in (("holdout", ("2011-01-01", "2015-12-31")), ("main", ("2016-01-01", "9999-12-31"))):
                p = panels[pk]
                rows_i = nash_panels.rebalance_rows(p, start, end)
                sel = select(p, tools[pk], series, classes, rows_i, b, c, slots)
                selections[(cname, form, pk)] = sel
                held_all |= {s for x in sel.values() for s in x["held"]}
    ctx = open_context()
    try:
        divs = N.load_dividends(ctx, held_all)
    finally:
        ctx.close()
    divs["SPY"] = bench["spy_dividends"]

    results = {"survivors": survivors, "runs": {}, "benchmarks": {}, "selections": {}}
    for period, (a, b) in PERIODS.items():
        pk = "holdout" if a < "2016" else "main"
        p = panels[pk]
        end = min(b, str(p.dates[-1]) if pk == "main" else "2015-12-31")
        spy = N.spy_curve(p, divs["SPY"], a, end)
        rows_i = nash_panels.rebalance_rows(p, a, end)
        last_row = int(np.searchsorted(p.dates, end, side="right")) - 1
        ew = ew_universe(p, tools[pk], series, classes, rows_i, last_row)
        results["benchmarks"][period] = {"spy": N.stats(spy, tbill), "ew_universe_cagr": cagr(ew)}
        for (cname, form, spk), sel in selections.items():
            if spk != pk:
                continue
            weight = FORMS[form][1]
            sub = {i: x for i, x in sel.items() if a <= x["day"] <= end}
            run = N.simulate(p, sub, divs, a, end, weight)
            price_only = N.simulate(p, sub, {}, a, end, weight)
            s = N.stats(run["curve"], tbill)
            s.update({"price_only_cagr": cagr(price_only["curve"]), "turnover_per_year": run["turnover"] / s["years"],
                      "avg_names": float(np.mean(run["names"])),
                      "avg_candidates": float(np.mean([x["candidates"] for x in sub.values()]))})
            results["runs"][f"{cname} | {form} | {period}"] = s
    for (cname, form, pk), sel in selections.items():
        results["selections"][f"{cname} | {form} | {pk}"] = [{"day": x["day"], "held": x["held"],
                                                               "candidates": x["candidates"]} for x in sel.values()]
    json.dump(results, open(OUT / "final.json", "w"), indent=1, default=float)

    print("Survivors:", survivors or "none")
    for period in PERIODS:
        bm = results["benchmarks"][period]
        print(f"\n{period}: SPY {bm['spy']['cagr']:.1f}% (max DD {bm['spy']['maxdd']:.0f}%, Sharpe {bm['spy']['sharpe']:.2f}); "
              f"equal-weight universe {bm['ew_universe_cagr']:.1f}% (price only)")
        for key, s in results["runs"].items():
            if key.endswith(period):
                print(f"  {key.rsplit(' | ', 1)[0]:34} {s['cagr']:5.1f}%  vs SPY {s['cagr'] - bm['spy']['cagr']:+5.1f}  "
                      f"price-only {s['price_only_cagr']:5.1f}% vs EW {s['price_only_cagr'] - bm['ew_universe_cagr']:+5.1f}  "
                      f"maxDD {s['maxdd']:4.0f}%  Sharpe {s['sharpe']:.2f}  names {s['avg_names']:4.1f} "
                      f"(candidates {s['avg_candidates']:4.0f})  turnover {100 * s['turnover_per_year']:.0f}%")


if __name__ == "__main__":
    main()
