"""Rule parameters for the scan strategies, validated like config.yaml's thresholds.

Every default reproduces the research configuration it was tested under (research/strategies/*.py
and the report named in strategies.py). Settings may override them under ``strategies:`` in
config.yaml. A position saves the values in force when it was opened, so a later edit never
changes an open holding's exits.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import ClassVar

from .config import ConfigError

MARKET_FILTERS = {
    "A": "No market filter",
    "B": "SPY above its 50-day average",
    "C": "QQQ 10-day average above its 20-day average",
    "D": "More than half of liquid stocks above their 50-day average",
    "E": "New highs minus new lows, 10-day average, above zero",
    "F": "SPY above its 50-day average and breadth above half",
}


@dataclass(frozen=True)
class ScanParams:
    """Fields every scan strategy shares: the research ground rules."""

    CHOICES: ClassVar[dict[str, tuple]] = {"market_filter": tuple(MARKET_FILTERS)}
    LABEL: ClassVar[str] = ""

    min_price: float = 5.0               # as-traded close on the signal day
    min_dollar_volume_m: float = 20.0    # 20-session average dollar volume, USD millions
    market_filter: str = "A"             # see MARKET_FILTERS; while off, no new signals are taken
    risk_pct: float = 0.5                # equity risked per trade: shares = risk / (entry - stop)
    max_position_pct: float = 20.0       # largest position, % of equity

    def __post_init__(self):
        name = type(self).__name__
        for field in dataclasses.fields(self):
            value, default = getattr(self, field.name), field.default
            label = f"strategies.{self.LABEL or name}.{field.name}"
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ConfigError(f"{label} must be true or false")
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigError(f"{label} must be an integer")
            elif isinstance(default, float):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ConfigError(f"{label} must be a finite number")
            elif isinstance(default, str):
                if not isinstance(value, str):
                    raise ConfigError(f"{label} must be text")
            if isinstance(default, (int, float)) and not isinstance(default, bool) and value < 0:
                raise ConfigError(f"{label} cannot be negative")
            if field.name in self.CHOICES and value not in self.CHOICES[field.name]:
                raise ConfigError(f"{label} must be one of {', '.join(map(str, self.CHOICES[field.name]))}")
        if not 0 < self.risk_pct <= 100 or not 0 < self.max_position_pct <= 100:
            raise ConfigError(f"strategies.{self.LABEL or name}: risk_pct and max_position_pct must be in (0, 100]")
        self.validate()

    def validate(self) -> None:
        """Strategy-specific cross-field checks."""


@dataclass(frozen=True)
class QullamaggieParams(ScanParams):
    """research/strategies/qullamaggie.py, pivot entry, 10-day trail (the as-specified test)."""

    LABEL: ClassVar[str] = "qullamaggie"
    lookback: int = 60
    prior_move_pct: float = 30.0
    consolidation_min: int = 10
    consolidation_max: int = 40
    max_depth_pct: float = 25.0
    tight_sessions: int = 5
    tight_range_pct: float = 12.0
    sma_fast: int = 10
    sma_slow: int = 20
    sma_rising_sessions: int = 5
    adr_sessions: int = 20
    min_adr_pct: float = 4.0
    order_life: int = 5
    max_stop_adr: float = 1.0            # skip the trade when the stop is further than this many ADRs
    partial_session: int = 3
    partial_fraction: float = 1 / 3
    trail_sma: int = 10

    def validate(self):
        if not 1 <= self.consolidation_min <= self.consolidation_max:
            raise ConfigError("strategies.qullamaggie: consolidation_min must be between 1 and consolidation_max")
        if min(self.lookback, self.tight_sessions, self.sma_fast, self.sma_slow, self.sma_rising_sessions,
               self.adr_sessions, self.order_life, self.partial_session, self.trail_sma) < 1:
            raise ConfigError("strategies.qullamaggie: session counts must be positive")
        if self.trail_sma not in (self.sma_fast, self.sma_slow):
            raise ConfigError("strategies.qullamaggie: trail_sma must be sma_fast or sma_slow")
        if not 0 < self.partial_fraction < 1:
            raise ConfigError("strategies.qullamaggie: partial_fraction must be between 0 and 1")


@dataclass(frozen=True)
class MinerviniParams(ScanParams):
    """research/strategies/minervini.py: trend template plus volatility contraction pattern."""

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "exit": ("c10", "half20")}
    LABEL: ClassVar[str] = "minervini"
    rs_min_pct: float = 70.0
    swing_pct: float = 3.0
    window: int = 60
    min_pullbacks: int = 2
    final_max_pct: float = 10.0
    volume_dry: float = 1.0
    breakout_volume: float = 1.4
    max_stop_pct: float = 8.0
    order_life: int = 5
    # c10: a third at the day-3 close, stop to breakeven, 10-day average trail (research "b").
    # half20: half at +20%, the rest on a close below the 50-day average (research "a").
    exit: str = "c10"

    def validate(self):
        if not 0 <= self.rs_min_pct <= 100 or not 0 < self.swing_pct < 50:
            raise ConfigError("strategies.minervini: rs_min_pct must be 0..100 and swing_pct between 0 and 50")
        if min(self.window, self.min_pullbacks, self.order_life) < 1:
            raise ConfigError("strategies.minervini: window, min_pullbacks and order_life must be positive")
        if not 0 < self.max_stop_pct < 100 or self.volume_dry <= 0 or self.breakout_volume <= 0:
            raise ConfigError("strategies.minervini: invalid stop or volume ratio")


@dataclass(frozen=True)
class EpisodicPivotParams(ScanParams):
    """research/strategies/episodic_pivot.py with the corrected liquidity and breakeven rules."""

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "exit": ("c10", "time20", "trail10")}
    LABEL: ClassVar[str] = "episodic_pivot"
    market_filter: str = "B"             # the corrected report's runs: SPY above its 50-day average
    gap_pct: float = 20.0
    volume_mult: float = 3.0
    volume_sessions: int = 50
    earnings: bool = True                # the gap must follow an earnings release
    neglected: bool = False
    neglect_sessions: int = 60
    neglect_max_pct: float = 20.0
    close_in_range: float = 0.5
    # c10: a third at the day-3 close if above entry, stop to breakeven, 10-day average trail.
    # time20: initial stop and a sale at the 20th session. trail10: initial stop until session 20,
    # then a close below the 10-day average.
    exit: str = "c10"

    def validate(self):
        if self.volume_sessions < 1 or self.neglect_sessions < 1 or not 0 <= self.close_in_range <= 1:
            raise ConfigError("strategies.episodic_pivot: invalid session count or close_in_range")


@dataclass(frozen=True)
class EmaPullbackParams(ScanParams):
    """research/strategies/ema_pullback.py (the Traveling Trader's 9/21 EMA trade)."""

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "exit_rule": ("first", "second")}
    LABEL: ClassVar[str] = "ema_pullback"
    ema_fast: int = 9
    ema_slow: int = 21
    sma_mid: int = 50
    sma_long: int = 200
    high_recent: int = 20
    high_lookback: int = 126
    fresh_sessions: int = 3
    stop_pct: float = 3.0                # below the slow EMA at entry
    exit_rule: str = "first"             # first or second close below the slow EMA
    reset_sessions: int = 10

    def validate(self):
        if not 1 <= self.ema_fast < self.ema_slow or self.high_recent > self.high_lookback:
            raise ConfigError("strategies.ema_pullback: EMA spans must increase and high_recent fit high_lookback")
        if min(self.sma_mid, self.sma_long, self.high_recent, self.fresh_sessions, self.reset_sessions) < 1:
            raise ConfigError("strategies.ema_pullback: session counts must be positive")
        if not 0 < self.stop_pct < 100:
            raise ConfigError("strategies.ema_pullback: stop_pct must be between 0 and 100")


