"""Plain-language rules and assumptions for the research reports.

Every report opens with a "Rules and assumptions" section, so a reader who has not seen the scripts can tell
what each run did. The pieces several reports share live here; each report script adds what is specific to it
and builds its section with section().
"""

from __future__ import annotations

HEADING = "## Rules and assumptions"


def section(*parts: list[str]) -> list[str]:
    """The heading, then each part (a list of Markdown lines) followed by a blank line."""
    lines = [HEADING, ""]
    for part in parts:
        lines += [*part, ""]
    return lines


# The engine's ground rules (stratlib.sim.engine.Rules defaults) as a reader meets them.
GROUND_RULES = [
    "Portfolio rules, common to these backtests unless stated otherwise:",
    "",
    "- Each test runs three times from $100,000: in-sample 2016–2021, out-of-sample 2022 to the end of the data, "
    "and combined 2016 onward. The runs are separate, so the same year can differ between them. Positions still "
    "open at the end of a run are sold at its last close.",
    "- Rules may be chosen by looking at 2016–2021. The out-of-sample period runs once per test, with the rules "
    "frozen, as a check on the in-sample result.",
    "- The universe is US-listed stocks and ETFs, including delisted ones where the price data has them, on days "
    "when the as-traded close is at least $5 and the 20-day average dollar volume is at least $20M. The test is "
    "made on the signal day and counts that day's own volume.",
    "- Each trade risks 0.5% of equity, so shares = 0.5% × equity / (entry price − initial stop). A position is "
    "capped at 20% of equity and at the cash on hand. There is no margin.",
    "- At most 10 positions are open at once. When more stocks signal than there are free slots, the ones with the "
    "highest 63-session (three-month) return are bought first.",
    "- Slippage is 0.10% of the price on every buy and sell, 0.25% when the as-traded price is under $20. There are "
    "no commissions or taxes.",
    "- A stop fills at the stop price, or at the open when the price gaps through it. When a stop and a profit "
    "target fall on the same day, the stop is assumed to come first.",
    "- Trade returns are price only, without dividends. Idle cash earns nothing. Shares can be fractional.",
    "- The benchmark is SPY bought at the first close and held, with dividends reinvested.",
    "- Prices are the data provider's split-adjusted daily bars. Its delisted stocks are thin before 2021 (prices "
    "for only 28 stocks delisted in 2019 and 9 in 2020), so survivorship flatters the in-sample years more than "
    "the later ones.",
]

TERMS = [
    "Table terms:",
    "",
    "- R is the initial risk per share, entry price minus initial stop; expectancy is the mean R per trade.",
    "- Sharpe uses the 3-month T-bill yield as the risk-free rate.",
    "- *Time with a position* is the share of sessions with any open trade; *average invested* is the mean share of "
    "equity in trades.",
]

QULLAMAGGIE_SETUP = [
    "The Qullamaggie breakout is a daily-bar version of the swing-trading setup taught by the trader Kristjan "
    "Kullamägi (Qullamaggie). It buys a stock that made a big move, then paused in a tight range, as it breaks out "
    "of that range. A stock sets up at the close of session T when all four conditions hold:",
    "",
    "1. Prior move. Its highest close of the last 60 sessions is at least 30% above the lowest low of the 60 "
    "sessions before that high.",
    "2. Consolidation. That high was 10 to 40 sessions ago. Since then the price has not fallen more than 25% below "
    "it, and the range of the last 5 sessions, highest high to lowest low, is under 12% of the close.",
    "3. Trend. The close is above its 10- and 20-day simple moving averages (SMA), and the 20-day SMA is higher than "
    "5 sessions earlier.",
    "4. Volatility. The average daily range over 20 sessions, (high − low) / low, is at least 4%. This is the ADR.",
    "",
    "The pivot is the highest high since the consolidation's high. A buy-stop order sits at the pivot for the next "
    "5 sessions and fills at the pivot, or at the open if the stock gaps above it. A newer setup on the same stock "
    "replaces the order.",
    "",
    "The initial stop is the low of the session before entry. The trade is skipped when that stop is more than one "
    "ADR below the entry price. This is the one-ADR stop limit.",
    "",
    "Daily bars cannot show whether the entry day's low came before or after the breakout. On an up day (close at or "
    "above the open) the price is assumed to go open, low, high, close, so a low before the breakout does not stop "
    "the trade. On a down day, or after a gap above the pivot, any low below the stop does.",
]

