"""Monthly snapshots of analysts' consensus estimates and of what the MSCI GARP index really holds, kept so that a
point-in-time record builds up from now on (FMP serves only today's estimates, and the history is licensed data).

Each snapshot is labelled with the month that has just ended, stands for that month-end and records the day it was
taken. A full backfill takes one when the month's snapshot is missing (``stratlib backfill``), and ``stratlib
estimates-snapshot`` takes one on demand. It holds, for the MSCI USA and S&P 500 members:
- FMP's annual consensus estimates (EPS and revenue, low, mean and high, number of analysts) for each fiscal year it
  lists: about 600 calls;
- FMP's free float for every member (``shares-float-all``, about ten calls);
- the holdings files iShares publishes for its MSCI USA Equal Weighted ETF (EUSA, MSCI USA's members with GICS
  sectors) and its MSCI USA Quality GARP ETF (GARP, the index's holdings), and MSCI's own constituent lists with
  weights for both indexes, all free downloads that keep no history of their own.

Saved as ``research:estimates:<month>``; research/mscigarp_full.py shows how a review is rebuilt from such inputs.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone

import requests

from .fmp import FMPAuthError, FMPError

log = logging.getLogger(__name__)

KEY = "research:estimates:"
SP500_KEY = "research:garp:sp500"          # the S&P 500 list the MSCI GARP screen refreshes
ISHARES = {"eusa": "https://www.ishares.com/us/products/239693/ishares-msci-usa-etf/latest-holdings.csv",
           "garp": "https://www.ishares.com/us/products/312212/ishares-msci-usa-quality-garp-etf/latest-holdings.csv"}
MSCI_TOOL = ("https://www.msci.com/web/msci/index-tools/constituents?p_p_id=indexconstituents_WAR_indexconstituents_"
             "INSTANCE_9uV8ur27dV1U&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view&p_p_resource_id={code}"
             "&p_p_cacheability=cacheLevelPage")
MSCI_INDEXES = {"756664": "MSCI USA Quality GARP Select", "984000": "MSCI USA"}
ESTIMATE_FIELDS = ("date", "epsLow", "epsAvg", "epsHigh", "numAnalystsEps", "revenueLow", "revenueAvg", "revenueHigh",
                   "numAnalystsRevenue")
FLOAT_PAGES = 40


def label_for(today: date) -> str:
    """The month a snapshot taken today stands for: the one that has just ended."""
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def due(store, today: date) -> bool:
    return store.document(KEY + label_for(today)) is None


def ishares_holdings(text: str) -> dict:
    """An iShares holdings CSV: its date and the equity rows (ticker, name, GICS sector, weight %, price)."""
    lines = text.splitlines()
    as_of = next((l.split(",", 1)[1].strip('"') for l in lines if l.startswith("Fund Holdings as of")), None)
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if r.get("Asset Class") != "Equity":
            continue
        price = (r.get("Price") or "").replace(",", "")
        rows.append([r["Ticker"].strip().replace(" ", "-").replace(".", "-"), r["Name"], r["Sector"],
                     float(r["Weight (%)"].replace(",", "")), float(price) if price.replace(".", "").isdigit() else None])
    as_of = datetime.strptime(as_of, "%b %d, %Y").date().isoformat() if as_of else None
    return {"as_of": as_of, "rows": rows}


def download(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    return resp.text


def free_downloads(fetch=download) -> tuple[dict, dict]:
    """(the iShares and MSCI files, errors by source)."""
    out, errors = {}, {}
    for name, url in ISHARES.items():
        try:
            out[name] = ishares_holdings(fetch(url))
        except Exception as exc:  # a changed page must not stop the snapshot
            errors[name] = f"{type(exc).__name__}: {exc}"
    for code in MSCI_INDEXES:
        try:
            rows = json.loads(fetch(MSCI_TOOL.format(code=code)))["constituents"]
            out[f"msci_{code}"] = [[r["security_name"], float(r["security_weight"])] for r in rows]
        except Exception as exc:
            errors[f"msci_{code}"] = f"{type(exc).__name__}: {exc}"
    return out, errors


def members(store, files: dict) -> list[str]:
    """The MSCI USA members (EUSA's holdings), the S&P 500 and the GARP fund's holdings."""
    found = {r[0] for name in ("eusa", "garp") for r in (files.get(name) or {}).get("rows", [])}
    found |= {x["symbol"] for x in (store.document(SP500_KEY) or {}).get("current", []) if x.get("symbol")}
    return sorted(s for s in found if s and s.replace("-", "").isalnum())


def take(store, client, today: date | None = None, *, fetch=download, progress=None) -> dict:
    """Take and save the month's snapshot. Returns a summary."""
    today = today or date.today()
    files, errors = free_downloads(fetch)
    symbols = members(store, files)
    if not symbols:
        raise FMPError("No members to snapshot: the iShares files failed and no S&P 500 list is cached.")
    estimates, floats = {}, {}
    wanted = set(symbols)
    try:
        for page in range(FLOAT_PAGES):
            rows = client.get("shares-float-all", {"page": page, "limit": 5000}) or []
            for r in rows:
                if r.get("symbol") in wanted:
                    floats[r["symbol"]] = [r.get("floatShares"), r.get("outstandingShares"), r.get("freeFloat")]
            if not rows or wanted <= set(floats):
                break
    except FMPAuthError:
        raise
    except FMPError as exc:
        errors["float"] = str(exc)
    for k, s in enumerate(symbols):
        try:
            rows = client.get("analyst-estimates", {"symbol": s, "period": "annual", "limit": 10}) or []
            estimates[s] = [[r.get(f) for f in ESTIMATE_FIELDS] for r in rows if isinstance(r, dict)]
        except FMPAuthError:
            raise
        except FMPError as exc:
            errors.setdefault("estimates", {})[s] = str(exc)
        if progress and (k + 1) % 100 == 0:
            progress("Saving consensus estimates", k + 1, len(symbols))
    doc = {"month": label_for(today), "taken_on": today.isoformat(), "taken_at": datetime.now(timezone.utc).isoformat(),
           "estimate_fields": list(ESTIMATE_FIELDS), "float_fields": ["floatShares", "outstandingShares", "freeFloat"],
           "estimates": estimates, "float": floats, "files": files, "errors": errors}
    store.save_document(KEY + doc["month"], doc)
    summary = {"month": doc["month"], "members": len(symbols), "estimates": len(estimates), "float": len(floats),
               "files": sorted(files), "errors": sorted(errors)}
    log.info("Estimates snapshot %s: %s", doc["month"], summary)
    return summary


def take_if_due(store, client, today: date | None = None, **kwargs) -> dict | None:
    """The month's snapshot, unless it is already saved. Failures are logged, never raised: a backfill must finish."""
    today = today or date.today()
    if not due(store, today):
        return None
    try:
        return take(store, client, today, **kwargs)
    except FMPAuthError:
        raise
    except Exception as exc:
        log.warning("The monthly estimates snapshot failed and will be retried at the next backfill: %s", exc)
        return {"month": label_for(today), "failed": f"{type(exc).__name__}: {exc}"}
