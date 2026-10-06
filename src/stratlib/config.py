"""Settings from config.yaml, secrets from .env."""

from __future__ import annotations

import dataclasses
import os
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class FMPSettings:
    base_url: str = "https://financialmodelingprep.com/stable"
    calls_per_minute: int = 700
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    timeout_seconds: float = 30.0
    max_workers: int = 8


@dataclass(frozen=True)
class DataSettings:
    db_path: Path = Path("data/stratlib.db")
    ratelimit_db_path: Path = Path("data/ratelimit.db")
    log_dir: Path = Path("logs")


@dataclass(frozen=True)
class UniverseSettings:
    exchanges: tuple[str, ...] = ("NYSE", "NASDAQ", "AMEX")
    include_all_share_classes: bool = False
    screener_page_size: int = 5000
    refresh_hours: float = 20.0


@dataclass(frozen=True)
class PriceSettings:
    history_years: int = 5
    eod_final_hour_et: int = 18
    market_symbols: tuple[str, ...] = ("^GSPC", "^IXIC", "SPY", "QQQ")
    restate_tolerance_pct: float = 0.5
    sample_seed: int = 7


@dataclass(frozen=True)
class Thresholds:
    min_price: float = 15.0
    min_avg_volume: float = 400_000
    max_below_high_pct: float = 15.0
    high_sessions: int = 252
    short_ma_sessions: int = 50
    long_ma_sessions: int = 200
    volume_sessions: int = 50
    rs_quarter_sessions: int = 63
    rs_recent_weight: float = 2.0
    rs_min: int = 80
    industry_sessions: int = 126
    industry_top: int = 40
    eps_growth_pct: float = 25.0
    eps_acceleration_min: int = 2
    sales_growth_pct: float = 25.0
    margin_near_high_pct: float = 5.0
    annual_eps_growth_pct: float = 25.0
    roe_min_pct: float = 17.0
    cash_flow_above_eps_pct: float = 20.0
    shares_max_growth_pct: float = 0.0
    shares_latest_max_growth_pct: float = 0.0
    debt_equity_max_growth_pct: float = 0.0
    one_time_item_pct: float = 20.0
    max_quarter_age_days: int = 190
    max_annual_age_days: int = 550
    # Quarterly EPS, quarterly sales and RS must always pass. Of the other eight
    # C/A/S/L checks, this many must pass. Unmeasurable checks are left out when
    # at least min_measurable_scored_checks can be measured; the requirement then
    # scales down in proportion. 8 and 8 are the specification: every check passes.
    scored_checks_required: int = 8
    min_measurable_scored_checks: int = 8
    distribution_decline_pct: float = 0.2
    distribution_window_sessions: int = 25
    distribution_pressure_count: int = 4
    distribution_correction_count: int = 5
    follow_through_gain_pct: float = 1.25
    follow_through_min_day: int = 4
    market_volume_stale_sessions: int = 5
    distribution_expiry_gain_pct: float = 0.0    # 0 keeps the specification's 25-session window only
    market_index_rule: str = "both"              # both | either | sp500 | nasdaq | average | ignore
    # 0 is the specification: distribution_correction_count ends an uptrend. Above
    # 0, distribution days only reach "under pressure"; a correction needs the
    # index to close this far below its peak since the follow-through, or to
    # undercut the rally low.
    correction_drawdown_pct: float = 0.0
    distribution_heavy_count: int = 6
    # Share of the portfolio that may be invested in each market condition. The
    # defaults reproduce the specification's gate: buy only in a confirmed uptrend.
    exposure_confirmed_pct: float = 100.0         # confirmed, below pressure_count - 1 distribution days
    exposure_late_confirmed_pct: float = 100.0    # confirmed, pressure_count - 1 distribution days
    exposure_new_uptrend_pct: float = 100.0       # cap during the first sessions after a follow-through
    exposure_new_uptrend_sessions: int = 10
    exposure_pressure_pct: float = 0.0            # under pressure, below distribution_heavy_count
    exposure_heavy_pressure_pct: float = 0.0      # under pressure, distribution_heavy_count or more
    base_lookback_weeks: int = 78
    base_max_weeks: int = 65
    base_recent_weeks: int = 3
    cup_min_weeks: int = 7
    cup_min_depth_pct: float = 12.0
    cup_max_depth_pct: float = 33.0
    cup_correction_max_depth_pct: float = 50.0
    cup_side_min_weeks: int = 2
    base_rim_tolerance_pct: float = 5.0
    handle_min_weeks: int = 1
    handle_max_weeks: int = 2
    handle_max_depth_pct: float = 12.0
    handle_volume_max_ratio: float = 1.0
    cup_pivot_offset: float = 0.10
    # O'Neil's cup without handle: the same cup rules, pivot at the left rim's high.
    # 0 keeps the specification's three patterns; above 0 is the minimum length.
    cup_without_handle_min_weeks: int = 0
    double_bottom_min_weeks: int = 7
    flat_min_weeks: int = 5
    flat_max_depth_pct: float = 15.0
    # O'Neil's second-stage flat base: within this many weeks before the flat base
    # began, the stock broke out of an earlier base and its high then rose at
    # least flat_prior_advance_pct above that base's pivot. 0 is the specification.
    flat_prior_breakout_weeks: int = 0
    flat_prior_advance_pct: float = 20.0
    buy_zone_max_pct: float = 5.0
    # 0 is the specification: buy only at the open after the breakout close. Above
    # 0, a backtest may buy on any close within this many weeks of the breakout
    # while the stock is still in the buy zone, as the Screen's N checks allow.
    buy_zone_entry_weeks: int = 0
    breakout_volume_pct: float = 40.0
    breakout_volume_sessions: int = 50
    stop_loss_pct: float = 7.0
    profit_target_pct: float = 20.0
    fast_gain_pct: float = 20.0
    fast_gain_weeks: int = 3
    minimum_hold_weeks: int = 8

    def __post_init__(self):
        if self.market_index_rule not in ("both", "either", "sp500", "nasdaq", "average", "ignore"):
            raise ConfigError("thresholds.market_index_rule must be both, either, sp500, nasdaq, average or ignore")
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name == "market_index_rule":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError(f"thresholds.{field.name} must be a finite number")
            if isinstance(field.default, int) and not isinstance(value, int):
                raise ConfigError(f"thresholds.{field.name} must be an integer")
            if value < 0:
                raise ConfigError(f"thresholds.{field.name} cannot be negative")
            if (field.name.endswith(('sessions', 'days')) or field.name == 'rs_recent_weight') and value <= 0:
                raise ConfigError(f"thresholds.{field.name} must be positive")
        if not 1 <= self.rs_min <= 99 or not 1 <= self.eps_acceleration_min <= 3:
            raise ConfigError("RS must be 1..99; EPS acceleration must be 1..3")
        if self.industry_top < 1 or self.max_below_high_pct > 100 or self.margin_near_high_pct > 100:
            raise ConfigError("Invalid industry rank or percentage threshold")
        if any(getattr(self, name) < 1 for name in (
            'base_lookback_weeks', 'base_max_weeks', 'cup_min_weeks', 'cup_side_min_weeks',
            'handle_min_weeks', 'handle_max_weeks', 'double_bottom_min_weeks', 'flat_min_weeks',
        )):
            raise ConfigError("Base durations must be positive")
        if not (1 <= self.distribution_pressure_count < self.distribution_correction_count
                <= self.distribution_window_sessions):
            raise ConfigError("Distribution counts must satisfy pressure < correction <= window")
        if self.cup_without_handle_min_weeks and not (
                2 * self.cup_side_min_weeks + 1 <= self.cup_without_handle_min_weeks <= self.base_max_weeks):
            raise ConfigError("Cup-without-handle length must be 0 (off) or fit a trough between two rims")
        if self.flat_prior_breakout_weeks > self.base_lookback_weeks:
            raise ConfigError("The flat base's prior-breakout window cannot exceed the base lookback")
        if not 0 <= self.buy_zone_entry_weeks <= self.base_recent_weeks:
            raise ConfigError("Buy-zone entry weeks must be between 0 and the recent-breakout window")
        if self.distribution_heavy_count < self.distribution_pressure_count:
            raise ConfigError("Heavy distribution count cannot be below the pressure count")
        if any(getattr(self, name) > 100 for name in (
                'correction_drawdown_pct', 'exposure_confirmed_pct', 'exposure_late_confirmed_pct',
                'exposure_new_uptrend_pct', 'exposure_pressure_pct', 'exposure_heavy_pressure_pct')):
            raise ConfigError("Exposure and drawdown percentages must be between 0 and 100")
        if not 0 <= self.scored_checks_required <= 8 or not 1 <= self.min_measurable_scored_checks <= 8:
            raise ConfigError("Scored C/A/S/L checks: required 0..8, minimum measurable 1..8")
        if self.follow_through_min_day < 2 or self.market_volume_stale_sessions < 2:
            raise ConfigError("Follow-through day and stale-volume window must be at least 2")
        if not (0 < self.cup_min_depth_pct <= self.cup_max_depth_pct
                <= self.cup_correction_max_depth_pct < 100):
            raise ConfigError("Invalid cup depth range")
        if not (self.handle_min_weeks <= self.handle_max_weeks
                and self.cup_min_weeks >= 2 * self.cup_side_min_weeks + 1
                and self.double_bottom_min_weeks >= 5 and self.flat_min_weeks >= 2
                and max(self.cup_min_weeks + self.handle_max_weeks, self.double_bottom_min_weeks,
                        self.flat_min_weeks) <= self.base_max_weeks <= self.base_lookback_weeks):
            raise ConfigError("Invalid base duration range")
        if not 0 < self.handle_volume_max_ratio <= 1 or any(
            not 0 <= getattr(self, name) < 100 for name in (
                'base_rim_tolerance_pct', 'handle_max_depth_pct', 'flat_max_depth_pct', 'buy_zone_max_pct')):
            raise ConfigError("Invalid base percentage or volume ratio")
        if not 0 < self.stop_loss_pct < 100 or self.profit_target_pct <= 0 or self.fast_gain_pct <= 0:
            raise ConfigError("Stop loss must be between 0 and 100%; profit and fast-gain targets must be positive")
        if not 1 <= self.fast_gain_weeks < self.minimum_hold_weeks:
            raise ConfigError("Hold duration must be longer than the positive fast-gain window")


