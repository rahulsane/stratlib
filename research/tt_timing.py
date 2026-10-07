"""The Traveling Trader's timing rules. Rules fixed before any results were seen.

(a) VIX spikes. Event = the first VIX close at or above a threshold (25, 30, 40) with no close at or above it in
    the previous 60 sessions. S&P 500 total return over the next 21, 63, 126 and 252 sessions, against every
    session from 1990 on (unconditional). Same for the first close of a QQQ-era event on QQQ (1999 on).
(b) June 22. Buy at the last close on or before June 22, sell at the last close on or before July 22 (his "QQQ
    bought at the June 22 close: 93% positive over the last 15 years, median +4.7%"). QQQ 1999-2025, S&P 500
    1950-2025, and 2011-2025 for both. Compared with the same 30-calendar-day window started on every other
    day of the year: rank of June 22 by share of years positive and by median return.
(c) Midterm years (year mod 4 == 2), S&P 500 price returns 1950-2025, against all other years:
    (i)   September opex close (third Friday) to election-day close;
    (ii)  April 30 close to October 31 close ("May to October is weak");
    (iii) October 31 of the midterm year to October 31 of the next year ("always positive after a midterm").
Price returns for (b) and (c); total returns for (a).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_rules as rr  # noqa: E402
import tt_data as T  # noqa: E402

HORIZONS = (21, 63, 126, 252)


def stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if not len(x):
        return {"n": 0}
    return {"n": int(len(x)), "mean": 100 * float(x.mean()), "median": 100 * float(np.median(x)),
            "positive": 100 * float((x > 0).mean())}


def fmt(s: dict) -> str:
    return f"n={s['n']}: mean {s['mean']:+.1f}%, median {s['median']:+.1f}%, positive {s['positive']:.0f}%" if s["n"] else "n=0"


def vix_events(idx: T.Index, v: np.ndarray, thr: float) -> list[int]:
    out = []
    for i in range(60, len(v)):
        if v[i] >= thr and np.isfinite(v[i - 1]) and v[i - 1] < thr and np.isfinite(v[i - 60:i]).sum() >= 50 \
                and np.nanmax(v[i - 60:i]) < thr:
            out.append(i)
    return out


def vix_section(idx: T.Index, v: np.ndarray, lines: list[str], res: dict) -> None:
    n = len(idx.days)
    first = int(np.flatnonzero(np.isfinite(v))[0])
    lines += [f"### {idx.name}, VIX from {idx.days[first]}", "", "| threshold | events | horizon | after the event | all sessions | difference of means |",
              "|---|---|---|---|---|---|"]
    for thr in (25.0, 30.0, 40.0):
        ev = vix_events(idx, v, thr)
        res[f"{idx.name} vix{int(thr)} events"] = [str(idx.days[i]) for i in ev]
        for h in HORIZONS:
            e = np.array([idx.tr[i + h] / idx.tr[i] - 1 for i in ev if i + h < n])
            u = idx.tr[first + h:] / idx.tr[first:n - h] - 1
            se, su = stats(e), stats(u)
            lines.append(f"| VIX >= {thr:.0f} | {len(ev)} | {h} | {fmt(se)} | {fmt(su)} | "
                         f"{(se['mean'] - su['mean']) if se['n'] else float('nan'):+.1f} pts |")
            res[f"{idx.name} vix{int(thr)} h{h}"] = {"event": se, "all": su}
    lines.append("")
    ev30 = res[f"{idx.name} vix30 events"]
    lines += [f"VIX >= 30 event dates ({idx.name}): " + ", ".join(ev30), ""]


def window_returns(idx: T.Index, years: range, start_md: tuple[int, int], days: int) -> np.ndarray:
    out = []
    for y in years:
        try:
            d0 = date(y, start_md[0], start_md[1])
        except ValueError:
            continue
        d1 = d0 + timedelta(days=days)
        i0, i1 = idx.index_on_or_before(d0.isoformat()), idx.index_on_or_before(d1.isoformat())
        if i0 < 0 or i1 <= i0 or idx.days[i0] < f"{y - 1}-12-01":
            continue
        out.append(idx.price[i1] / idx.price[i0] - 1)
    return np.array(out)


def june22_section(idx: T.Index, years: range, lines: list[str], res: dict, label: str) -> None:
    ret = window_returns(idx, years, (6, 22), 30)
    s = stats(ret)
    starts = [(m, d) for m in range(1, 13) for d in range(1, 32) if (m, d) != (2, 29) and
              (d <= 28 or (m != 2 and (d <= 30 or m in (1, 3, 5, 7, 8, 10, 12))))]
    table = []
    for md in starts:
        r = window_returns(idx, years, md, 30)
        if len(r) >= 0.8 * len(ret):
            table.append((md, 100 * (r > 0).mean(), 100 * np.median(r)))
    pos_rank = 1 + sum(1 for _, p, _ in table if p > s["positive"] + 1e-9)
    med_rank = 1 + sum(1 for _, _, m in table if m > s["median"] + 1e-9)
    all_pos = np.mean([p for _, p, _ in table])
    all_med = np.median([m for _, _, m in table])
    best = sorted(table, key=lambda x: -x[1])[:3]
    lines.append(f"| {label} | {s['n']} | {s['positive']:.0f}% | {s['median']:+.2f}% | {s['mean']:+.2f}% | {pos_rank} of {len(table)} "
                 f"| {med_rank} of {len(table)} | {all_pos:.0f}% / {all_med:+.2f}% | "
                 + ", ".join(f"{m:02d}-{d:02d} ({p:.0f}%)" for (m, d), p, _ in best) + " |")
    res[label] = {"june22": s, "rank_positive": pos_rank, "rank_median": med_rank, "starts": len(table),
                  "all_starts_positive_mean": float(all_pos), "all_starts_median": float(all_med),
                  "yearly": [float(x) for x in ret]}


def midterm_section(idx: T.Index, lines: list[str], res: dict) -> None:
    years = range(1950, 2026)
    defs = {
        "Sept opex close to election-day close": lambda y: (T.third_friday(y, 9).isoformat(), T.election_day(y).isoformat()),
        "April 30 close to October 31 close": lambda y: (f"{y}-04-30", f"{y}-10-31"),
        "October 31 to October 31 of the next year": lambda y: (f"{y}-10-31", f"{y + 1}-10-31"),
    }
    lines += ["| window | midterm years | other years | midterm values |", "|---|---|---|---|"]
    for name, fn in defs.items():
        mid, oth, vals = [], [], []
        for y in years:
            a, b = fn(y)
            if b > str(idx.days[-1]):
                continue
            i0, i1 = idx.index_on_or_before(a), idx.index_on_or_before(b)
            if i0 < 0 or i1 <= i0:
                continue
            r = idx.price[i1] / idx.price[i0] - 1
            (mid if y % 4 == 2 else oth).append(r)
            if y % 4 == 2:
                vals.append((y, r))
        sm, so = stats(np.array(mid)), stats(np.array(oth))
        lines.append(f"| {name} | {fmt(sm)} | {fmt(so)} | " + ", ".join(f"{y} {100 * r:+.0f}%" for y, r in vals) + " |")
        res[name] = {"midterm": sm, "other": so, "midterm_values": vals}
    lines.append("")


def rules_section() -> list[str]:
    return rr.section(
        ["Three market-timing claims from the Traveling Trader's videos, each tested as an event study against "
         "ordinary days. The rules were fixed before any results were seen."],
        ["(a) Buy VIX spikes. An event is the first VIX close at or above a threshold (25, 30 or 40) with no close at "
         "or above it in the previous 60 sessions. The tables give the S&P 500's total return over the next 21, 63, "
         "126 and 252 sessions after each event, against the same horizons from every session since 1990. The QQQ "
         "tables do the same from 1999."],
        ["(b) June 22 to July 22. Buy at the last close on or before June 22 and sell at the last close on or before "
         "July 22 (his claim: QQQ bought at the June 22 close was positive in 93% of the last 15 years, median "
         "+4.7%). Tested on QQQ 1999–2025, the S&P 500 1950–2025, and 2011–2025 for both. The same 30-day window "
         "started on every other day of the year ranks June 22 by the share of years positive and by the median "
         "return."],
        ["(c) Midterm election years, every fourth year from 1950 to 2022, against all other years, S&P 500 "
         "1950–2025:", "",
         "- from the September options-expiry close (third Friday) to the election-day close;",
         "- from the April 30 close to the October 31 close (\"May to October is weak\");",
         "- from October 31 of the midterm year to October 31 of the next (\"always positive after a midterm\")."],
        ["Data: S&P 500 daily closes from 1950, with Shiller's monthly dividends less a 0.09% a year fund fee for "
         "total returns; QQQ with dividends from 1999; VIX closes from 1990. (a) uses total returns, (b) and (c) price "
         "returns. There are no costs or taxes. VIX events number in the tens and there are only 19 midterm years, so "
         "the uncertainty is wide."],
    )


def main() -> None:
    spx = T.load_spx()
    qqq = T.load_qqq(spx)
    vix = T.load_vix()
    lines = ["# The Traveling Trader's timing rules", "", *rules_section(), "## (a) VIX spikes, total returns", ""]
    res = {}
    vix_section(spx, T.vix_on(spx, vix), lines, res)
    vix_section(qqq, T.vix_on(qqq, vix), lines, res)
    lines += ["## (b) June 22 to July 22, price returns", "",
              "| series | years | positive | median | mean | rank by share positive | rank by median | all start days: mean positive / median | best three start days |",
              "|---|---|---|---|---|---|---|---|---|"]
    june22_section(qqq, range(1999, 2026), lines, res, "QQQ 1999-2025")
    june22_section(qqq, range(2011, 2026), lines, res, "QQQ 2011-2025")
    june22_section(spx, range(1950, 2026), lines, res, "S&P 500 1950-2025")
    june22_section(spx, range(2011, 2026), lines, res, "S&P 500 2011-2025")
    lines += ["", "QQQ June 22 windows by year: " + ", ".join(f"{y} {100 * r:+.1f}%" for y, r in zip(range(1999, 2026), res["QQQ 1999-2025"]["yearly"])), "",
              "## (c) Midterm years, S&P 500 price returns 1950-2025", ""]
    midterm_section(spx, lines, res)
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "timing.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump({"rules": __doc__, **res}, open(T.OUT / "timing.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
