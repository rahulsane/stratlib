"""Supported workspace strategies, saved rule sets and screen projections.

Research reports are evidence, not registrations. Only strategies implemented here
can create a live screen or own positions.

Two kinds of strategy exist. ``screen`` strategies (CANSLIM, Trend Leaders) read the facts the
C/A/S/L screen saves. ``scan`` strategies (scanning.py) apply their research rules to the cached
daily bars directly and carry their own parameters, saved with every screen and position.
"""

from dataclasses import asdict, dataclass
import hashlib
import json

from .config import BacktestSettings, Thresholds
from .scoring import Criterion, meets_rule, trend_order
from .strategy_params import MARKET_FILTERS, PARAM_TYPES, ScanParams, build_params
from .technical import buyable


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    variant: str
    variant_name: str
    backtest_label: str
    description: str
    family: str = ""                    # the research family whose reports back it
    engine: str = "screen"              # screen: the C/A/S/L facts; scan: its own scan of the cache
    uses_exposure: bool = True          # new buying is limited by the market-direction exposure ladder
    reports: tuple[str, ...] = ()       # report slugs in research/reports.json

    @property
    def scan(self) -> bool:
        return self.engine == "scan"


STRATEGIES = {
    "canslim": Strategy("canslim", "CANSLIM", "breakout", "Breakout trades", "Breakout trades",
                        "Buy qualified breakouts; use the loss limit, profit target and fast-gain hold exception."),
    "trend": Strategy("trend", "Trend Leaders", "long-trend", "Long trend", "Trend leaders",
                      "Rank price leaders by relative strength and sales growth; hold until the long trend breaks."),
    "qullamaggie": Strategy(
        "qullamaggie", "Qullamaggie Breakout", "breakout-trail", "Breakout, 10-day trail", "Qullamaggie breakout",
        "Buy a tight consolidation after a large prior move when it breaks through its pivot; sell a third on day 3, "
        "then trail the rest under the 10-day average.",
        family="Qullamaggie", engine="scan", uses_exposure=False,
        reports=("qullamaggie-breakout", "breakout-entry-rules", "breakout-exit-rules")),
    "minervini": Strategy(
        "minervini", "Minervini VCP", "trend-template-vcp", "Trend template and VCP", "Minervini VCP",
        "Buy a stock in a Stage 2 uptrend when it breaks out of a volatility contraction pattern on heavy volume.",
        family="Minervini", engine="scan", uses_exposure=False, reports=("minervini-vcp",)),
    "episodic_pivot": Strategy(
        "episodic_pivot", "Episodic Pivot", "earnings-gap", "Earnings gap", "Episodic pivot",
        "Buy the close of a stock that gaps up on earnings with heavy volume and closes strong, while SPY is above "
        "its 50-day average.",
        family="Episodic Pivot", engine="scan", uses_exposure=False,
        reports=("episodic-pivot-corrected", "episodic-pivot-robustness", "episodic-pivot-signals",
                 "episodic-pivot-cash-and-overlay", "episodic-pivot-position-sizing")),
    "ema_pullback": Strategy(
        "ema_pullback", "Traveling Trader 9/21 EMA", "ema-pullback", "9/21 EMA pullback", "9/21 EMA pullback",
        "Buy a pullback to the 9-day average in a strong uptrend; sell at the first close below the 21-day average.",
        family="Traveling Trader", engine="scan", uses_exposure=False, reports=("ema-pullback",)),
    "tt_checklist": Strategy(
        "tt_checklist", "Traveling Trader Checklist", "quality-top-ten", "Quality checklist, top ten",
        "Traveling Trader checklist",
        "Hold the ten best-performing stocks that pass the quality checklist: P/E under its own history, PEG at most 1, "
        "ROIC at least 15%, debt/equity under 1 and free cash flow rising. Rebalance quarterly.",
        family="Traveling Trader", engine="scan", uses_exposure=False,
        reports=("checklist-top-ten", "checklist-trend-filters", "checklist-portfolio-construction",
                 "fundamentals-checklist", "traveling-trader-overview")),
    "nash_quality": Strategy(
        "nash_quality", "Nash Quality Screen", "rule-of-40", "Quality screen, Rule of 40", "Nash quality screen",
        "Hold up to ten profitable, growing companies with more cash than debt, ranked by Rule of 40 (revenue growth plus "
        "free-cash-flow margin). Rebalance quarterly.",
        family="Tom Nash", engine="scan", uses_exposure=False, reports=("nash-quality-screen", "nash-rule-study")),
    "msci_garp": Strategy(
        "msci_garp", "MSCI GARP", "quality-garp-select", "USA Quality GARP Select", "MSCI GARP",
        "Hold the MSCI USA Quality GARP Select Index (the iShares GARP ETF's index), rebuilt from MSCI's rules: the "
        "fastest-growing half of the market by value, weighted by market cap and tilted toward quality and value. "
        "Rebalance quarterly at the end of February, May, August and November.",
        family="MSCI", engine="scan", uses_exposure=False, reports=("msci-usa-quality-garp", "sp500-garp-index")),
}
# The index strategies hold what their index holds: as many stocks as the rules select, at the index's weights.
INDEXES = ("msci_garp",)
# More holdings than an index strategy can select (half the S&P 500's value by growth has never needed 300 stocks).
INDEX_HOLDINGS = 500


