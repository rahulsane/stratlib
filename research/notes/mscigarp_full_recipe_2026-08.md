# The August 2026 review rebuilt with MSCI's inputs (2026-10-02)

A one-off test, not part of the app. Question: how close does the rebuild of the MSCI USA Quality GARP Select Index
get to what the index really holds, once it uses the parent universe, free-float weights, sectors and analysts'
forecasts MSCI uses? Script: `research/mscigarp_full.py`. Numbers: `mscigarp_full_recipe_2026-08.json` beside this note.

## Answer

| Version | Parent | Holdings | Names shared with the fund (130) | Weight overlap |
|---|---|---|---|---|
| The app's rebuild (S&P 500, trailing data only) | 499 | 94 | 61 | 66% |
| MSCI USA parent, free-float caps, GICS sectors | 514 | 114 | 93 | 71% |
| + analysts' forecasts, long-term growth from FY1 to FY3 | 514 | 138 | 118 | 76% |
| + analysts' forecasts, long-term growth from FY0 to FY3 | 514 | 138 | 120 | 82% |
| Same, with MSCI USA as it stood before the August changes | 503 | 133 | 117 | 85% |

Weight overlap is the sum, over stocks, of the smaller of the two weights (100% would be identical). The fund's
holdings are those of 30 September 2026. Our weights are the review's, moved with prices from the 18 August close to the
same day.

- **The parent universe** (MSCI USA, with free float and GICS) adds 5 points and 32 shared names, mostly mid caps
  outside the S&P 500, such as MercadoLibre, and both Alphabet classes.
- **The forecasts** add 5 to 11 points and bring in the large holdings the trailing data missed: Caterpillar,
  Applied Materials, Intel, AMD, Oracle, Palo Alto, Dell. Caterpillar and Intel show why: Caterpillar's consensus
  rises from $18.67 for 2025 to $27.25 for 2026, and Intel's from $0.34 to $1.54, while their reported earnings had
  been falling.
- **The stand-in for MSCI's long-term growth matters.**
  - Growth from FY1 to FY3 breaks for Alphabet. FMP's 2026 consensus is $20.61, against $15.15 for 2027, probably
    because 2026 includes a one-time gain. That makes Alphabet's long-term growth negative, and both share classes
    drop out.
  - Growth from the last reported year (FY0) to FY3 keeps Alphabet.
- **SanDisk** was not in MSCI USA on 1 June. It entered at the August review, and the fund does not hold it. With
  the parent as it stood before August, it drops out. That version also loses a few small August additions the
  fund does hold: SpaceX, Carpenter Technology and ATI, together 0.3%.

## What is still different (the best version, 85%)

- **Names (13 of the fund's 130 missing, 16 extra), all small.**
  - Missing: Tapestry 0.53%, Steel Dynamics, Welltower, Flex, Jacobs, ON Semiconductor, ResMed, United
    Therapeutics, TransDigm, and the August additions SpaceX, Carpenter Technology and ATI.
  - Extra: Marathon Petroleum 1.4%, NetApp, Interactive Brokers, Phillips 66, Valero, Texas Pacific Land.
- **Weights.** Most of the remaining gap is weights, not names.
  - Lilly 3.9% against 1.5%, GE Vernova 3.1% against 0.2%, Netflix 2.6% against 0.5%.
  - The other way: Caterpillar 1.6% against 3.5%, and Palantir 1.8% against 2.6%.
  - The tilt multiplies a stock's weight by anything from 0.25 to 7, according to its value and quality rank
    within its sector. One step in a rank moves a weight a lot. These ranks depend on MSCI's own fundamental data
    (continuing-operations EPS, fiscal-year figures, five years of history), which FMP approximates.
- **Likely sources of the rest:**
  - FMP's consensus estimates as of 2 October rather than I/B/E/S's as of 31 July.
  - The long-term growth stand-in.
  - FMP's fundamentals in place of MSCI's.
  - The refiners (Marathon, Phillips 66, Valero) may be cases of estimates that moved after the data date; that
    is not checked.

## How it was built

- **Parent.** The holdings of the iShares MSCI USA Equal Weighted ETF (EUSA) on 30 September 2026, which hold the
  MSCI USA members, with GICS sectors. Every share class is a separate security, as MSCI treats them.
- **Free float.**
  - FMP's float shares (`shares-float-all`) × the 31 July close.
  - Dual-class companies where FMP gives one float figure for both classes are split by MSCI's own weights.
  - Against MSCI's free-float weights for MSCI USA on 1 June (its constituents tool, 503 names matched):
    correlation 0.996, and 80% of the ratios fall within 0.84–1.22. Prices moved between June and July, which
    accounts for part of the spread.
- **Forecasts.** FMP's consensus EPS by fiscal year (`analyst-estimates`), with MSCI's formulas:
  - EPS12F = (M·EPS1 + (12 − M)·EPS2)/12 and EPS12B = (M·EPS0 + (12 − M)·EPS1)/12, where M is the months left in the
    current fiscal year on 31 July.
  - Short-term growth = (EPS12F − EPS12B)/|EPS12B|.
  - Forward P/E = price/EPS12F.
  - EPS0 is FMP's consensus row for the last reported year, so the base and the estimates share one basis.
  - MSCI's long-term growth is I/B/E/S's 3–5 year consensus rate, which FMP does not have. Two stand-ins were
    tried: FY1 to FY3, and FY0 to FY3. Both need positive ends and at least three analysts for FY3.
  - Growth score = (2·long-term + short-term + internal growth + EPS trend + sales trend)/6.
- **Current members for the buffer.** MSCI's constituents tool, which lists the index on 1 June 2026, after the May
  review: 131 names, 128 matched to tickers. The other three, Lululemon, IonQ and Insulet, are no longer in MSCI USA.
- **Weights.** Market cap × tilt, capped by MSCI's procedure. The 5% cap applies per issuer (both Alphabet
  classes together). Sectors are GICS.
- **FMP calls:** 876 (float list 10, estimates 514, statements for 70 members missing from the cache 350, prices 2).
  The iShares and MSCI files are free downloads, cached in `research/cache/mscigarp_full/`.

## Caveats

- **This compares one review on one day, with forecasts from two months later.** It shows the recipe is right,
  not that it would have tracked the index in the past.
- **No history.** FMP keeps only today's estimates, so earlier reviews cannot be rebuilt this way. MSCI's
  constituent history and point-in-time I/B/E/S forecasts are licensed data.
- **EUSA's holdings are dated after the review.** So "before the August changes" is approximate: it uses MSCI's 1
  June list, and the stocks MSCI USA deleted in August cannot be priced back in.
- **The fund is not exactly the index.** It holds a little cash and a futures position, and these are left out.
  The fund's own trading can differ slightly from the index's weights.
