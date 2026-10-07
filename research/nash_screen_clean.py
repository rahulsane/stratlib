"""Post-hoc cleanup of the Nash screen, chosen after seeing the first screen's results.

The Rule-of-40 ranking fixed in advance was dominated by data errors (JOYY revenue about a tenth of the
real figure, TAL free cash flow about 245x revenue, 34 histories that switch reporting currency) and by
biotechs whose "growth" is a one-off milestone payment on a near-zero base (CRSP, BEAM: Rule of 40 above
100,000%). Three guards, everything else as in the first screen:
  - all 8 quarters in the TTM windows reported in the same currency;
  - TTM free-cash-flow margin between -100% and +100%;
  - prior-year TTM revenue of at least $250M (and a $1B check), converted at rough fixed exchange rates.
"""

from __future__ import annotations

import json
import sys

import numpy as np

import benchmarks
import nash_screen as N
import panel as P
from stratlib.app import open_context
from nash_fundamentals import KEY, classifications

# Rough units per US dollar; only used for the size floor.
FX = {"USD": 1, "CNY": 6.8, "CAD": 1.3, "EUR": 0.9, "BRL": 4.5, "GBP": 0.78, "JPY": 120, "MXN": 19, "AUD": 1.4,
      "ZAR": 15, "RUB": 70, "INR": 75, "TWD": 30, "KRW": 1200, "HKD": 7.8, "DKK": 6.7, "SGD": 1.36, "ARS": 100,
      "VND": 23000, "IDR": 14500, "TRY": 15, "CHF": 0.95, "KZT": 450, "SEK": 10, "COP": 4000}


def clean_snapshots(doc: dict) -> list:
    """nash_screen.snapshots plus the currency check and prior-year revenue in dollars."""
    inc = sorted((r for r in doc.get("income", []) if r.get("date")), key=lambda r: r["date"])
    by_end = {r["date"]: r for r in inc}
    out = []
    for avail, qend, m in N.snapshots(doc):
        k = [r["date"] for r in inc].index(qend)
        window = inc[k - 7:k + 1]
        ccys = {r.get("reportedCurrency") for r in window}
        if len(ccys) != 1 or next(iter(ccys)) not in FX:
            continue
        rate = FX[next(iter(ccys))]
        rev0 = sum(r["revenue"] for r in window[:4]) / rate
        if m["fcfm"] is not None and not -1 <= m["fcfm"] <= 1:
            continue
        out.append((avail, qend, {**m, "rev0_usd": rev0}))
    return out


def select_clean(p, snaps, classes, margin, rule7, floor):
    sized = {s: [(a, q, m) for a, q, m in v if m["rev0_usd"] >= floor] for s, v in snaps.items()}
    return N.select_all(p, sized, classes, margin, rule7)


def main() -> None:
    p = P.load()
    bench = benchmarks.load()
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        snaps = {}
        for key in ctx.store.document_keys(KEY):
            doc = ctx.store.document(key)
            if doc and "error" not in doc:
                snaps[key[len(KEY):]] = clean_snapshots(doc)
        variants = {(m, r7, fl): select_clean(p, snaps, classes, m, r7, fl)
                    for fl in (250e6, 1e9) for m in ("OM", "FCF") for r7 in (False, True)}
        held = {s for sel in variants.values() for x in sel.values() for s in x["held"]}
        divs = N.load_dividends(ctx, held)
    finally:
        ctx.close()
    divs["SPY"] = bench["spy_dividends"]
    tbill = bench["tbill3m"]
    out = {"rules": __doc__, "runs": {}, "selections": {}}
    for (margin, r7, fl), sel in variants.items():
        name = f"{margin}{'+rule7' if r7 else ''} floor{fl / 1e6:.0f}M"
        out["selections"][name] = [{"day": x["day"], "passing": x["passing"], "held": x["held"],
                                    "r40": {s: round(100 * (m["r40"] or 0), 1) for s, m in x["metrics"].items()}}
                                   for x in sel.values()]
        for form, weight in (("blend", 0.05), ("sleeve", 0.10)):
            for period, (a, b) in N.PERIODS.items():
                run = N.simulate(p, {i: x for i, x in sel.items() if a <= x["day"] <= b}, divs, a, b, weight)
                s = N.stats(run["curve"], tbill)
                v = np.array([x for _, x in run["curve"]])
                trough = int(np.argmax(1 - v / np.maximum.accumulate(v)))
                peak = int(np.argmax(v[:trough + 1]))
                s.update({"turnover_per_year": run["turnover"] / s["years"], "avg_names": float(np.mean(run["names"])),
                          "dd_when": [run["curve"][peak][0], run["curve"][trough][0]]})
                out["runs"][f"{name} {form} {period}"] = s
    json.dump(out, open(N.OUT / "results_clean.json", "w"), indent=1, default=float)
    bm = json.load(open(N.OUT / "results.json"))["benchmark"]
    print(f"{'run':44}{'CAGR':>7}{'SPY':>7}{'excess':>8}{'maxDD':>7}{'Sharpe':>7}{'SPY':>6}{'turn/yr':>8}  worst drawdown")
    for key, s in out["runs"].items():
        b = bm[key.rsplit(" ", 1)[1]]
        print(f"{key:44}{s['cagr']:6.1f}%{b['cagr']:6.1f}%{s['cagr'] - b['cagr']:+7.1f}%{s['maxdd']:6.1f}%"
              f"{s['sharpe']:7.2f}{b['sharpe']:6.2f}{100 * s['turnover_per_year']:7.0f}%  {s['dd_when'][0]}..{s['dd_when'][1]}")


if __name__ == "__main__":
    main()