@dataclass(frozen=True)
class BacktestSettings:
    initial_capital: float = 100_000.0
    max_holdings: int = 10
    default_years: int = 1
    raise_cash: bool = False    # sell the weakest holdings when market exposure falls below them
    # Leaders portfolio: hold the top-ranked stocks that pass the Screen. At each
    # rebalance, sell holdings that no longer pass or rank below leaders_rank_buffer.
    leaders_rank_buffer: int = 20
    leaders_rebalance: str = "monthly"      # monthly | weekly
    stop_loss: bool = True                  # backtests only: False ignores thresholds.stop_loss_pct
    # Trend leaders: the rules the nine-year rule study supported (README, "Rule study").
    # Buy price and RS leaders with RS >= trend_min_rs, an industry group in
    # thresholds.industry_top and quarterly sales growth >= trend_min_sales_growth_pct.
    # Sell only on a close below the long moving average or a loss of trend_loss_cap_pct.
    trend_min_rs: int = 90
    trend_min_sales_growth_pct: float = 20.0
    trend_min_dollar_volume_m: float = 0.0  # average daily trading value, USD millions
    trend_loss_cap_pct: float = 0.0         # sell this far below cost; 0 means no cap

    def __post_init__(self):
        if (isinstance(self.initial_capital, bool) or not isinstance(self.initial_capital, (float, int))
                or not math.isfinite(self.initial_capital) or self.initial_capital <= 0):
            raise ConfigError("backtest.initial_capital must be positive and finite")
        if not isinstance(self.raise_cash, bool):
            raise ConfigError("backtest.raise_cash must be true or false")
        if not isinstance(self.stop_loss, bool):
            raise ConfigError("backtest.stop_loss must be true or false")
        if self.leaders_rebalance not in ("monthly", "weekly"):
            raise ConfigError("backtest.leaders_rebalance must be monthly or weekly")
        if isinstance(self.leaders_rank_buffer, bool) or not isinstance(self.leaders_rank_buffer, int) \
                or self.leaders_rank_buffer < self.max_holdings:
            raise ConfigError("backtest.leaders_rank_buffer must be an integer of at least max_holdings")
        if isinstance(self.trend_min_rs, bool) or not isinstance(self.trend_min_rs, int) or not 1 <= self.trend_min_rs <= 99:
            raise ConfigError("backtest.trend_min_rs must be an integer from 1 to 99")
        for name in ("trend_min_sales_growth_pct", "trend_min_dollar_volume_m", "trend_loss_cap_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError(f"backtest.{name} must be a number")
        if self.trend_min_dollar_volume_m < 0 or not 0 <= self.trend_loss_cap_pct < 100:
            raise ConfigError("backtest.trend_min_dollar_volume_m must be at least 0 and trend_loss_cap_pct from 0 to 99")
        for name in ("max_holdings", "default_years"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"backtest.{name} must be a positive integer")


@dataclass(frozen=True)
class JevSettings:
    enabled: bool = False
    model: str = "typesafe-ai/jev"
    timeout_seconds: float = 30.0
    max_retries: int = 2

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ConfigError("jev.enabled must be true or false")
        if not isinstance(self.model, str) or not re.fullmatch(r"typesafe-ai/jev(?:-\d+\.\d+\.\d+)?", self.model):
            raise ConfigError("jev.model must be typesafe-ai/jev or a Gateway-supported versioned Jev ID")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120):
            raise ConfigError("jev.timeout_seconds must be greater than zero and at most 120")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 5:
            raise ConfigError("jev.max_retries must be an integer from 0 to 5")