# Exit rules by the codes the reports use (strategies/exit_replay.py).
EXITS = {
    "A5": "A5: keep only the initial stop and sell at the close of the 5th session after entry.",
    "A10": "A10: keep only the initial stop and sell at the close of the 10th session after entry.",
    "A20": "A20: keep only the initial stop and sell at the close of the 20th session after entry.",
    "A40": "A40: keep only the initial stop and sell at the close of the 40th session after entry.",
    "B10": "B10: keep the initial stop and sell at the first close below the 10-day SMA, the entry day included.",
    "B20": "B20: as B10 with the 20-day SMA.",
    "B50": "B50: as B10 with the 50-day SMA.",
    "C10": "C10: sell a third of the position at the close of the 3rd session after entry and move the stop on the "
           "rest up to the entry price (breakeven). From the next session, sell the rest at the first close below "
           "the 10-day SMA. This is the exit Qullamaggie describes.",
    "C20": "C20: as C10, with the rest sold at the first close below the 20-day SMA.",
    "D10": "D10: as C10, with the partial sale at the close of the 5th session instead of the 3rd.",
    "D20": "D20: as D10, with the rest sold at the first close below the 20-day SMA.",
    "E": "E: a limit order sells half at the entry price plus 2R. Then the stop moves to breakeven and the rest is "
         "sold at the first close below the 20-day SMA. Until the target fills, only the initial stop applies.",
    "F": "F: a chandelier stop, the highest high since entry minus 3 times the 14-day average true range (ATR) as of "
         "entry. It rises with each new high, never falls, and never sits below the initial stop.",
}


def exits(*codes: str) -> list[str]:
    return [f"- {EXITS[code]}" for code in codes]


EPISODIC_PIVOT = [
    "The episodic pivot buys a stock that gaps up on news, here an earnings release, on the idea that a real "
    "surprise keeps moving the price for weeks. A signal on session T needs:",
    "",
    "1. Gap. T opens at least the gap threshold (5%, 10%, 15%, 20% or 25%, as stated) above the previous close.",
    "2. Volume. T's volume is at least 3 times the average of the previous 50 sessions.",
    "3. Earnings. T is the first session after an earnings release. The release dates carry no time of day, so a "
    "release dated the session before (after the close), T itself (before the open), or a weekend or holiday just "
    "before T all count.",
    "4. Neglected, only where a test says so. The stock rose less than 20% over the 60 sessions before T, so the "
    "gap is not the end of a run that had already happened.",
    "",
    "Entry: buy at T's close, but only if the close is above the open and in the upper half of the day's range. "
    "Initial stop: T's low.",
]

EP_SIGNAL_DAY_LIQUIDITY = [
    "Liquidity is tested on the signal day itself, as in the portfolio rules below, so the gap day's own close and "
    "volume count toward the $5 and $20M floors. A later report (*Episodic pivot, after correcting liquidity and exits*) found that "
    "this let in stocks that were illiquid until the news, and moved the test to the sessions before the signal.",
]

EP_PRIOR_LIQUIDITY = [
    "Liquidity is tested before the signal day: the average dollar volume of the 20 sessions before T is at least "
    "$20M, and the previous as-traded close is at least $5. Stocks whose split adjustment varies more than 100-fold "
    "from 2016 on are left out, since their old prices are unreliable.",
]

SPY_OVERLAY = [
    "SPY overlay: all capital not in trades is held in SPY, with dividends reinvested. At each close SPY is sold to "
    "pay for that session's entries, and the cash from exits goes back into SPY, paying the normal slippage on SPY "
    "each way. Positions are sized on total equity, SPY included.",
]