@dataclass(frozen=True)
class ChecklistParams(ScanParams):
    """research/strategies/tt_checklist_top10.py: the Traveling Trader's fundamentals checklist as a portfolio.

    Non-financial stocks pass when trailing P/E is under the company's own median, trailing PEG is at most
    peg_max, ROIC is at least roic_min_pct, debt/equity is under debt_equity_max and free cash flow is rising
    (research/tt_quality.py, "QUAL"). The passers with the best 63-session return are held, quarterly.
    """

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "trend_gate": ("none", "stock", "market", "both")}
    LABEL: ClassVar[str] = "tt_checklist"
    max_position_pct: float = 100 / 3    # the research caps each entry at a third of equity
    top_n: int = 10
    stop_pct: float = 20.0               # below the entry, not trailed
    trend_gate: str = "none"             # entry-only gates: stock and/or SPY above their 200-day average
    trend_sessions: int = 200
    roic_min_pct: float = 15.0
    debt_equity_max: float = 1.0
    peg_max: float = 1.0
    stale_days: int = 200                # statements older than this (from the quarter end) make a stock unevaluable

    def validate(self):
        if self.top_n < 1 or self.trend_sessions < 2 or self.stale_days < 1:
            raise ConfigError("strategies.tt_checklist: top_n, trend_sessions and stale_days must be positive")
        if not 0 < self.stop_pct < 100 or self.debt_equity_max <= 0 or self.peg_max <= 0:
            raise ConfigError("strategies.tt_checklist: invalid stop, debt/equity or PEG limit")


