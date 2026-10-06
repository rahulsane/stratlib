"""Tax accounting for a taxable account: an accounting layer that never changes a strategy's trades.

Federal rules, simplified to what a backtest needs (the reporting standard of 2026-10-02):
- Lots: each purchase is a lot with its cost (fill price including slippage) and dates. Sales take lots first in,
  first out. A gain is long-term when the sale comes more than one year after the lot's holding period began.
- Netting: short- and long-term gains and losses net within the year; a net loss in one class offsets a net gain
  in the other; what is left carries forward with its class. No $3,000 deduction against other income (it depends
  on the reader's other income).
- Wash sales: a loss on shares bought again within 30 days (before or after the sale; shares from the same
  purchase do not count) is disallowed to the extent of the new shares, added to their cost, and their holding
  period starts when the sold shares' did. A loss already used in an earlier year's settlement is clawed back as a
  gain of the same class in the year of the repurchase.
- Dividends: taxed in the year of the ex-date; qualified (long-term rate) when the shares were held more than 60 days
  of the 121-day window around the ex-date, otherwise ordinary (short-term rate). Shares still held when a year is
  settled are assumed held to the window's end.
- Margin interest: deductible against the year's net short-term gain and ordinary dividends (income taxed at ordinary
  rates); the excess carries forward.
- Settlement: the tax for a year is due at the first session of the next year (settle). Sales tagged "end of test"
  (the final liquidation) are kept apart, so the last year can be settled with or without them (finish).
Rates are percentages; the defaults are the top federal rates with the 3.8% net investment income tax, no state tax.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

WASH_DAYS = 30
END = "end of test"


@dataclass(frozen=True)
class TaxRates:
    short_term: float = 40.8   # 37% ordinary + 3.8%
    long_term: float = 23.8    # 20% + 3.8%

    @property
    def zero(self) -> bool:
        return self.short_term == 0 and self.long_term == 0


NO_TAX = TaxRates(0.0, 0.0)


def anniversary(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:           # 29 February
        return d.replace(year=d.year + 1, day=28)


@dataclass
class Lot:
    shares: float
    cost: float            # total basis, including wash-sale additions
    start: date            # holding period start (moved back for wash-sale replacements)
    bought: date           # purchase date
    lot_id: int = 0


@dataclass
class Loss:
    """A realized loss that a repurchase within 30 days may still wash."""
    day: date
    shares: float          # shares sold at a loss not yet matched to a replacement
    per_share: float       # loss per share (positive)
    long: bool
    held_days: int
    lot_id: int
    year: int


@dataclass
class Dividend:
    key: object
    ex: date
    amount: float
    bought: date
    sold: date | None = None


@dataclass
class YearItems:
    st_gain: float = 0.0
    st_loss: float = 0.0
    lt_gain: float = 0.0
    lt_loss: float = 0.0
    interest: float = 0.0
    end_st: float = 0.0     # net short-term result of the "end of test" sales
    end_lt: float = 0.0


class TaxLedger:
    def __init__(self, rates: TaxRates = TaxRates()):
        self.rates = rates
        self.lots: dict[object, list[Lot]] = defaultdict(list)
        self.losses: dict[object, list[Loss]] = defaultdict(list)
        self.years: dict[int, YearItems] = defaultdict(YearItems)
        self.dividends: list[Dividend] = []
        self.carry_st = self.carry_lt = self.carry_interest = 0.0
        self.paid: dict[int, float] = {}
        self.settled: set[int] = set()
        self.stats = defaultdict(float)
        self._next_id = 0

    # ------------------------------------------------------------------ events

    def buy(self, key, day: date, shares: float, cost: float) -> None:
        self._next_id += 1
        lot = Lot(shares, cost, day, day, self._next_id)
        self._wash_after(key, lot)
        self.lots[key].append(lot)

    def sell(self, key, day: date, shares: float, proceeds: float, reason: str = "") -> None:
        """Sell `shares` (first in, first out) for total `proceeds`."""
        left, per_share = shares, proceeds / shares if shares > 0 else 0.0
        lots = self.lots[key]
        while left > 1e-12 and lots:
            lot = lots[0]
            take = min(left, lot.shares)
            basis = lot.cost * take / lot.shares
            gain = take * per_share - basis
            long = day > anniversary(lot.start)
            self._realize(day, gain, long, reason)
            if gain < 0 and reason != END:
                loss = Loss(day, take, -gain / take, long, (day - lot.start).days, lot.lot_id, day.year)
                self.losses[key].append(loss)
                self._wash_before(key, loss, lot)
            lot.cost -= basis
            lot.shares -= take
            left -= take
            if lot.shares <= 1e-12:
                lots.pop(0)
        if not lots:
            for d in self.dividends:
                if d.key == key and d.sold is None:
                    d.sold = day

    def dividend(self, key, ex: date, per_share: float) -> float:
        """Cash from a dividend on the shares held at the previous close; recorded per lot for the qualified test."""
        total = 0.0
        for lot in self.lots.get(key, []):
            amount = lot.shares * per_share
            if amount:
                self.dividends.append(Dividend(key, ex, amount, lot.bought))
                total += amount
        self.stats["dividends"] += total
        return total

    def interest(self, day: date, amount: float) -> None:
        self.years[day.year].interest += amount
        self.stats["interest"] += amount

    # ------------------------------------------------------------------ wash sales

    def _wash(self, loss: Loss, lot: Lot, shares: float) -> None:
        """Disallow the loss on `shares` sold, moving it into `lot` (the replacement)."""
        amount = shares * loss.per_share
        lot.cost += amount
        lot.start = min(lot.start, lot.bought - timedelta(days=loss.held_days))
        loss.shares -= shares
        if loss.year in self.settled:      # already used: claw it back now as a gain of the same class
            self._realize(lot.bought, amount, loss.long, "wash clawback")
        else:
            items = self.years[loss.year]
            if loss.long:
                items.lt_loss -= amount
            else:
                items.st_loss -= amount
        self.stats["wash_sales"] += 1
        self.stats["wash_disallowed"] += amount

    def _wash_after(self, key, lot: Lot) -> None:
        left = lot.shares
        for loss in self.losses.get(key, []):
            if left <= 1e-12:
                break
            if loss.shares > 1e-12 and 0 <= (lot.bought - loss.day).days <= WASH_DAYS:
                take = min(left, loss.shares)
                self._wash(loss, lot, take)
                left -= take
        self.losses[key] = [x for x in self.losses.get(key, []) if x.shares > 1e-12
                            and (lot.bought - x.day).days <= WASH_DAYS]

    def _wash_before(self, key, loss: Loss, sold: Lot) -> None:
        for lot in self.lots.get(key, []):
            if lot.lot_id == sold.lot_id or loss.shares <= 1e-12:
                continue
            if 0 < (loss.day - lot.bought).days <= WASH_DAYS:
                self._wash(loss, lot, min(loss.shares, lot.shares))

    # ------------------------------------------------------------------ settlement

    def _realize(self, day: date, gain: float, long: bool, reason: str) -> None:
        items = self.years[day.year]
        if reason == END:
            if long:
                items.end_lt += gain
            else:
                items.end_st += gain
            return
        if long:
            if gain >= 0:
                items.lt_gain += gain
            else:
                items.lt_loss -= gain
        else:
            if gain >= 0:
                items.st_gain += gain
            else:
                items.st_loss -= gain
        self.stats["realized_st_gains" if not long and gain > 0 else
                   "realized_lt_gains" if long and gain > 0 else
                   "realized_st_losses" if not long else "realized_lt_losses"] += abs(gain)

    def _dividend_split(self, year: int, settle_day: date) -> tuple[float, float]:
        qualified = ordinary = 0.0
        for d in self.dividends:
            if d.ex.year != year:
                continue
            start, stop = d.ex - timedelta(days=60), d.ex + timedelta(days=60)
            end = stop if d.sold is None or d.sold > settle_day else min(d.sold, stop)
            held = (end - max(d.bought, start)).days
            if held > 60:
                qualified += d.amount
            else:
                ordinary += d.amount
        return qualified, ordinary

    def _tax(self, items: YearItems, qualified: float, ordinary: float, carry: tuple, with_end: bool) -> tuple:
        carry_st, carry_lt, carry_int = carry
        st = items.st_gain - items.st_loss - carry_st + (items.end_st if with_end else 0.0)
        lt = items.lt_gain - items.lt_loss - carry_lt + (items.end_lt if with_end else 0.0)
        new_st = new_lt = 0.0
        if st < 0 and lt < 0:
            new_st, new_lt, st, lt = -st, -lt, 0.0, 0.0
        elif st < 0 <= lt:
            lt += st
            st = 0.0
            if lt < 0:
                new_st, lt = -lt, 0.0
        elif lt < 0 <= st:
            st += lt
            lt = 0.0
            if st < 0:
                new_lt, st = -st, 0.0
        interest = items.interest + carry_int
        use = min(interest, st + ordinary)
        from_st = min(use, st)
        st -= from_st
        ordinary_left = ordinary - (use - from_st)
        tax = (st * self.rates.short_term + lt * self.rates.long_term + qualified * self.rates.long_term
               + ordinary_left * self.rates.short_term) / 100
        return tax, (new_st, new_lt, interest - use), {"st": st, "lt": lt, "qualified": qualified,
                                                       "ordinary": ordinary_left, "interest_used": use}

    def settle(self, year: int, settle_day: date) -> float:
        """The tax for `year` (excluding any "end of test" sales), carrying losses and interest forward."""
        if year in self.settled:
            return 0.0
        q, o = self._dividend_split(year, settle_day)
        tax, carry, detail = self._tax(self.years[year], q, o,
                                       (self.carry_st, self.carry_lt, self.carry_interest), with_end=False)
        self.carry_st, self.carry_lt, self.carry_interest = carry
        self.settled.add(year)
        self.paid[year] = tax
        for k, v in detail.items():
            self.stats[f"taxed_{k}"] += v
        return tax

    def finish(self, year: int, day: date) -> dict:
        """The last (partial) year's tax with and without the final liquidation, plus carryforwards left."""
        q, o = self._dividend_split(year, day)
        carry = (self.carry_st, self.carry_lt, self.carry_interest)
        held, carry_held, _ = self._tax(self.years[year], q, o, carry, with_end=False)
        sold, carry_sold, _ = self._tax(self.years[year], q, o, carry, with_end=True)
        return {"tax_held": held, "tax_sold": sold, "carry_held": carry_held, "carry_sold": carry_sold}