def strategy(strategy_id: str) -> Strategy:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"Unsupported strategy: {strategy_id}")
    return STRATEGIES[strategy_id]


def scan_params(strategy_id: str, source=None) -> ScanParams:
    """A scan strategy's parameters from Settings (or a mapping), else the research defaults."""
    if strategy_id not in PARAM_TYPES:
        raise ValueError(f"{strategy_id} has no scan parameters")
    if isinstance(source, ScanParams):
        return source
    strategies = getattr(source, "strategies", None)
    if strategies is not None and strategy_id in strategies:
        return strategies[strategy_id]
    return build_params(strategy_id, source if isinstance(source, dict) else None)


def holding_limit(strategy_id: str, params, portfolio: BacktestSettings) -> int:
    """Holdings the strategy's model portfolio may carry: its own count where the research fixed one."""
    if strategy_id in ("tt_checklist", "nash_quality"):
        chosen = scan_params(strategy_id, params)
        return chosen.top_n if strategy_id == "tt_checklist" else chosen.slots
    if strategy_id in INDEXES:
        return INDEX_HOLDINGS
    return portfolio.max_holdings


def equal_weighted(strategy_id: str) -> bool:
    """Strategies the research split equally among holdings, rather than sizing each trade by its stop risk."""
    return strategy_id in ("tt_checklist", "nash_quality")


def rule_snapshot(strategy_id: str, thresholds: Thresholds, portfolio: BacktestSettings, params=None) -> dict:
    spec = strategy(strategy_id)
    if spec.scan:
        # Only what the strategy reads: unrelated threshold or portfolio edits must not look like a rule change.
        return {"version": 1, "strategy_id": spec.id, "variant_id": spec.variant,
                "params": asdict(scan_params(strategy_id, params)),
                "portfolio": {"max_holdings": holding_limit(strategy_id, params, portfolio)}}
    return {"version": 1, "strategy_id": spec.id, "variant_id": spec.variant,
            "thresholds": asdict(thresholds), "portfolio": asdict(portfolio)}


def current_rules(settings, strategy_id: str) -> dict:
    """The rule snapshot for the strategy under the current Settings."""
    return rule_snapshot(strategy_id, settings.thresholds, settings.backtest, settings)


def snapshot_spec(snapshot: dict) -> Strategy:
    if snapshot.get("version") != 1:
        raise ValueError("Unsupported saved rule version")
    spec = strategy(snapshot["strategy_id"])
    if snapshot.get("variant_id") != spec.variant:
        raise ValueError("Unsupported saved variant")
    return spec


def read_rules(snapshot: dict) -> tuple[Strategy, Thresholds, BacktestSettings]:
    spec = snapshot_spec(snapshot)
    if spec.scan:
        raise ValueError(f"{spec.name} saves strategy parameters, not C/A/S/L thresholds")
    return spec, Thresholds(**snapshot["thresholds"]), BacktestSettings(**snapshot["portfolio"])


