"""Test 1: the Traveling Trader's index dip ladder against plain DCA. Rules fixed before any results were seen.

Data: S&P 500 total return 1950-2026 and QQQ 1999-2026 (tt_data.py). A reserve earns the cash rate.
No taxes or trading costs beyond the 0.09%/yr fund fee.

Each month, at the close of its first session, the investor has $1,000.
Ladder triggers, checked at every session's close: drawdown = close / highest close of the trailing 252
sessions - 1.
    -10%: deploy 1/3 of the reserve on hand;  -20%: 1/2 of what is left;  -30%: all of it.
Each threshold fires once per episode; all three re-arm when the close makes a new 252-session high.
Deployment is at that session's close. ("Buy the indices with every -10% correction, give an additional
weight to your investment ... every -10% after the initial -10%.")

Rules:
    A  plain DCA        buy $1,000 of the index each month.
    B  wait for dips    every $1,000 goes to the reserve; only the ladder buys.
    C  60/40 + ladder   buy $600, $400 to the reserve; the ladder deploys the reserve (his "40% cash").
    D  C, capped        as C, but at each contribution any reserve above 40% of portfolio value is bought.
    E  VIX ladder       as B, but the triggers are VIX closes >= 30 (1/2 of the reserve) and >= 40 (the rest),
                        re-armed after a close below 20. 1990 on, where VIX exists.
Portfolio value counts the reserve. Windows: every monthly start, 10/20/30 years of contributions, valued at
the next month's first session; money-weighted return per window, share of windows beating A, median gap.
Also single runs over the full history and the last 30/20/10 years. Rule E is compared with A and B over
the windows that start in 1990 or later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt_data as T  # noqa: E402

B = 1000.0
LADDER = [(-0.10, 1 / 3), (-0.20, 1 / 2), (-0.30, 1.0)]
VIX_LADDER = [(30.0, 0.5), (40.0, 1.0)]
RULES = {"A": "plain DCA", "B": "wait for dips (ladder only)", "C": "60/40 + ladder", "D": "60/40 + ladder, reserve capped 40%",
         "E": "VIX ladder (30/40)"}


def ladder_triggers(dd: np.ndarray) -> list[tuple[int, float, float]]:
    armed, out = [True] * len(LADDER), []
    for i in range(len(dd)):
        if not np.isfinite(dd[i]):
            continue
        if dd[i] >= -1e-12:
            armed = [True] * len(LADDER)
            continue
        for k, (thr, frac) in enumerate(LADDER):
            if armed[k] and dd[i] <= thr:
                armed[k] = False
                out.append((i, frac, thr))
    return out


def vix_triggers(v: np.ndarray) -> list[tuple[int, float, float]]:
    armed, out = [True] * len(VIX_LADDER), []
    for i in range(len(v)):
        if not np.isfinite(v[i]):
            continue
        if v[i] < 20:
            armed = [True] * len(VIX_LADDER)
            continue
        for k, (thr, frac) in enumerate(VIX_LADDER):
            if armed[k] and v[i] >= thr:
                armed[k] = False
                out.append((i, frac, thr))
    return out


def simulate(idx: T.Index, rule: str, pts: list[int], k0: int, n: int, trig: list, curve: bool = False):
    """Contributions at pts[k0..k0+n-1], value at pts[k0+n]. Returns (value, reserve, triggers used, path)."""
    tr, ci = idx.tr, idx.cash
    mode = "B" if rule == "E" else rule
    start, end = pts[k0], pts[k0 + n]
    events = [(pts[k], 0, 0.0) for k in range(k0, k0 + n)]
    if mode != "A":
        events += [(i, 1, f) for i, f, _ in trig if start <= i < end]
    events.sort()
    units = reserve = 0.0
    last, used, path = start, 0, []
    for i, kind, f in events:
        reserve *= ci[i] / ci[last]
        last = i
        if kind == 0:
            if mode == "A":
                units += B / tr[i]
            elif mode == "B":
                reserve += B
            else:
                units += 0.6 * B / tr[i]
                reserve += 0.4 * B
                if mode == "D":
                    total = units * tr[i] + reserve
                    if reserve > 0.4 * total:
                        x = reserve - 0.4 * total
                        units += x / tr[i]
                        reserve -= x
            if curve:
                path.append((str(idx.days[i]), units * tr[i] + reserve))
        else:
            x = reserve * f
            units += x / tr[i]
            reserve -= x
            used += 1
    reserve *= ci[end] / ci[last]
    value = units * tr[end] + reserve
    if curve:
        path.append((str(idx.days[end]), value))
    return value, reserve, used, path


def worst_fall(path):
    peak, peak_day, worst = 0.0, None, (0.0, None, None)
    for d, v in path:
        if v > peak:
            peak, peak_day = v, d
        if 1 - v / peak > worst[0]:
            worst = (1 - v / peak, peak_day, d)
    return worst


def recovery_months(idx: T.Index, trig) -> dict:
    """Months from each trigger buy to the last session on which it was under water (total return); 0 = never."""
    out = {}
    for i, _, thr in trig:
        under = np.flatnonzero(idx.tr[i + 1:] < idx.tr[i])
        months = (under[-1] + 1) / 21 if len(under) else 0.0
        out.setdefault(thr, []).append(months)
    return {thr: (float(np.median(m)), float(np.max(m)), int((m == 0).sum()), len(m))
            for thr, m in ((thr, np.array(v)) for thr, v in out.items())}


def run_index(idx: T.Index, lines: list[str], results: dict, vix: np.ndarray | None, window_years) -> None:
    dd = T.drawdown_252(idx.price)
    trig = ladder_triggers(dd)
    pts = T.month_starts(idx)
    kmin = next(k for k, i in enumerate(pts) if i >= 252)
    vtrig = vix_triggers(vix) if vix is not None else []
    k1990 = next((k for k, i in enumerate(pts) if idx.days[i] >= "1990-01-01"), None)

    lines += [f"## {idx.name}: {idx.days[pts[kmin]]} .. {idx.days[pts[-1]]}", ""]
    by_thr = {}
    for i, f, thr in trig:
        by_thr.setdefault(thr, []).append(str(idx.days[i]))
    for thr in sorted(by_thr, reverse=True):
        yrs = sorted({d[:4] for d in by_thr[thr]})
        lines.append(f"- {int(thr * 100)}% triggers: {len(by_thr[thr])} ({', '.join(yrs)})")
    rec = recovery_months(idx, trig)
    lines += ["", "Months from each trigger buy to its last session under water (total return): median / longest / never under water",
              ""]
    for thr in sorted(rec, reverse=True):
        med, mx, never, n = rec[thr]
        lines.append(f"- {int(thr * 100)}%: {med:.1f} / {mx:.0f} / {never} of {n}")
    if vtrig:
        lines.append(f"- VIX triggers (1990 on): {sum(1 for _, _, t in vtrig if t == 30)} at 30, "
                     f"{sum(1 for _, _, t in vtrig if t == 40)} at 40")
    lines.append("")
    results[idx.name] = {"ladder_triggers": [(str(idx.days[i]), f, thr) for i, f, thr in trig],
                         "vix_triggers": [(str(idx.days[i]), f, thr) for i, f, thr in vtrig],
                         "recovery_months": {str(k): v for k, v in rec.items()}, "single": {}, "windows": {}}

    rules = [r for r in RULES if r != "E" or vix is not None]
    singles = [("Full history", kmin)]
    for years in (30, 20, 10):
        k0 = len(pts) - 1 - 12 * years
        if k0 > kmin:
            singles.append((f"Last {years} years", k0))
    for label, k0 in singles:
        n = len(pts) - 1 - k0
        if k0 < (k1990 or 0) and vix is not None:
            use = [r for r in rules if r != "E"]
        else:
            use = rules
        lines += [f"### {label}: {idx.days[pts[k0]]} .. {idx.days[pts[-1]]}, {n} deposits, ${B * n:,.0f} contributed", "",
                  "| rule | final | vs A | MWR/yr | reserve at end | triggers | worst fall |", "|---|---|---|---|---|---|---|"]
        base = simulate(idx, "A", pts, k0, n, trig)[0]
        for r in use:
            v, res, used, path = simulate(idx, r, pts, k0, n, vtrig if r == "E" else trig, curve=True)
            fall, a, b = worst_fall(path)
            mwr = T.money_weighted(np.array([v]), n, B)[0]
            lines.append(f"| {RULES[r]} | {v:,.0f} | {100 * (v / base - 1):+.1f}% | {mwr:.2f}% | {res:,.0f} ({100 * res / v:.0f}%) "
                         f"| {used} | {100 * fall:.0f}% ({a[:7]}..{b[:7]}) |")
            results[idx.name]["single"][f"{label} {r}"] = {"final": v, "mwr": mwr, "reserve": res, "triggers": used,
                                                           "worst_fall": fall}
        lines.append("")

    for years in window_years:
        n = 12 * years
        starts = list(range(kmin, len(pts) - 1 - n + 1))
        if len(starts) < 12:
            continue
        res = {r: np.array([simulate(idx, r, pts, k0, n, trig)[:2] for k0 in starts]) for r in rules if r != "E"}
        mwr = {r: T.money_weighted(res[r][:, 0], n, B) for r in res}
        lines += [f"### {years}-year windows: {len(starts)} monthly starts, {idx.days[pts[starts[0]]][:7]}..{idx.days[pts[starts[-1]]][:7]}",
                  "", "| rule | MWR median | worst | best | beat A | gap vs A median | worst | best | reserve share at end median | max |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in res:
            gap = mwr[r] - mwr["A"]
            share = 100 * res[r][:, 1] / res[r][:, 0]
            beat = "" if r == "A" else f"{100 * (res[r][:, 0] > res['A'][:, 0]).mean():.0f}%"
            g = "" if r == "A" else f"{np.median(gap):+.2f} | {gap.min():+.2f} | {gap.max():+.2f}"
            lines.append(f"| {RULES[r]} | {np.median(mwr[r]):.2f}% | {mwr[r].min():.2f}% | {mwr[r].max():.2f}% | {beat} | {g if g else ' | | '} "
                         f"| {np.median(share):.0f}% | {share.max():.0f}% |")
            results[idx.name]["windows"][f"{years}y {r}"] = {
                "starts": len(starts), "mwr_median": float(np.median(mwr[r])), "mwr_min": float(mwr[r].min()),
                "mwr_max": float(mwr[r].max()), "beat_A": None if r == "A" else float((res[r][:, 0] > res["A"][:, 0]).mean()),
                "gap_median": None if r == "A" else float(np.median(gap))}
        if vix is not None and k1990 is not None:
            vstarts = [k for k in starts if k >= k1990]
            if len(vstarts) >= 12:
                sub = {r: np.array([simulate(idx, r, pts, k0, n, vtrig if r == "E" else trig)[:2] for k0 in vstarts])
                       for r in ("A", "B", "E")}
                m = {r: T.money_weighted(sub[r][:, 0], n, B) for r in sub}
                lines += ["", f"Windows starting 1990 or later ({len(vstarts)} starts), for the VIX rule:", "",
                          "| rule | MWR median | beat A | gap vs A median | worst | best | reserve share at end median |",
                          "|---|---|---|---|---|---|---|"]
                for r in sub:
                    gap = m[r] - m["A"]
                    share = 100 * sub[r][:, 1] / sub[r][:, 0]
                    beat = "" if r == "A" else f"{100 * (sub[r][:, 0] > sub['A'][:, 0]).mean():.0f}%"
                    g = " | | " if r == "A" else f"{np.median(gap):+.2f} | {gap.min():+.2f} | {gap.max():+.2f}"
                    lines.append(f"| {RULES[r]} | {np.median(m[r]):.2f}% | {beat} | {g} | {np.median(share):.0f}% |")
                    results[idx.name]["windows"][f"{years}y 1990+ {r}"] = {
                        "starts": len(vstarts), "mwr_median": float(np.median(m[r])),
                        "beat_A": None if r == "A" else float((sub[r][:, 0] > sub["A"][:, 0]).mean()),
                        "gap_median": None if r == "A" else float(np.median(gap))}
        lines.append("")


def main() -> None:
    spx = T.load_spx()
    qqq = T.load_qqq(spx)
    vix = T.load_vix()
    lines = ["# Test 1: index dip ladder vs plain DCA", "", "Rules are in the docstring of `tt_ladder.py`.", ""]
    results = {"rules": __doc__}
    run_index(spx, lines, results, T.vix_on(spx, vix), (10, 20, 30))
    run_index(qqq, lines, results, T.vix_on(qqq, vix), (5, 10, 20))
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "ladder.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump(results, open(T.OUT / "ladder.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