MINERVINI = [
    "Mark Minervini's trend template with a volatility contraction pattern (VCP), on daily bars. The trend "
    "template holds at the close of session T when:",
    "",
    "1. The close is above the 50-, 150- and 200-day SMAs.",
    "2. The 50-day SMA is above the 150-day, and the 150-day is above the 200-day.",
    "3. The 200-day SMA is above its level 21 sessions earlier.",
    "4. The close is at least 30% above the 52-week low and within 25% of the 52-week high.",
    "5. Relative strength is in the top 30% of the day's liquid stocks and ETFs. The score weights the latest "
    "quarter's return 0.4 and each of the three quarters before it 0.2 (quarters of 63 sessions).",
    "",
    "The VCP is checked on template days. Swing highs and lows come from a zigzag that needs a 3% reversal to confirm "
    "a swing, so no later prices are used. A pullback runs from a swing high to the lowest low after it. A setup "
    "needs all of these:",
    "",
    "- At least two pullbacks starting in the last 60 sessions, each shallower than the one before.",
    "- The latest pullback no deeper than 10%.",
    "- Average volume since the latest swing high below the 50-session average (volume drying up).",
    "- No trade above the latest swing high since. That high is the pivot.",
    "",
    "Entry: a buy-stop at the pivot, live for 5 sessions after the last setup day, filled at the pivot or at the open "
    "on a gap. The trade is kept only if the breakout day's volume is at least 1.4 times the average of the previous "
    "50 sessions; otherwise it is sold at that day's close. Initial stop: the lowest low of the final pullback, but "
    "no more than 8% below the entry price.",
]

MINERVINI_EXIT_A = [
    "Exit (a): a limit order sells half at 20% above the entry price. Until it fills, only the initial stop applies. "
    "After it fills, the rest is sold at the first close below the 50-day SMA, with the initial stop still in place.",
]

MARKET_FILTERS = [
    "Market filters, each checked at the close of the signal day. While a filter is off no new trades are taken; "
    "open positions keep their exits.",
    "",
    "- A has no filter.",
    "- B requires SPY to close above its 50-day SMA.",
    "- C requires QQQ's 10-day SMA to be above its 20-day SMA.",
    "- D requires more than half of the day's liquid stocks to close above their own 50-day SMA. ETFs are left out "
    "of the count.",
    "- E requires new 52-week highs minus new 52-week lows across the liquid stocks, averaged over 10 days, to be "
    "above zero.",
    "- F requires both B and D.",
]

# The Traveling Trader's fundamentals checklist, shared by the screen and the portfolio tests.
CHECKLIST_SOURCE = [
    "The Traveling Trader is a YouTube investor. His fundamentals checklist, taken from his videos, asks for a "
    "stock whose forward PE is below its current PE, whose PE is below its own history, with a PEG of 1 or less, "
    "a return on invested capital (ROIC) of at least 15%, debt below equity and rising free cash flow.",
]

CHECKLIST_UNIVERSE = [
    "Universe and timing: on the first session of each calendar quarter, every stock outside Financial Services "
    "that passes the liquidity floor that day ($5 as-traded close, $20M of 20-day average dollar "
    "volume) and has 8 consecutive quarters of statements in one currency available before the day. A statement counts from its "
    "filing date, or 45 days after the quarter's end where the data gives the quarter's end as the filing date. "
    "Data more than 200 days old leaves the stock out.",
]

CHECKLIST_TRAILING = [
    "The hindsight-free checklist (QUAL) uses only data public at the rebalance. The data has no history of analyst "
    "estimates, so the forward PE test is dropped and the PEG uses past growth. A stock passes when all five hold:",
    "",
    "- PEHIST: trailing PE below the median of its own trailing PE at the previous 12 quarter-ends (at least 8 "
    "available). The PE uses the quarter-end market cap moved to the rebalance day by the price change, over "
    "trailing twelve-month (TTM) net income; negative earnings fail.",
    "- PEGT: trailing PE divided by TTM net-income growth on a year earlier, in percent, is 1 or less, with "
    "positive earnings and growth.",
    "- ROIC15: ROIC of at least 15% at the latest quarter. ROIC = TTM operating income after tax / (equity + "
    "total debt − cash and short-term investments).",
    "- DE1: total debt below equity, with positive equity.",
    "- FCFUP: TTM free cash flow above its level 4 quarters earlier, which is above its level 8 quarters earlier.",
]

CHECKLIST_PORTFOLIO = [
    "The portfolio: at the close of each quarter's first session, buy every passing stock not already held. The "
    "initial stop is 20% below the entry price and is never moved. At a later quarter's close, a holding that no "
    "longer passes, or can no longer be checked, is sold. Between rebalances only the stop, or a delisting, "
    "sells.",
]
