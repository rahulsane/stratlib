"""Tax ledger rules on worked examples. Run: .venv\\Scripts\\python -m pytest research\\test_tax_accounting.py -q"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tax_accounting import END, TaxLedger, TaxRates  # noqa: E402

R = TaxRates(40.0, 20.0)   # round rates for hand-checked answers


def test_short_and_long_term_rates():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 10, 1000)
    t.sell("A", date(2020, 6, 1), 10, 1500)        # +500 short-term
    t.buy("B", date(2019, 1, 2), 10, 1000)
    t.sell("B", date(2020, 6, 1), 10, 2000)        # +1000 long-term
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(500 * 0.4 + 1000 * 0.2)


def test_one_year_and_a_day_is_long_term():
    t = TaxLedger(R)
    t.buy("A", date(2020, 3, 2), 1, 100)
    t.sell("A", date(2021, 3, 2), 1, 200)          # exactly one year: still short-term
    t.buy("B", date(2020, 3, 2), 1, 100)
    t.sell("B", date(2021, 3, 3), 1, 200)          # one year and a day: long-term
    assert t.settle(2021, date(2022, 1, 3)) == pytest.approx(100 * 0.4 + 100 * 0.2)


def test_netting_and_carryforward_keep_their_class():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 1, 1000)
    t.sell("A", date(2020, 3, 2), 1, 400)          # -600 short-term
    t.buy("B", date(2018, 1, 2), 1, 100)
    t.sell("B", date(2020, 3, 2), 1, 300)          # +200 long-term
    assert t.settle(2020, date(2021, 1, 4)) == 0   # net -400, carried as short-term
    assert t.carry_st == pytest.approx(400)
    t.buy("C", date(2021, 1, 4), 1, 100)
    t.sell("C", date(2021, 2, 1), 1, 1100)         # +1000 short-term, 400 of it offset
    assert t.settle(2021, date(2022, 1, 3)) == pytest.approx(600 * 0.4)
    assert t.carry_st == 0


def test_wash_sale_moves_the_loss_into_the_new_shares():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 10, 1000)
    t.sell("A", date(2020, 3, 2), 10, 700)         # -300
    t.buy("A", date(2020, 3, 20), 10, 800)         # within 30 days: loss washed, basis 1100
    t.sell("A", date(2020, 6, 1), 10, 1200)        # gain 100, not 400
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(100 * 0.4)
    assert t.stats["wash_sales"] == 1 and t.stats["wash_disallowed"] == pytest.approx(300)


def test_repurchase_after_31_days_is_not_a_wash():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 10, 1000)
    t.sell("A", date(2020, 3, 2), 10, 700)
    t.buy("A", date(2020, 4, 2), 10, 800)          # 31 days later
    t.sell("A", date(2020, 6, 1), 10, 1200)        # gain 400, loss 300 stands
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(100 * 0.4)
    assert t.stats["wash_sales"] == 0


def test_wash_across_a_settled_year_is_clawed_back():
    t = TaxLedger(R)
    t.buy("B", date(2020, 1, 2), 1, 500)
    t.sell("B", date(2020, 6, 1), 1, 800)          # +300 short-term
    t.buy("A", date(2020, 11, 2), 10, 1000)
    t.sell("A", date(2020, 12, 21), 10, 800)       # -200, used in 2020
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(100 * 0.4)
    t.buy("A", date(2021, 1, 8), 10, 800)          # repurchase: the 2020 loss is clawed back in 2021
    assert t.settle(2021, date(2022, 1, 3)) == pytest.approx(200 * 0.4)


def test_wash_tacks_the_holding_period():
    t = TaxLedger(R)
    t.buy("A", date(2019, 6, 3), 10, 1000)
    t.sell("A", date(2020, 3, 2), 10, 900)         # -100 after 273 days
    t.buy("A", date(2020, 3, 10), 10, 900)         # basis 1000, holding period from 2019-06-11
    t.sell("A", date(2020, 6, 15), 10, 1500)       # +500, long-term thanks to the tacked period
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(500 * 0.2)


def test_qualified_and_ordinary_dividends():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 100, 1000)
    assert t.dividend("A", date(2020, 6, 1), 1.0) == pytest.approx(100)   # held long: qualified
    t.buy("B", date(2020, 5, 25), 100, 1000)
    t.dividend("B", date(2020, 6, 1), 1.0)                                # sold 20 days later: ordinary
    t.sell("B", date(2020, 6, 15), 100, 1000)
    assert t.settle(2020, date(2021, 1, 4)) == pytest.approx(100 * 0.2 + 100 * 0.4)


def test_margin_interest_offsets_ordinary_income_and_carries_forward():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 1, 1000)
    t.sell("A", date(2020, 3, 2), 1, 1300)         # +300 short-term
    t.interest(date(2020, 12, 1), 500)             # 300 used, 200 carried
    assert t.settle(2020, date(2021, 1, 4)) == 0
    assert t.carry_interest == pytest.approx(200)


def test_finish_with_and_without_the_final_sale():
    t = TaxLedger(R)
    t.buy("A", date(2020, 1, 2), 1, 1000)
    t.sell("A", date(2020, 3, 2), 1, 1100, END)
    out = t.finish(2020, date(2020, 12, 31))
    assert out["tax_held"] == 0 and out["tax_sold"] == pytest.approx(100 * 0.4)


# ---------------------------------------------------------------------- the engine's accountant hook

import numpy as np  # noqa: E402
from datetime import timedelta  # noqa: E402

from engine import Rules, simulate  # noqa: E402
from panel import Panel  # noqa: E402
from tax_accounting import NO_TAX, Accountant  # noqa: E402
from test_engine import Scripted  # noqa: E402


def year_end_panel(jump_at=20, price=100.0):
    """Business days from 2016-11-01 into 2017; S0 jumps to 150 at `jump_at`; S1 is flat at 100. Stocks become
    eligible after 15 sessions of volume (the liquidity rule)."""
    days, d = [], date(2016, 11, 1)
    while len(days) < 70:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    n = len(days)
    close = np.full((n, 2), price)
    close[jump_at:, 0] = 150.0
    return Panel(dates=np.array(days), symbols=np.array(["S0", "S1"]), kind=np.array(["stock", "stock"]),
                 until=np.array(["", ""]), open=close.copy(), high=close * 1.01, low=close * 0.99, close=close,
                 volume=np.full((n, 2), 1e6), factor=np.ones((n, 2)))


def run(panel, strategy, rules, accountant=None):
    strategy.setup(panel, {})
    return simulate(panel, strategy, str(panel.dates[2]), None, rules, accountant=accountant)


def test_a_zero_rate_ledger_without_dividends_changes_nothing():
    rules = Rules(risk_pct=5.0, max_position_pct=100.0)
    p = year_end_panel()
    plain = run(p, Scripted({16: [0], 26: [1]}, stop_pct=5, exit_at={0: 24}), rules)
    kept = run(p, Scripted({16: [0], 26: [1]}, stop_pct=5, exit_at={0: 24}), rules, Accountant(p, NO_TAX))
    assert np.array_equal(plain["equity"], kept["equity"])
    assert [t.pnl for t in plain["trades"]] == [t.pnl for t in kept["trades"]]


def test_dividends_go_to_shares_held_at_the_previous_close():
    rules = Rules(risk_pct=5.0, max_position_pct=100.0)
    p = year_end_panel(jump_at=99)
    ex = 20
    acc = Accountant(p, NO_TAX, {ex: [(1, 2.0)], 15: [(1, 9.0)]})  # the session-15 dividend predates the purchase
    r = run(p, Scripted({16: [1]}, stop_pct=5), rules, acc)
    shares = r["trades"][0].shares
    assert r["equity"][ex - 2] - r["equity"][ex - 3] == pytest.approx(2.0 * shares)
    assert acc.ledger.stats["dividends"] == pytest.approx(2.0 * shares)


def test_the_years_tax_is_paid_at_the_first_session_cutting_positions_when_cash_is_short():
    rules = Rules(risk_pct=5.0, max_position_pct=100.0, slippage_pct=0.0, slippage_low_price_pct=0.0)
    p = year_end_panel()
    first_2017 = next(i for i, d in enumerate(p.dates) if str(d) >= "2017")
    acc = Accountant(p, TaxRates(40.0, 20.0))
    # S0 bought at 100 (session 16), sold at 150 (session 24): +50 a share, short-term. All the proceeds go into
    # S1 at session 26, so the tax due in January has to come from selling part of S1.
    r = run(p, Scripted({16: [0], 26: [1]}, stop_pct=5, exit_at={0: 24}), rules, acc)
    s0, s1 = r["trades"]
    tax = 0.4 * s0.pnl
    assert r["taxes_paid"] == [(str(p.dates[first_2017]), pytest.approx(tax))]
    assert s1.partial_date == str(p.dates[first_2017])
    assert s1.partial_exit_price == pytest.approx(100.0)
    assert r["counts"]["tax_sales"] == 1
    end_value = 100_000 + s0.pnl - tax
    assert r["equity"][-1] == pytest.approx(end_value)