def read_params(snapshot: dict) -> tuple[Strategy, ScanParams, int]:
    """(strategy, parameters, maximum holdings) of a saved scan-strategy rule set."""
    spec = snapshot_spec(snapshot)
    if not spec.scan:
        raise ValueError(f"{spec.name} has no scan parameters")
    return spec, build_params(spec.id, snapshot["params"]), int(snapshot["portfolio"]["max_holdings"])


def revision(snapshot: dict | None) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:8] if snapshot else "Legacy"


def candidate_rows(report: dict, strategy_id: str, t: Thresholds, options: BacktestSettings) -> list[dict]:
    """Rank candidates using the same selection predicates as the backtests.

    Market exposure is applied by construction, separately from candidate quality.
    """
    spec = strategy(strategy_id)
    if spec.scan:
        return report.get("candidates", [])
    leaders = []
    for row in report["rows"]:
        prices = row.get("prices") or {}
        if not row.get("criteria") or not prices.get("price_pass") or (row.get("rs") or 0) < t.rs_min:
            continue
        criteria = [Criterion(**c) for c in row["criteria"]]
        if strategy_id == "trend":
            traded = (prices.get("close") or 0) * (prices.get("avg_volume") or 0)
            order, _ = trend_order(row["symbol"], row["rs"], row.get("industry_stats"), criteria, traded, t, options)
            if order is None:
                continue
        else:
            if not meets_rule(criteria, t) or not buyable(row.get("base") or {}, report["price_date"], t):
                continue
            order = (-row["rs"], row["symbol"])
        sales = next((c.value for c in criteria if c.key == "c_sales"), None)
        leaders.append({"order": list(order), "symbol": row["symbol"], "name": row.get("name"), "rs": row["rs"],
                        "sales": sales, "industry": row.get("industry"),
                        "industry_rank": (row.get("industry_stats") or {}).get("rank"),
                        "close": prices.get("close")})
    return sorted(leaders, key=lambda leader: leader["order"])


def project_screen(report: dict, strategy_id: str, t: Thresholds, options: BacktestSettings) -> dict:
    """Project immutable screen facts; refuse facts evaluated under different thresholds."""
    if strategy(strategy_id).scan:
        raise ValueError("Scan strategies read prices directly; run a scan instead of projecting saved screen facts.")
    if Thresholds(**report["thresholds"]) != t:
        raise ValueError("Thresholds changed. Run a fresh screen before evaluating this strategy.")
    snapshot = rule_snapshot(strategy_id, t, options)
    return {**report, "strategy_id": strategy_id, "variant_id": snapshot["variant_id"],
            "rule_snapshot": snapshot, "candidates": candidate_rows(report, strategy_id, t, options)}


def latest_screen(store, strategy_id: str) -> dict | None:
    spec = strategy(strategy_id)
    saved = store.document(f"screen:latest:{spec.id}:{spec.variant}")
    return saved or (store.document("latest_screen") if strategy_id == "canslim" else None)


def market_policy(strategy_id: str, options: BacktestSettings, params=None) -> str:
    spec = strategy(strategy_id)
    if spec.scan:
        code = scan_params(strategy_id, params).market_filter
        if code == "A":
            compared = (" The research compared six market filters on it and none helped reliably."
                        if strategy_id in ("qullamaggie", "minervini") else
                        " The research tried a switch to cash below the S&P 500's 200-day average: it halved the worst "
                        "drawdown since 2002 but cost about 2 points a year." if strategy_id == "msci_garp" else "")
            return (f"{spec.name} is tested and run without a market rule. The exposure ladder belongs to CANSLIM and "
                    f"Trend Leaders.{compared}")
        return (f"New entries wait while this filter is off ({MARKET_FILTERS[code]}). Open holdings keep "
                "their saved exits. The exposure ladder belongs to CANSLIM and Trend Leaders.")
    if strategy_id == "trend":
        return "Exposure limits new entries. Existing holdings keep their saved trend exits; falling exposure does not force a sale."
    if options.raise_cash:
        return "Exposure limits new entries. When exposure falls, reduce the weakest holdings to the permitted number of slots."
    return "Exposure limits new entries. Existing holdings keep their saved exit rules; falling exposure does not force a sale."