@dataclass(frozen=True)
class Settings:
    fmp: FMPSettings
    data: DataSettings
    universe: UniverseSettings
    prices: PriceSettings
    path: Path
    # The parsed YAML, for sections later phases add (thresholds, jev).
    raw: dict[str, Any]
    thresholds: Thresholds = dataclasses.field(default_factory=Thresholds)
    backtest: BacktestSettings = dataclasses.field(default_factory=BacktestSettings)
    jev: JevSettings = dataclasses.field(default_factory=JevSettings)
    # Rule parameters of the scan strategies, by strategy id (see strategy_params.py).
    strategies: dict[str, Any] = dataclasses.field(default_factory=dict)


def _build(cls: type, section: str, values: dict[str, Any] | None, base_dir: Path):
    if values is not None and not isinstance(values, dict):
        raise ConfigError(f"[{section}] must be a mapping")
    values = dict(values or {})
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ConfigError(f"Unknown setting(s) in [{section}]: {', '.join(unknown)}")
    for name, value in values.items():
        default = fields[name].default
        if isinstance(default, tuple):
            values[name] = tuple(value)
        elif isinstance(default, Path):
            path = Path(value)
            values[name] = path if path.is_absolute() else base_dir / path
    for name, field in fields.items():
        if name not in values and isinstance(field.default, Path):
            values[name] = base_dir / field.default
    return cls(**values)


