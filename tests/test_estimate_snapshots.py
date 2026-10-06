"""The monthly estimates snapshot: what it saves, when it is due, and that a failure never stops a backfill.
Every response here is synthetic."""

import json
from datetime import date
from unittest.mock import Mock

import pytest

from stratlib import estimate_snapshots as ES
from stratlib.fmp import FMPAuthError, FMPError

EUSA = """iShares MSCI USA Equal Weighted ETF
Fund Holdings as of,"Sep 30, 2026"
Inception Date,"May 05, 2010"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price
"AAA","ALPHA INC","Information Technology","Equity","1,000.00","0.50","1,000.00","10.00","100.00"
"BRK B","BERKSHIRE HATHAWAY CLASS B","Financials","Equity","1,000.00","0.50","1,000.00","2.00","500.00"
"USD","USD CASH","Cash and/or Derivatives","Cash","10.00","0.01","10.00","10.00","1.00"
"""
GARP = EUSA.replace("MSCI USA Equal Weighted ETF", "MSCI USA Quality GARP ETF").replace('"BRK B"', '"CCC"')
MSCI = {"constituents": [{"security_name": "ALPHA", "security_weight": "60.5"}, {"security_name": "CEE", "security_weight": "39.5"}]}


def fetch_ok(url):
    if "239693" in url:
        return EUSA
    if "312212" in url:
        return GARP
    return json.dumps(MSCI)


def client_for(*, fail=()):
    def get(path, params=None):
        if path == "shares-float-all":
            return [{"symbol": "AAA", "floatShares": 90, "outstandingShares": 100, "freeFloat": 90.0},
                    {"symbol": "ZZZ", "floatShares": 1, "outstandingShares": 1, "freeFloat": 100.0}] if params["page"] == 0 else []
        if params["symbol"] in fail:
            raise FMPError("no data")
        return [{"symbol": params["symbol"], "date": "2026-12-31", "epsAvg": 2.5, "epsLow": 2.0, "epsHigh": 3.0,
                 "numAnalystsEps": 12, "revenueAvg": 1e9}]
    client = Mock()
    client.get.side_effect = get
    return client


def test_a_snapshot_stands_for_the_month_that_has_just_ended():
    assert ES.label_for(date(2026, 10, 2)) == "2026-09"
    assert ES.label_for(date(2026, 1, 31)) == "2025-12"


def test_ishares_files_are_read_as_equity_rows_with_fmp_tickers():
    parsed = ES.ishares_holdings(EUSA)
    assert parsed["as_of"] == "2026-09-30"
    assert parsed["rows"] == [["AAA", "ALPHA INC", "Information Technology", 0.5, 100.0],
                              ["BRK-B", "BERKSHIRE HATHAWAY CLASS B", "Financials", 0.5, 500.0]]


def test_the_snapshot_saves_estimates_float_and_the_free_files_for_every_member(store):
    store.save_document(ES.SP500_KEY, {"current": [{"symbol": "DDD"}, {"symbol": "AAA"}]})
    client = client_for(fail=("DDD",))
    summary = ES.take(store, client, date(2026, 10, 2), fetch=fetch_ok)
    doc = store.document(ES.KEY + "2026-09")
    assert summary["members"] == 4 and summary["month"] == "2026-09"
    assert sorted(doc["estimates"]) == ["AAA", "BRK-B", "CCC"]                     # DDD failed and is recorded
    assert doc["errors"]["estimates"] == {"DDD": "no data"}
    assert doc["estimates"]["AAA"] == [["2026-12-31", 2.0, 2.5, 3.0, 12, None, 1e9, None, None]]
    assert doc["float"] == {"AAA": [90, 100, 90.0]}                                # only members are kept
    assert doc["files"]["garp"]["rows"][1][0] == "CCC" and doc["files"]["msci_756664"][0] == ["ALPHA", 60.5]
    assert doc["taken_on"] == "2026-10-02"
    assert not ES.due(store, date(2026, 10, 20)) and ES.due(store, date(2026, 11, 1))


def test_a_failed_download_is_recorded_and_the_rest_is_kept(store):
    def fetch(url):
        if "312212" in url:
            raise ConnectionError("down")
        return fetch_ok(url)
    ES.take(store, client_for(), date(2026, 10, 2), fetch=fetch)
    doc = store.document(ES.KEY + "2026-09")
    assert "garp" not in doc["files"] and "down" in doc["errors"]["garp"]
    assert sorted(doc["estimates"]) == ["AAA", "BRK-B"]


def test_a_backfill_takes_it_once_a_month_and_never_fails_because_of_it(store):
    assert ES.take_if_due(store, client_for(), date(2026, 10, 2), fetch=fetch_ok)["estimates"] == 3
    assert ES.take_if_due(store, client_for(), date(2026, 10, 3), fetch=fetch_ok) is None       # already saved
    def broken(url):
        raise ConnectionError("offline")
    failed = ES.take_if_due(store, client_for(), date(2026, 11, 2), fetch=broken)               # no members at all
    assert failed["month"] == "2026-10" and "failed" in failed and ES.due(store, date(2026, 11, 2))
    client = Mock()
    client.get.side_effect = FMPAuthError("bad key")
    with pytest.raises(FMPAuthError):
        ES.take_if_due(store, client, date(2026, 11, 2), fetch=fetch_ok)