class Accountant:
    """The engine's accountant (stratlib.sim.engine.simulate): panel columns and sessions mapped onto a TaxLedger.

    dividends: {session: [(column, dividend per share on the panel's split-adjusted basis), ...]} by ex-date.
    With NO_TAX the ledger still credits dividends, which gives the before-tax result with dividends."""

    def __init__(self, panel, rates: TaxRates = TaxRates(), dividends: dict | None = None):
        self.ledger = TaxLedger(rates)
        self.days = [date.fromisoformat(str(d)) for d in panel.dates]
        self.divs = dividends or {}

    def on_buy(self, j, t, shares, fill):
        self.ledger.buy(j, self.days[t], shares, shares * fill)

    def on_sell(self, j, t, shares, fill, reason):
        if shares > 0:
            self.ledger.sell(j, self.days[t], shares, shares * fill, reason)

    def dividends(self, t, holdings: dict) -> float:
        cash = 0.0
        for j, per_share in self.divs.get(t, ()):
            if j in holdings:
                cash += self.ledger.dividend(j, self.days[t], per_share)
        return cash

    def tax_due(self, t) -> float:
        return self.ledger.settle(self.days[t].year - 1, self.days[t])

    def finish(self, t) -> dict:
        out = self.ledger.finish(self.days[t].year, self.days[t])
        out.update(paid=dict(self.ledger.paid), stats=dict(self.ledger.stats))
        return out
