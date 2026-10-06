"""What the saved research runs found for each strategy, read from research/output.

Nothing here runs a backtest or makes an API call. A strategy's live rules are the ones these runs
tested, so the numbers describe that rule set's history under the research ground rules (2016 on,
stocks and ETFs, risk-based sizing, costs), not a forecast.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .reports import RESEARCH_ROOT, ReportError, load_result

# (label, run folder under research/output) for the runs that used the live rule set.
RUNS: dict[str, tuple[tuple[str, str], ...]] = {
    "qullamaggie": (("10-day trail, as specified", "qullamaggie_sma10"),
                    ("20-day trail", "qullamaggie_sma20")),
    "minervini": (("Exit (b): a third on day 3, 10-day trail", "minervini_vcp_exit_b"),
                  ("Exit (a): half at +20%, then the 50-day average", "minervini_vcp_exit_a")),
    "episodic_pivot": (("Exit (a): day-3 third, 10-day trail", "ep_fixed_a_alone_0.5"),
                       ("Exit (b): 20-session time exit", "ep_fixed_b_alone_0.5"),
                       ("Exit (c): 10-day trail after session 20", "ep_fixed_c_alone_0.5"),
                       ("Exit (a) with all idle capital held in SPY, 1.0% risk", "ep_fixed_a_overlay_1.0")),
    "ema_pullback": (("Exit at the first close below the 21-day EMA", "tt_ema_first"),
                     ("Exit at the second close below the 21-day EMA", "tt_ema_second")),
    "tt_checklist": (("Top ten by 63-session return", "tt_checklist_qual_top10"),
                     ("Top ten, SPY above its 200-day average", "tt_checklist_qual_top10_market200"),
                     ("Top ten, stock above its 200-day average", "tt_checklist_qual_top10_stock200"),
                     ("Top ten, stock and SPY above their 200-day averages", "tt_checklist_qual_top10_both200")),
}
PERIODS = (("combined", "2016 to present"), ("out_of_sample", "2022 to present"))
# MSCI GARP's study compared versions of one index rather than engine runs (research/mscigarp_backtest.py):
# (label, series in research/output/mscigarp/results.json), and (period there, period here).
INDEX_STUDY = {"msci_garp": ("mscigarp", (("ETF version: MSCI's index less 0.20% a year", "Version 2: ETF (0.20%/yr)"),
                                          ("Self-managed copy of MSCI's index", "Version 1: self-managed"),
                                          ("These rules (the rebuild), self-managed", "Rebuilt index, self-managed")),
                             (("Main: Dec 2015 - now", "Dec 2015 to present"),
                              ("Out-of-sample 2022-now", "2022 to present")))}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def index_study(strategy_id: str, root: Path = RESEARCH_ROOT) -> dict | None:
    """An index strategy's saved study (results.json), if there is one."""
    if strategy_id not in INDEX_STUDY:
        return None
    try:
        return json.loads((root / "output" / INDEX_STUDY[strategy_id][0] / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def evidence_rows(strategy_id: str, root: Path = RESEARCH_ROOT) -> list[dict]:
    """One row per saved run and period: trades, CAGR against SPY, drawdown and expectancy."""
    rows = []
    if strategy_id in INDEX_STUDY:
        study = index_study(strategy_id, root) or {}
        _, series, periods = INDEX_STUDY[strategy_id]
        for label, name in series:
            for period, title in periods:
                found = (study.get("periods") or {}).get(period) or {}
                metrics, spy = found.get(name) or {}, (found.get("SPY") or {}).get("cagr")
                cagr = _number(metrics.get("cagr"))
                if cagr is None:
                    continue
                spy = _number(spy)
                rows.append({"Run": label, "Period": title, "CAGR, %": cagr, "SPY CAGR, %": spy,
                             "Gap vs SPY, pp": cagr - spy if spy is not None else None,
                             "Max drawdown, %": _number(metrics.get("maxdd"))})
        return rows
    for label, folder in RUNS.get(strategy_id, ()):
        try:
            result = load_result(root / "output" / folder)
        except ReportError:
            continue
        for period, title in PERIODS:
            metrics = result["results"].get(period)
            if not metrics:
                continue
            cagr, spy = _number(metrics.get("cagr")), _number((metrics.get("spy") or {}).get("cagr"))
            rows.append({"Run": label, "Period": title, "Trades": _number(metrics.get("trades")), "CAGR, %": cagr,
                         "SPY CAGR, %": spy, "Gap vs SPY, pp": cagr - spy if cagr is not None and spy is not None else None,
                         "Max drawdown, %": _number(metrics.get("max_drawdown")),
                         "Expectancy, R": _number(metrics.get("expectancy_r"))})
    return rows


def verdict(strategy_id: str, root: Path = RESEARCH_ROOT) -> str | None:
    """One honest sentence from the saved runs: how many beat SPY over the combined period."""
    if strategy_id in INDEX_STUDY:
        return index_verdict(strategy_id, root)
    rows = [r for r in evidence_rows(strategy_id, root) if r["Period"] == PERIODS[0][1] and r["Gap vs SPY, pp"] is not None]
    if not rows:
        return None
    beat = [r for r in rows if r["Gap vs SPY, pp"] > 0]
    best = max(rows, key=lambda r: r["Gap vs SPY, pp"])
    gap = best["Gap vs SPY, pp"]
    position = (f"the best finished {gap:.1f} points a year ahead of it" if gap > 0
                else f"the closest finished {-gap:.1f} points a year behind it")
    return f"{len(beat)} of {len(rows)} saved runs beat SPY with dividends from 2016; {position} ({best['Run']})."


def index_verdict(strategy_id: str, root: Path = RESEARCH_ROOT) -> str | None:
    """MSCI GARP: MSCI's own index against SPY, and how far the rebuild these rules follow trails it."""
    rows = {r["Run"]: r for r in evidence_rows(strategy_id, root) if r["Period"] == "Dec 2015 to present"}
    _, series, _ = INDEX_STUDY[strategy_id]
    etf, rebuild = rows.get(series[0][0]), rows.get(series[2][0])
    spy = _number((((index_study(strategy_id, root) or {}).get("periods") or {}).get("Main: Dec 2015 - now", {})
                   .get("SPY") or {}).get("maxdd"))
    if not etf or not rebuild or etf["SPY CAGR, %"] is None or spy is None:
        return None
    return (f"From December 2015 the research's ETF version (MSCI's own index less the fund's costs) returned "
            f"{etf['CAGR, %']:.1f}% a year against SPY's {etf['SPY CAGR, %']:.1f}%, with a maximum drawdown of "
            f"{etf['Max drawdown, %']:.0f}% against SPY's {spy:.0f}%. The rebuild these rules follow, self-managed, returned "
            f"{rebuild['CAGR, %']:.1f}%: without analysts' forecasts it trails MSCI's index, most of all in 2023-2025.")