def _strategy_params(raw: Any) -> dict[str, Any]:
    from .strategy_params import PARAM_TYPES, build_params
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError("[strategies] must be a mapping")
    unknown = sorted(set(raw or {}) - set(PARAM_TYPES))
    if unknown:
        raise ConfigError(f"Unknown strategy in [strategies]: {', '.join(unknown)}")
    return {strategy_id: build_params(strategy_id, (raw or {}).get(strategy_id)) for strategy_id in PARAM_TYPES}


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config file: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")
    base_dir = path.resolve().parent
    return Settings(
        fmp=_build(FMPSettings, "fmp", raw.get("fmp"), base_dir),
        data=_build(DataSettings, "data", raw.get("data"), base_dir),
        universe=_build(UniverseSettings, "universe", raw.get("universe"), base_dir),
        prices=_build(PriceSettings, "prices", raw.get("prices"), base_dir),
        path=path,
        raw=raw,
        thresholds=_build(Thresholds, "thresholds", raw.get("thresholds"), base_dir),
        backtest=_build(BacktestSettings, "backtest", raw.get("backtest"), base_dir),
        jev=_build(JevSettings, "jev", raw.get("jev"), base_dir),
        strategies=_strategy_params(raw.get("strategies")),
    )


def save_thresholds(path: str | Path, thresholds: Thresholds, *, expected: Thresholds) -> None:
    """Validate and atomically replace thresholds, preserving unrelated YAML and comments.

    Refuse an outdated form rather than overwrite threshold edits from another session.
    The GUI edits a plain block mapping; aliases and flow mappings can still be read,
    but are deliberately not rewritten here.
    """
    path = Path(path)
    current = load_settings(path)
    if current.thresholds != expected:
        raise ConfigError("Thresholds changed on disk. Reload settings before saving your edits.")
    thresholds = Thresholds(**dataclasses.asdict(thresholds))
    source = path.read_text(encoding="utf-8")
    root = yaml.compose(source)
    values = dataclasses.asdict(thresholds)
    pair = next(((k, v) for k, v in root.value if k.value == "thresholds"), None) if root else None
    section = pair[1] if pair else None
    edits = []
    if section is None:
        updated = source.rstrip() + "\n\n" + yaml.safe_dump({"thresholds": values}, sort_keys=False)
    else:
        if (not isinstance(section, yaml.MappingNode) or section.flow_style
                or section.start_mark.index < pair[0].end_mark.index):
            raise ConfigError("To edit here, write thresholds as a plain YAML block mapping.")
        remaining = dict(values)
        indent = section.value[0][0].start_mark.column if section.value else 2
        for key, value in section.value:
            if key.value not in remaining:
                raise ConfigError("Remove duplicate keys from thresholds before editing here.")
            if not isinstance(value, yaml.ScalarNode) or value.start_mark.index < key.end_mark.index:
                raise ConfigError("Expand YAML aliases in thresholds before editing here.")
            replacement = str(remaining.pop(key.value))
            edits.append((value.start_mark.index, value.end_mark.index, replacement))
        if remaining:
            addition = "".join(" " * indent + f"{key}: {value}\n" for key, value in remaining.items())
            offset = section.end_mark.index
            if offset and source[offset - 1] != "\n":
                addition = "\n" + addition
            edits.append((offset, offset, addition))
        updated = source
        for start, end, replacement in sorted(edits, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
    # Check the exact document before touching the destination.
    check = yaml.safe_load(updated)
    if Thresholds(**check["thresholds"]) != thresholds:
        raise ConfigError("Could not safely update thresholds in this YAML document.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.resolve().parent, delete=False, suffix=".tmp") as handle:
            temporary = Path(handle.name)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_text(encoding="utf-8") != source:
            raise ConfigError("Config changed on disk. Reload settings before saving your edits.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_fmp_api_key(env_path: str | Path | None = None) -> str:
    """Return FMP_API_KEY from the environment, loading .env first.

    Values already set in the environment win over .env.
    """
    load_dotenv(env_path or DEFAULT_ENV_PATH, override=False)
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        raise ConfigError("The market-data API key is not set. Add it to the .env file (see README).")
    return key


def load_ai_gateway_api_key(env_path: str | Path | None = None) -> str:
    load_dotenv(env_path or DEFAULT_ENV_PATH, override=False)
    key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    if not key:
        raise ConfigError("AI_GATEWAY_API_KEY is not set. Add your Vercel AI Gateway key to .env.")
    return key