@dataclass(frozen=True)
class NashParams(ScanParams):
    """research/nash_screen.py and nash_screen_clean.py: Tom Nash's quality checklist, ranked by Rule of 40."""

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "margin": ("FCF", "OM")}
    LABEL: ClassVar[str] = "nash_quality"
    margin: str = "FCF"                  # free-cash-flow or operating margin for rule 3
    min_revenue_growth_pct: float = 10.0
    min_margin_pct: float = 15.0
    exclude_cyclicals: bool = False      # rule 7, the "recession-proof" industry screen
    # The research's post-hoc cleanup of FMP data errors: one reporting currency over the eight quarters,
    # free-cash-flow margin within +-100% and prior-year revenue of at least min_prior_revenue_m.
    data_guards: bool = True
    min_prior_revenue_m: float = 250.0
    slots: int = 10
    stale_days: int = 200

    def validate(self):
        if self.slots < 1 or self.stale_days < 1 or self.min_prior_revenue_m < 0:
            raise ConfigError("strategies.nash_quality: slots and stale_days must be positive")


@dataclass(frozen=True)
class MsciGarpParams(ScanParams):
    """research/mscigarp_index.py: the MSCI USA Quality GARP Select Index rebuilt on the S&P 500 (msci_garp.py).

    The index has no price or liquidity floor and no market rule: min_price and min_dollar_volume_m are not read, and
    the weights are the index's (parent cap x tilt, capped), not the research's risk sizing.
    """

    CHOICES: ClassVar[dict[str, tuple]] = {**ScanParams.CHOICES, "growth_variant": ("proxy", "methodology")}
    LABEL: ClassVar[str] = "msci_garp"
    min_price: float = 0.0
    min_dollar_volume_m: float = 0.0
    # proxy: trailing-year EPS growth stands in for the short-term forecast; methodology: both forecasts left out, as
    # MSCI's missing-data rule would (it tracked MSCI's index less closely).
    growth_variant: str = "proxy"
    coverage_pct: float = 50.0           # selection covers this share of the parent's market cap
    buffer_low_pct: float = 35.0         # everything to this coverage is in
    buffer_high_pct: float = 65.0        # current members up to this coverage come before others
    max_issuer_pct: float = 5.0
    sector_band_pct: float = 5.0         # each sector within this many points of its share of the selected cap
    warmup_reviews: int = 12             # reviews rebuilt before the latest, so the buffer acts as it would have

    def validate(self):
        if not 0 < self.buffer_low_pct <= self.coverage_pct <= self.buffer_high_pct < 100:
            raise ConfigError("strategies.msci_garp: need 0 < buffer_low_pct <= coverage_pct <= buffer_high_pct < 100")
        if not 0 < self.max_issuer_pct <= 100 or not 0 <= self.sector_band_pct <= 100:
            raise ConfigError("strategies.msci_garp: max_issuer_pct must be in (0, 100] and sector_band_pct in [0, 100]")
        if self.warmup_reviews < 0:
            raise ConfigError("strategies.msci_garp: warmup_reviews cannot be negative")


PARAM_TYPES: dict[str, type[ScanParams]] = {
    "qullamaggie": QullamaggieParams,
    "minervini": MinerviniParams,
    "episodic_pivot": EpisodicPivotParams,
    "ema_pullback": EmaPullbackParams,
    "tt_checklist": ChecklistParams,
    "nash_quality": NashParams,
    "msci_garp": MsciGarpParams,
}


def build_params(strategy_id: str, values: dict | None) -> ScanParams:
    """Defaults plus config overrides; unknown keys are an error, not silently ignored."""
    cls = PARAM_TYPES[strategy_id]
    if values is not None and not isinstance(values, dict):
        raise ConfigError(f"strategies.{strategy_id} must be a mapping")
    values = dict(values or {})
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ConfigError(f"Unknown setting(s) in [strategies.{strategy_id}]: {', '.join(unknown)}")
    return cls(**values)
