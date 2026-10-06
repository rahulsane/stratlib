import pytest

from stratlib.universe import exclusion_reason, parse_listings, sample_symbols

from conftest import load_fixture

COMMON = sorted([
    "AAPL", "NVDA", "MSFT", "CMCSA", "FWONK", "BATRA", "RYAAY", "PTRN", "YICC",
    "OPENL",  # Opendoor warrants; Nasdaq's "L" suffix is also used by GOOGL
    "JPM", "BRK-B", "BF-B", "BLK",   # BRK-A is the less traded class of the same company
    "MGRB",  # a note under the parent's exact name; AMG itself is not in this fixture
    "ACCS", "ACU", "AEON", "AGIG", "AIB", "AIM",
])


def screener_rows():
    rows = []
    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        rows.extend(load_fixture(f"company-screener_{exchange}.json"))
    return rows


def test_recorded_universe_keeps_common_stock_only():
    stocks = parse_listings(screener_rows())

    assert sorted(s.symbol for s in stocks if s.is_common) == COMMON
    assert {s.symbol: s.excluded_reason for s in stocks if not s.is_common} == {
        "YICCU": "unit",
        "YICCW": "warrant",
        "APURR": "right",
        "MCHPP": "preferred",
        "WTFCN": "preferred",
        "ZXZZT": "other",
        "TMUSI": "debt",
        "OXLCG": "debt",
        "FPCRX": "fund",
        "METCI": "debt",
        "FITB-PM": "preferred",
        "PEW-WT": "warrant",
        "AAC-UN": "unit",
        "DTK": "debt",
        "SOJC": "debt",
        "SOJE": "debt",
        "GJS": "structured",
        "PYT": "structured",
        "BXSL": "fund",
        "DUKB": "fixed-income",
        "DUKU": "unit",
        "MHLA": "debt",
        "SCCD": "debt",
        "SCCE": "fixed-income",
        "SCCF": "fixed-income",
        "SCCG": "fixed-income",
        "BRK-A": "secondary listing",
    }


def test_parse_listings_maps_fields_and_drops_duplicates():
    rows = screener_rows()
    stocks = parse_listings(rows + rows)
    assert len(stocks) == len(rows)

    aapl = next(s for s in stocks if s.symbol == "AAPL")
    assert aapl.exchange == "NASDAQ"
    assert aapl.exchange_full == "NASDAQ Global Select"
    assert aapl.sector == "Technology"
    assert aapl.industry == "Consumer Electronics"
    assert aapl.price > 0 and aapl.avg_volume > 0


@pytest.mark.parametrize(
    "symbol, exchange, name, expected",
    [
        ("PFBC", "NASDAQ", "Preferred Bank", None),
        ("URI", "NYSE", "United Rentals, Inc.", None),
        ("U", "NYSE", "Unity Software Inc.", None),
        ("GOOGL", "NASDAQ", "Alphabet Inc.", None),
        ("FWONK", "NASDAQ", "Liberty Media Corporation", None),
        ("RYAAY", "NASDAQ", "Ryanair Holdings plc", None),
        ("PSA-PH", "NYSE", "Public Storage", "preferred"),
        ("ABCDW", "NASDAQ", "Abcd Holdings Inc.", "warrant"),
        ("ABCDU", "NASDAQ", "Abcd Acquisition Corp.", "unit"),
        ("ABCDR", "NASDAQ", "Abcd Acquisition Corp.", "right"),
        ("ABCDP", "NASDAQ", "Abcd Bancorp", "preferred"),
        # The fifth-letter rule is Nasdaq's convention only.
        ("ABCDW", "NYSE", "Abcd Holdings Inc.", None),
        ("ABC-U", "NYSE", "ABC Acquisition Corp", "unit"),
        ("HYT", "NYSE", "BlackRock Corporate High Yield Fund, Inc.", "fund"),
        ("PTRN", "NASDAQ", "Pattern Group Inc. Series A Common Stock", None),
    ],
)
def test_exclusion_reason(symbol, exchange, name, expected):
    assert exclusion_reason(symbol, name, exchange=exchange, is_etf=False, is_fund=False) == expected


def test_etf_and_fund_flags_take_priority():
    assert exclusion_reason("SPY", "SPDR S&P 500", is_etf=True, is_fund=False) == "etf"
    assert exclusion_reason("XYZ", "Anything", is_etf=False, is_fund=True) == "fund"


def test_sample_is_repeatable_and_sorted():
    symbols = [f"S{i:04d}" for i in range(500)]

    first = sample_symbols(symbols, 50, seed=7)

    assert first == sample_symbols(list(reversed(symbols)), 50, seed=7)
    assert first == sorted(first) and len(set(first)) == 50
    assert sample_symbols(symbols, 50, seed=8) != first
    assert sample_symbols(symbols[:10], 50, seed=7) == symbols[:10]


def test_one_listing_per_company_keeps_the_most_traded():
    from stratlib.universe import company_key
    rows = [{"symbol": "GOOGL", "companyName": "Alphabet Inc. Class A", "avgVolume": 30e6},
            {"symbol": "GOOG", "companyName": "Alphabet Inc.", "avgVolume": 20e6},
            {"symbol": "SO", "companyName": "The Southern Company", "avgVolume": 5e6},
            {"symbol": "SOMN", "companyName": "The Southern Company", "avgVolume": 2e5},
            {"symbol": "AMG", "companyName": "Affiliated Managers Group, Inc.", "avgVolume": None},
            {"symbol": "MGRB", "companyName": "Affiliated Managers Group, Inc.", "avgVolume": None},
            {"symbol": "SOJC", "companyName": "Southern Company (The) Series 2017B 5.25%", "avgVolume": 9e9}]
    stocks = {s.symbol: s.excluded_reason for s in parse_listings(rows)}
    assert stocks == {"GOOGL": None, "GOOG": "secondary listing", "SO": None, "SOMN": "secondary listing",
                      "AMG": None, "MGRB": "secondary listing", "SOJC": "fixed-income"}   # ties keep the shorter symbol
    assert company_key("Alphabet Inc. Class A") == company_key("ALPHABET INC") == "alphabet inc"
    banks = parse_listings([{"symbol": "FBP", "companyName": "First BanCorp.", "exchangeShortName": "NYSE"},
                            {"symbol": "FBNC", "companyName": "First Bancorp", "exchangeShortName": "NASDAQ"}])
    assert all(s.is_common for s in banks)      # different companies on different exchanges


def test_stored_universe_is_reclassified_without_an_api_call(store):
    from dataclasses import replace
    stocks = parse_listings([{"symbol": "BRK-A", "companyName": "Berkshire Hathaway Inc.", "avgVolume": 188},
                             {"symbol": "BRK-B", "companyName": "Berkshire Hathaway Inc.", "avgVolume": 4e6}])
    # As stored before this rule existed: both classes counted as common stock.
    store.replace_universe([replace(s, excluded_reason=None) for s in stocks])
    assert store.universe_symbols() == ["BRK-A", "BRK-B"]
    assert store.reclassify_universe() == 1
    assert store.universe_symbols() == ["BRK-B"]
    assert store.reclassify_universe() == 0
