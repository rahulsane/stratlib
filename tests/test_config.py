import pytest

from stratlib.cli import main
from stratlib.config import DEFAULT_CONFIG_PATH, ConfigError, load_fmp_api_key, load_settings


def test_project_config_loads():
    settings = load_settings(DEFAULT_CONFIG_PATH)
    assert settings.fmp.calls_per_minute <= 750
    assert settings.universe.exchanges == ("NYSE", "NASDAQ", "AMEX")
    assert settings.data.db_path.is_absolute()


def test_relative_paths_resolve_from_config_dir(settings, tmp_path):
    assert settings.data.db_path == tmp_path / "data" / "stratlib.db"
    assert settings.data.log_dir == tmp_path / "logs"


def test_unknown_setting_is_rejected(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("fmp:\n  calls_per_minut: 100\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="calls_per_minut"):
        load_settings(config)


def test_missing_api_key_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="API key is not set"):
        load_fmp_api_key(tmp_path / "missing.env")


def test_status_runs_without_an_api_key(monkeypatch, settings, capsys):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setattr("stratlib.cli.configure_logging", lambda *args, **kwargs: None)
    assert main(["--config", str(settings.path), "status"]) == 0
    assert '"symbols_with_prices": 0' in capsys.readouterr().out


@pytest.mark.parametrize("line", ["rs_min: 100", "min_price: -1", "rs_quarter_sessions: 0",
                                  "volume_sessions: 2.5", "min_price: .nan", "min_price: true"])
def test_invalid_thresholds_are_rejected(tmp_path, line):
    config = tmp_path / "config.yaml"
    config.write_text("thresholds:\n  " + line + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(config)


@pytest.mark.parametrize("line",["distribution_pressure_count: 5", "follow_through_min_day: 1",
                                  "cup_min_weeks: 0", "cup_max_depth_pct: 51", "handle_max_weeks: 0",
                                  "flat_max_depth_pct: 100", "handle_volume_max_ratio: 1.1",
                                  "base_max_weeks: 3", "base_recent_weeks: 1.5"])
def test_invalid_phase3_thresholds_are_rejected(tmp_path,line):
    config=tmp_path/'config.yaml'
    config.write_text('thresholds:\n  '+line+'\n',encoding='utf-8')
    with pytest.raises(ConfigError):
        load_settings(config)


@pytest.mark.parametrize("line", ["stop_loss_pct: 0", "stop_loss_pct: 100", "profit_target_pct: 0",
                                  "fast_gain_pct: 0", "fast_gain_weeks: 0", "fast_gain_weeks: 8",
                                  "minimum_hold_weeks: 3", "minimum_hold_weeks: 8.5"])
def test_invalid_sell_thresholds_are_rejected(tmp_path, line):
    path = tmp_path / "config.yaml"
    path.write_text("thresholds:\n  " + line + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_save_thresholds_preserves_comments_paths_and_unrelated_sections(settings):
    from dataclasses import replace
    from stratlib.config import save_thresholds
    source = settings.path.read_text(encoding="utf-8")
    source += "\n# Keep this comment\nthresholds:\n  stop_loss_pct: 7 # Keep this too\n\nfuture:\n  enabled: false\n"
    settings.path.write_text(source, encoding="utf-8")
    save_thresholds(settings.path, replace(settings.thresholds, stop_loss_pct=8), expected=settings.thresholds)
    updated = load_settings(settings.path)
    assert updated.thresholds.stop_loss_pct == 8
    assert updated.raw["future"] == {"enabled": False}
    assert updated.raw["data"]["db_path"] == "data/stratlib.db"
    assert "# Keep this comment" in settings.path.read_text(encoding="utf-8")
    assert "# Keep this too" in settings.path.read_text(encoding="utf-8")


def test_save_adds_defaults_to_older_config_and_refuses_stale_form(settings):
    from dataclasses import replace
    from stratlib.config import save_thresholds
    changed = replace(settings.thresholds, stop_loss_pct=8)
    save_thresholds(settings.path, changed, expected=settings.thresholds)
    assert load_settings(settings.path).thresholds == changed
    with pytest.raises(ConfigError, match="changed on disk"):
        save_thresholds(settings.path, settings.thresholds, expected=settings.thresholds)
    assert load_settings(settings.path).thresholds == changed


def test_failed_atomic_replace_keeps_original_file(settings, monkeypatch):
    from stratlib.config import save_thresholds
    original = settings.path.read_bytes()
    def fail(*args):
        raise OSError("disk write failed")
    monkeypatch.setattr("stratlib.config.os.replace", fail)
    with pytest.raises(OSError):
        save_thresholds(settings.path, settings.thresholds, expected=settings.thresholds)
    assert settings.path.read_bytes() == original
    assert not list(settings.path.parent.glob("*.tmp"))
