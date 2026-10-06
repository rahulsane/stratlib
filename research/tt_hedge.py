"""Test 5: the cost of a rolling index put hedge. Rules fixed before any results were seen (pricing
assumptions widened after a first run showed flat-VIX pricing makes out-of-the-money puts unrealistically cheap).

S&P 500 total return 1990-2026 with the VIX. On each quarterly option expiry (the last session on or before
the third Friday of March, June, September and December) the portfolio is rebalanced: puts on the index
expiring at the next quarterly expiry are bought, and the rest holds the index. Puts settle at expiry for
max(strike - index, 0) and are marked to market daily.

Sizing, two readings of "I hedge 5 to 10% of my portfolio ... in OTM puts that expire within 2 or 3 months":
    budget    premium spend of X% of portfolio value per quarter (2.5%, 5%);
    notional  puts covering N% of portfolio value (50%, 100%), the classic protective put.
Strike: 90% of the index (10% out of the money); variants 95% and 100%.
Pricing: Black-Scholes with the 3-month T-bill rate and the trailing dividend yield, implied volatility =
    flat     the VIX close (a lower bound: no skew, no term premium);
    central  VIX + 1 point of term premium + 0.5 point per 1% out of the money (about +5 points at 90%);
    high     VIX x 1.2 + 1 point + 0.7 point per 1% out of the money.
Seasonal variant: hedge only the September-December quarter.
Benchmarks: the index alone; 95% index + 5% cash rebalanced at the same dates.
Metrics: CAGR, maximum daily drawdown, worst calendar year, portfolio change over the 2000-02, 2007-09, 2020
and 2022 index declines, and the annual cost (CAGR minus the index's).
The option prices are model prices, so the results are indicative, not a record of tradable quotes.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt_data as T  # noqa: E402

START = "1990-03-16"
EPISODES = {"2000-02": ("2000-03-24", "2002-10-09"), "2007-09": ("2007-10-09", "2009-03-09"),
            "2020": ("2020-02-19", "2020-03-23"), "2022": ("2022-01-03", "2022-10-12")}
PRICING = {"flat": (1.0, 0.0, 0.0), "central": (1.0, 1.0, 0.5), "high": (1.2, 1.0, 0.7)}


def spec(budget=None, notional=None, strike=0.90, pricing="central", seasonal=False):
    return {"budget": budget, "notional": notional, "strike": strike, "pricing": pricing, "seasonal": seasonal}


VARIANTS = [
    ("index only", None),
    ("95% index + 5% cash", {"cash": 5.0}),
    ("budget 5%, strike 90%, central pricing", spec(budget=5.0)),
    ("budget 5%, strike 90%, flat VIX pricing", spec(budget=5.0, pricing="flat")),
    ("budget 5%, strike 90%, high pricing", spec(budget=5.0, pricing="high")),
    ("budget 2.5%, strike 90%, central", spec(budget=2.5)),
    ("budget 5%, strike 95%, central", spec(budget=5.0, strike=0.95)),
    ("budget 5%, at the money, central", spec(budget=5.0, strike=1.00)),
    ("notional 100%, strike 90%, central", spec(notional=100.0)),
    ("notional 100%, strike 90%, flat VIX", spec(notional=100.0, pricing="flat")),
    ("notional 50%, strike 90%, central", spec(notional=50.0)),
    ("budget 5%, strike 90%, central, Sept-Dec only", spec(budget=5.0, seasonal=True)),
]


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bs_put(s: float, k: float, t: float, iv: float, r: float, q: float) -> float:
    if t <= 0:
        return max(k - s, 0.0)
    iv = max(iv, 1e-4)
    d1 = (math.log(s / k) + (r - q + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


def implied_vol(vix: float, s: float, k: float, pricing: str) -> float:
    mult, term, slope = PRICING[pricing]
    otm = max(0.0, 100 * math.log(s / k))
    v = vix if np.isfinite(vix) else 20.0
    return (v * mult + term + slope * otm) / 100


def roll_rows(idx: T.Index, rows: np.ndarray) -> list[int]:
    out = []
    for y in range(1990, 2027):
        for m in (3, 6, 9, 12):
            i = idx.index_on_or_before(T.third_friday(y, m).isoformat())
            if rows[0] <= i <= rows[-1] and (not out or i > out[-1]):
                out.append(i)
    return out


def simulate(idx: T.Index, v: np.ndarray, rows: np.ndarray, rolls: list[int], sp: dict | None) -> np.ndarray:
    """Portfolio value per session in `rows`, starting at 1.0."""
    tr, ci, price, rate, dy, days = idx.tr, idx.cash, idx.price, idx.rate, idx.dy, idx.days
    n = len(rows)
    value = np.ones(n)
    i0 = rows[0]
    units, cash, contracts, strike, expiry = 1.0 / tr[i0], 0.0, 0.0, 0.0, None
    roll_set = set(rolls)
    if sp is not None and "cash" in sp:
        units, cash = (1 - sp["cash"] / 100) / tr[i0], sp["cash"] / 100

    def put_value(i: int) -> float:
        if contracts <= 0:
            return 0.0
        t_left = (date.fromisoformat(days[expiry]) - date.fromisoformat(days[i])).days / 365
        iv = implied_vol(v[i], price[i], strike, sp["pricing"])
        return contracts * bs_put(price[i], strike, t_left, iv, rate[i] / 100, dy[i])

    for n_i, i in enumerate(rows):
        if n_i:
            cash *= ci[i] / ci[rows[n_i - 1]]
        if i in roll_set:
            if contracts > 0 and i == expiry:
                cash += contracts * max(strike - price[i], 0.0)
                contracts = 0.0
            total = units * tr[i] + cash + put_value(i)
            if sp is None:
                pass
            elif "cash" in sp:
                cash = sp["cash"] / 100 * total
                units = (total - cash) / tr[i]
            else:
                k = rolls.index(i)
                nxt = rolls[k + 1] if k + 1 < len(rolls) else None
                hedge = nxt is not None and (not sp["seasonal"] or days[i][5:7] == "09")
                if hedge:
                    strike = sp["strike"] * price[i]
                    expiry = nxt
                    t_left = (date.fromisoformat(days[nxt]) - date.fromisoformat(days[i])).days / 365
                    iv = implied_vol(v[i], price[i], strike, sp["pricing"])
                    unit_price = bs_put(price[i], strike, t_left, iv, rate[i] / 100, dy[i])
                    if sp["budget"] is not None:
                        spend = sp["budget"] / 100 * total
                        contracts = spend / unit_price
                    else:
                        contracts = sp["notional"] / 100 * total / price[i]
                        spend = contracts * unit_price
                    units = (total - spend) / tr[i]
                    cash = 0.0
                else:
                    units, cash = total / tr[i], 0.0
        value[n_i] = units * tr[i] + cash + put_value(i)
    return value


def episode_change(idx: T.Index, rows: np.ndarray, value: np.ndarray, a: str, b: str) -> float:
    ia, ib = idx.index_on_or_before(a), idx.index_on_or_before(b)
    ka, kb = int(np.searchsorted(rows, ia)), int(np.searchsorted(rows, ib))
    return 100 * (value[kb] / value[ka] - 1)


def main() -> None:
    spx = T.load_spx()
    v = T.vix_on(spx, T.load_vix())
    rows = spx.window(START)
    rolls = roll_rows(spx, rows)
    days = spx.days[rows]
    # what a 3-month 10%-OTM put costs under each pricing, as % of the index, on the roll dates
    costs = {}
    for name in PRICING:
        c = []
        for i in rolls[:-1]:
            t_left = 91 / 365
            iv = implied_vol(v[i], spx.price[i], 0.9 * spx.price[i], name)
            c.append(100 * bs_put(spx.price[i], 0.9 * spx.price[i], t_left, iv, spx.rate[i] / 100, spx.dy[i]) / spx.price[i])
        costs[name] = (float(np.median(c)), float(np.min(c)), float(np.max(c)))
    lines = ["# Test 5: rolling put hedge on the S&P 500, 1990-2026", "", "Rules: docstring of `tt_hedge.py`. "
             "Option prices are model prices (Black-Scholes on the VIX with an assumed skew), so treat the costs as indicative.", "",
             f"Period {days[0]} .. {days[-1]}, {len(rolls)} quarterly rolls.", "",
             "Model cost of a 3-month put struck 10% below the index, % of the index, over the roll dates (median, min, max): "
             + "; ".join(f"{k} {a:.2f}% ({b:.2f}-{c:.2f})" for k, (a, b, c) in costs.items()), "",
             "| portfolio | CAGR | vs index | max drawdown | worst year | 2000-02 | 2007-09 | 2020 | 2022 |",
             "|---|---|---|---|---|---|---|---|---|"]
    res = {"rules": __doc__, "put_costs": costs}
    yearly = {}
    for label, sp in VARIANTS:
        value = simulate(spx, v, rows, rolls, sp)
        yr = T.yearly_returns(value, days)
        yearly[label] = yr
        c = T.cagr(1.0, float(value[-1]), str(days[0]), str(days[-1]))
        if label == "index only":
            base = c
        eps = {k: episode_change(spx, rows, value, a, b) for k, (a, b) in EPISODES.items()}
        worst = min(yr.values())
        lines.append(f"| {label} | {c:.2f}% | {c - base:+.2f} | {T.max_drawdown(value):.1f}% | {worst:+.1f}% | "
                     + " | ".join(f"{eps[k]:+.1f}%" for k in EPISODES) + " |")
        res[label] = {"cagr": c, "max_drawdown": T.max_drawdown(value), "worst_year": worst, "episodes": eps, "yearly": yr}
    cols = ["index only", "95% index + 5% cash", "budget 5%, strike 90%, central pricing", "notional 100%, strike 90%, central",
            "budget 5%, strike 90%, central, Sept-Dec only"]
    lines += ["", "## Yearly returns", "", "| year | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for y in yearly["index only"]:
        lines.append(f"| {y} | " + " | ".join(f"{yearly[c][y]:+.1f}%" for c in cols) + " |")
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "hedge.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump(res, open(T.OUT / "hedge.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
