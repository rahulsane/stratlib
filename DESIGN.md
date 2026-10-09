---
name: StratLib
description: A calm, TradingView-grade dark workspace for after-close stock screening, where the data carries the colour.
colors:
  slate-bg: "#131722"
  slate-panel: "#1e222d"
  slate-raised: "#262a35"
  hairline: "#2a2e39"
  hairline-strong: "#363a45"
  border-hover: "#434651"
  text: "#d1d4dc"
  text-bright: "#f0f3fa"
  text-muted: "#9598a1"
  faint: "#50535e"
  action-blue: "#2962ff"
  action-blue-hover: "#1e53e5"
  action-blue-text: "#5b8cff"
  action-blue-tint: "rgba(41, 98, 255, 0.16)"
  up-green: "#26a69a"
  up-green-text: "#3cc4b5"
  up-green-tint: "rgba(38, 166, 154, 0.15)"
  down-red: "#ef5350"
  down-red-text: "#ff7f7c"
  down-red-tint: "rgba(239, 83, 80, 0.14)"
  caution-amber: "#ff9800"
  caution-amber-tint: "rgba(255, 152, 0, 0.14)"
  ma50-steel: "#a3abbd"
  ma200-brass: "#c9a26e"
  chart-gridline: "#232733"
typography:
  price:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 600
    letterSpacing: "-0.02em"
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  headline:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  title:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  body:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  body-dense:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  label:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
  micro:
    fontFamily: "Inter, -apple-system, Segoe UI, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    fontFeature: "\"tnum\" 1, \"cv11\" 1"
rounded:
  hair: "2px"
  badge: "3px"
  tag: "4px"
  md: "6px"
  sheet: "12px"
  pill: "13px"
spacing:
  xxs: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.action-blue}"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0 14px"
    height: "32px"
    typography: "{typography.body-dense}"
  button-primary-hover:
    backgroundColor: "{colors.action-blue-hover}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "0 14px"
    height: "32px"
  button-secondary-hover:
    backgroundColor: "{colors.slate-raised}"
  card:
    backgroundColor: "{colors.slate-panel}"
    rounded: "{rounded.md}"
  field:
    backgroundColor: "{colors.slate-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "32px"
  market-chip-caution:
    backgroundColor: "{colors.caution-amber-tint}"
    textColor: "{colors.caution-amber}"
    rounded: "{rounded.pill}"
    padding: "0 10px"
    height: "26px"
  results-row:
    height: "38px"
    padding: "0 12px"
    typography: "{typography.body-dense}"
  results-row-selected:
    backgroundColor: "{colors.action-blue-tint}"
  letter-badge-pass:
    backgroundColor: "{colors.up-green-tint}"
    textColor: "{colors.up-green-text}"
    rounded: "{rounded.badge}"
    size: "19px"
  letter-badge-fail:
    backgroundColor: "{colors.down-red-tint}"
    textColor: "{colors.down-red-text}"
    rounded: "{rounded.badge}"
    size: "19px"
  verdict-chip-pass:
    backgroundColor: "{colors.up-green-tint}"
    textColor: "{colors.up-green-text}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "28px"
  check-pill-pass:
    backgroundColor: "{colors.up-green-tint}"
    textColor: "{colors.up-green-text}"
    rounded: "10px"
    padding: "1px 8px"
---

# Design System: StratLib

Scope: StratLib's GUI, the NiceGUI web app in `src/stratlib/web/` (`stratlib web`). Its stylesheet is `src/stratlib/web/assets/theme.css`. It replaced the Streamlit interface and its broadsheet system on 2026-10-01.

## Overview

**Creative North Star: "The Quiet Trading Desk"**

The category standard, played straight: a TradingView dark-slate workspace for one owner researching US stocks after the close, desktop first. Surfaces are three close slates separated by 1px hairlines; colour lives in the data (prices, changes, letter marks, candles), never in filled blocks. Density is high and calm: 38px rows, 12 to 13px labels, tabular figures everywhere.

The owner rejected two looks, and both refusals bind this system: the broadsheet (too plain, old-fashioned) and the Metro build (giant headline, solid colour tiles, neon saturation). Convention is the commitment; there is no smuggled quirk.

**Key Characteristics:**
- Dark first, three-step tonal layering (bg, panel, raised) with hairline borders.
- One blue for action and selection; green/red for up/down and pass/fail; amber for market caution.
- Semantic colour appears as text, strokes or a 14 to 16% tint, never as a solid fill behind text (except the chart's last-close tag). Green and red have a fill shade for marks and a lighter text shade for words.
- Inter with tabular numbers; the largest type on screen is the 30px price.
- Chart devices copied from the category leader: OHLC legend, last-close tag, pivot chip, month labels.

## Colors

A low-chroma blue-grey slate with four signal hues held at TradingView's calm saturation.

### Primary
- **Action Blue** (action-blue): the primary button, focus rings (2px outline, 1px offset), the active nav tab and current funnel step underline (2px), the focused field border. Its text-safe sibling **Action Blue Text** (action-blue-text) marks the active option in menus and the caret. **Selection Tint** (action-blue-tint) is the selected results row and `::selection`.

### Secondary (signal hues)
Green and red each come in two shades. The fill shade is for marks; the text shade is for words and figures.
- **Up Green** (up-green): fill shade for rising sparklines, up candles and volume bars, the meter fill in an uptrend, the last-close line and tag when up, the brand mark's breakout dot.
- **Up Green Text** (up-green-text): every green word or figure: price rises, the 6M change, pass letter badges (pass and partial), pass verdicts and pills, the uptrend market state and chip, the OHLC legend on an up bar.
- **Down Red** (down-red): fill shade for falling sparklines, down candles and volume bars, the meter fill in a correction, the last-close line and tag when down.
- **Down Red Text** (down-red-text): every red word or figure: price falls, fail letter badges, fail verdicts and pills, the correction state and chip, the OHLC legend on a down bar.
- **Caution Amber** (caution-amber): the uptrend-under-pressure market state and chip, and its meter fill. One shade serves both roles; it clears 6.5:1 on its tint.
- Each signal hue has a tint (up-green-tint, down-red-tint, caution-amber-tint), built from the fill shade, used behind chips, badges, pills and verdicts. The partial badge's outline is the fill green at 60%.

### Tertiary (chart only)
- **MA 50 Steel** (ma50-steel) and **MA 200 Brass** (ma200-brass): the two moving-average lines (1.2px) and their legend text. Deliberately desaturated so they never read as up/down.
- **Chart Gridline** (chart-gridline): horizontal split lines, a step darker than the hairline.

### Neutral
- **Slate Background** (slate-bg): page and sticky top bar.
- **Slate Panel** (slate-panel): cards, fields, menus, toasts, sticky table header.
- **Slate Raised** (slate-raised): hover on rows, buttons, menu items, funnel steps; the current chart range; letter headings; neutral verdict and pill fills.
- **Hairline** (hairline) for card borders and dividers; row dividers use it at 60% alpha. **Hairline Strong** (hairline-strong) for button, field, tag and kbd borders, empty letter badges, the sheet grabber, the pivot chip. **Border Hover** (border-hover) on hovered buttons and fields.
- **Text** (text) body; **Text Bright** (text-bright) symbols, headings, counts, the price; **Text Muted** (text-muted) labels, meta, company names, axes, and N/A pills on slate-raised; it clears 4.5:1 on every surface it meets, including slate-raised and the selected-row tint; **Faint** (faint) is non-text only: funnel chevrons and the chart crosshair.

### Named Rules
**The Data Carries the Colour Rule.** Green, red and amber appear only as text, a stroke, or a light tint. No solid colour tile, no filled card, no coloured section background.

**The Text/Fill Split Rule.** Green and red text always uses the text shade (up-green-text, down-red-text), never the fill shade. The fill shade is only for candles, sparklines, volume, meter segments, chart lines and the last-close tag. A filled tag carries slate-bg text, not white.

**The One Blue Rule.** Blue means "you can act here" or "this is selected": primary button, focus, active tab, current step, selected row. It is never a data colour.

## Typography

**Body Font:** Inter variable (self-hosted `/fonts/InterVariable.woff2`, weights 100 to 900), fallback -apple-system, Segoe UI, system-ui, sans-serif.
**Mono:** Consolas for terminal commands, saved rule snapshots and the threshold names in the settings grid.

**Character:** One neutral grotesk at three weights (400, 500, 600), set globally with `tnum` and `cv11` so every column of numbers aligns.

### Hierarchy
- **Price** (600, 30px, -0.02em): the selected stock's last price in the detail panel, Robinhood manner. Nothing on screen is larger.
- **Headline** (600, 20px, 1.2, -0.01em): the page title and the funnel counts.
- **Title** (600, 15 to 17px): brand name and market state (15px), detail symbol (16px), empty-state heading (17px).
- **Body** (400, 14px, 1.45): page default; the price change beside the price.
- **Body dense** (400 to 600, 13px): table rows, buttons, fields, menu items, nav tabs, check rows, section heads.
- **Label** (400 to 500, 12px): meta lines, column heads, funnel captions, market foot, notes, chips, verdicts.
- **Micro** (500 to 600, 11px): letter badges, pass/fail pills, exchange tag, kbd, OHLC legend, chart axis labels and tags.

### Named Rules
**The Tabular Rule.** Every number is set in tabular figures and right-aligned in its column.

**The No Poster Type Rule.** Display sizes stop at 30px. Section labels are sentence-case 12 to 13px headings, never uppercase eyebrows.

## Layout

- **Shell:** sticky 48px top bar (brand, tab nav, market chip and data date at right), then a page with 16px top, 20px side and 28px bottom padding.
- **Summary strip:** two-column grid, market card (300 to 380px) beside the funnel card, 12px gap.
- **Body:** results card (fluid) beside a 400px detail panel, 12px gap. The results card fills the viewport (`100vh - 252px`, min 460px) and scrolls internally; the detail panel is sticky at 60px from the top and scrolls on its own.
- **Spacing rhythm:** 4px base with 8, 12, 16, 20, 28 steps; tight 2 to 3px gaps inside badge and meter groups; 6 to 10px inside chips and controls.
- **At 1100px and below:** summary and body stack to one column; the detail panel becomes static under a 560px-tall results card, and a row click smooth-scrolls to it.
- **At 720px and below:** the top bar wraps and the nav becomes a full-width horizontally scrolling 40px tab row; secondary top-bar text hides, only the market chip stays; header buttons go full width and split equally; the results list grows to its natural height; the detail panel becomes a **bottom sheet** (fixed, max 78vh, 12px top corners, 36x4px grabber, Close button shown) that slides up on row tap.
- **Container queries on the results card:** at 820px or narrower the Base column drops; at 560px or narrower the row collapses to symbol, change and letters (sparkline, last price and RS hidden) with auto height, 46px minimum.
- **Keyboard:** J/K and arrow keys step the selection; the selected row scrolls into view.

## Elevation & Depth

Flat by default; depth comes from tonal layering (bg, panel, raised) and 1px hairlines. Shadows exist only on things that float above the page.

### Shadow Vocabulary
- **Float** (`box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45)`): dropdown menus, the Run options and coverage menus, and toasts.
- **Sheet** (`box-shadow: 0 -12px 32px rgba(0, 0, 0, 0.5)`): the mobile bottom sheet.

### Named Rules
**The Floating-Only Shadow Rule.** Cards, rows, chips and buttons carry no shadow. Only menus, toasts and the bottom sheet do.

## Shapes

Soft, small corners and 1px lines. The default radius is 6px (cards, buttons, fields, menus, verdict chips). Smaller pieces step down: 4px for the exchange tag, chart range buttons, kbd and letter headings; 3px for letter badges; 2px for meter segments and the chart's price tags. Fully rounded only for the market chip (13px), check pills (10px) and the sheet's top corners (12px). Icons are inline SVG strokes at 1.8px, round caps and joins.

## Components

### Buttons
- **Shape:** 32px tall, 0 14px padding, 6px radius, 13px/500, optional 14px stroke icon with a 6px gap.
- **Primary:** Action Blue fill, white text; hover deepens to action-blue-hover. One per header (Run screen).
- **Secondary:** transparent with a hairline-strong border; hover fills slate-raised and lifts the border to border-hover. Used for Run options, Open, Close.
- **Transitions:** background and border, 120ms ease-out.

### Chips
- **Market chip:** 26px pill, 7px status dot in currentColor, signal colour on its tint, the exposure figure in bright 600.
- **Verdict chips:** 28px, 6px radius, muted label then a 600 verdict word in the green or red text shade on the matching tint; neutral states on slate-raised.
- **Exchange tag:** 11px muted text in a 1px hairline-strong outline, 4px radius.

### Cards / Containers
- **Corner Style:** 6px. **Background:** slate-panel. **Border:** 1px hairline. **Shadow:** none. **Padding:** 12 to 16px.

### Inputs / Fields
- **Style:** Quasar outlined dense fields restyled to 32px, slate-panel fill, 1px hairline-strong outline, 6px radius, 13px/500 text, muted prefix ("Rank by", "Min RS") and placeholder.
- **Hover / Focus:** border to border-hover; focus swaps to a 1px Action Blue border.
- **Strategy select:** 330px, bright 600 value.

### Navigation
- **Top tabs:** 13px/500 muted; hover to text; current page bright with a 2px Action Blue bottom border. Horizontally scrollable without a scrollbar.

### Market Card
Market state as a 15px/600 title in its signal colour, a five-segment exposure meter (6px tall, 3px gaps, each segment hairline-strong with a proportional fill in the state colour), then two muted 12px foot lines with bright figures.

### Screen Funnel
Horizontal steps (min 128px, 10px 18px padding): 20px/600 bright count over a 12px muted caption, joined by small faint chevrons. The current step gets a 2px Action Blue underline and text-coloured caption; zero-count steps mute their number; hover fills slate-raised. Steps are tabs.

### Results Rows
- **Grid:** symbol and company (stacked), 76px sparkline, last price, 6M change, RS, six letter badges, base or industry. 38px rows, 14px column gap, 60%-alpha hairline dividers, sticky 32px header on slate-panel.
- **States:** hover slate-raised (90ms); selected Action Blue tint; rows that stopped before scoring are muted.
- **Sparkline:** 76x24 polyline, 1.4px stroke in the fill shade, green when the series ends higher, red otherwise. The 6M change figure uses the text shade.

### Letter Badges (signature)
19px squares, 3px radius, 11px/600 letter. Four states from the data layer:
- **Pass:** green text shade on green tint, every measured check passed.
- **Partial:** green text shade, 1px inset green outline at 60%, no fill; measured checks passed but some had no data.
- **Fail:** red text shade on red tint; any check failed.
- **None:** muted text, 1px inset hairline-strong outline; nothing measured.

### Detail Panel
Symbol (16px bright) with exchange tag, company and sector lines, Open action top right; the 30px price with its coloured change and muted "6M"; a muted facts line; verdict chips; 3M/6M/1Y range buttons (24px, current on slate-raised); the chart; a base note; CANSLIM checks; a kbd hint. Content fades from 0.4 to 1 opacity over 160ms on change.

### Candle Chart
ECharts, transparent background, no animation, 250px tall: price pane (62%) over a volume pane, right-side price axis.
- **OHLC legend:** top-left overlay, 11px, two unwrapped lines: the date muted and O/H/L/C in the text shade of the bar's direction, then compact volume and the MA values in their line colours; it follows the crosshair.
- **Last-close tag:** dashed 1px line in the fill shade of the bar's direction, with an 11px label filled in that shade, set in slate-bg text, at the right edge, 2px radius.
- **Pivot chip:** dotted muted line with a "Pivot" label on hairline-strong, text colour, top-left of the line.
- **Tick hiding:** axis labels within 15% of the range from the last close are blanked so the tag never collides.
- **Price axis:** at least 56px, widened to fit the last-close tag so four-digit prices are never clipped.
- **Month labels:** one short month name on each month's first session; no ticks.
- **Volume:** bars in the green or red fill shade at 40% opacity. Crosshair dashed in faint.

### Check Rows and Pills
Grouped under letter headings (20px raised square with a bright letter, then the muted letter name). Each row: label, right-aligned bright value, 58px pill column; 30px minimum, 60%-alpha divider. Pills: 11px/600, 1px 8px padding, 10px radius, Pass in the green text shade on its tint, Fail in the red text shade on its tint, N/A muted on raised.

### Ranked Rows
Strategies with their own order (Trend Leaders and the scans) add a 24px muted rank column before the symbol; the letter badges give way to the trailing column, which shows the signal date when the scan has one, otherwise the industry. A muted 12px footnote under the list says how the research acts on the candidates.

### Filters
Search (grows), sector, Rank by with a 32px square direction button beside it (arrow down for highest first), Min RS, and for CANSLIM a check filter whose result select (Fails, Passes, Not measured) appears once a check is chosen. The bar wraps; the count stays at the right.

### Menus
Run options and the funnel's coverage button open Quasar menus on slate-panel with the Float shadow, 280 to 360px wide, 12px 14px padding: a 12px bright title, 12px lines, checkboxes in 13px text with a muted help line under each.

### Notices
One line each under the header, 9px 12px padding, 6px radius, a 7px dot then a 500 bright message and an optional muted detail. Tones by tint: info on slate-raised with a blue-text dot, caution on the amber tint, error on the red tint, done on the green tint. A run in progress shows a bordered panel with the step, the count and the API calls, over a 4px Action Blue progress bar.

### Closed Sections
Below the results, each detail behind the screen is a card holding a native disclosure: 13px/600 bright summary with a small rotating chevron and an optional count badge (4px radius, muted on raised). Inside: paragraphs capped at 110ch, plain tables (12px, muted header on slate-panel, hairline rows, right-aligned numbers), Consolas code blocks on slate-bg, and the settings grid (auto-filled 280px columns of name and value, names in Consolas).

### Stock Detail
- **Layout:** the page head (title and a searchable stock picker), then a chart card (fluid) beside a 340px side card, then two independent columns of section cards (base and market checks over the price prefilter; saved C/A/S/L checks over sponsorship), then full-width statements and the Jev review. Everything stacks at 1100px.
- **Chart card:** the identity block left and the 30px price right, with the change over the chosen range; Daily/Weekly and 6M/1Y/3Y toggles in the chart-range style; a 460px chart (360px on a phone) with 50/200-session averages, or 10/40-week averages on weekly bars; a muted footnote.
- **Base on the chart:** the base is a hairline-strong outline over a 5% text-colour wash, the handle a slightly stronger wash, and the first closing breakout a small text-colour triangle on the pivot. These are neutral marks, never signal colours.
- **Axis:** 12% headroom above the highest bar keeps the pivot chip clear of the legend; overlapping date labels hide; charts spanning two or more years label years instead of months.
- **Explained checks:** on this page each check row also states its rule in muted 12px under its name, and opens (a small chevron in a fourth column) to show the saved explanation on a slate-bg well.
- **Forms:** Quasar fields in the same 32px style; the notes field grows from 84px. Saving confirms with a toast.
- **Paid actions:** the Jev review button opens a dialog (slate-panel card, Float shadow, 440px) that names the request and its cost before anything is sent; the primary button there says "Make paid request".
- **Jev answers:** one bordered card per judgment in an auto-fitting grid: a 13px bright heading, the Jev answer and the rule evidence as labelled 12px lines, and the probability and confidence in muted text.

### Market
- **Summary strip:** the market card (or a scan strategy's filter card) beside index tabs. Index tabs share the funnel card's style but sit side by side as equals, split by a hairline rather than chevrons: muted index name, the state as a 15px title in its signal colour, then the distribution count and exposure.
- **Chart card:** the index name with its symbol tag, the level and its change over the range, 3M/6M/1Y, and a key for the marks. Distribution days are small red (fill shade) triangles pointing down above the bar; follow-through days small green diamonds below it. Volume is the series each comparison used.
- **Side card:** the index's state as a verdict chip, its measurements as plain rows, the latest reason, then the selected strategy's market policy.
- **Below:** the selected index's events as an open table section, then the closed market rules and strategy sections.

### Positions
- **Summary strip:** two equal cards, the alert counts (category tabs split by hairlines, with Sell in the red text shade when present) and the exit defaults for new entries.
- **Holdings list:** the results-row grid with its own columns: position (ticker over strategy and id), alert pill, close, gain, shares, value, stop, target, entry date; optional columns drop in narrow containers. A stale or missing price adds a small amber word under the close. Alert pills follow the classic tones: Sell red, Take profits and Hold exception green, Review breakout amber, Hold and Unavailable neutral.
- **Side panel:** the Screen panel's structure (it becomes the phone's bottom sheet): identity, price and gain since entry, the alert as a verdict chip with its reason, measurements as plain rows, secondary Edit and Mark closed buttons, the saved rules closed.
- **Forms:** add and edit open in a 560px dialog with Quasar stack-label fields in a two-column grid (one column on phones), muted placeholders, a dark native date picker, and errors as an error notice inside the dialog. The primary action is the dialog's only blue.

### Portfolio
- **Top:** blocking data issues as caution notices, then the market card (exposure strategies only) beside a slot card whose steps carry a value, a text-colour label and a muted note; the strategy's market policy as a muted line under the strip.
- **Recorded account:** three 20px figures (holdings value, recorded cash with when it was recorded, total value); unknown amounts read "Unknown", never zero.
- **Tables:** plain tables in section cards. Symbols are bright links to Stock detail with the company or lot underneath; plan and alert words are pills (Proposed buy green, Review data and Review reduction amber, Held and Watch neutral); reasons are muted 12px.
- **Record cash:** a small dialog with one field; the error for an empty amount appears inside it.
- **Hypothetical portfolio:** the first section after the strip. Its head carries the title with a muted subtitle and, at the right, the starting-capital field. Four 20px figures (invested, cash, positions with the market's slot allowance, the size of the buy list) sit over a 6px allocation bar (invested in the text colour on a hairline-strong track), then the positions table with a cash row set off by a stronger hairline, a muted note on sizing and pricing, and a quiet CSV button.

### Settings
- **Layout:** a 220px sticky list of the groups (muted 13px items, slate-raised on hover; it wraps into a row below 1100px) beside a stack of group cards. Each card holds an auto-filling grid of fields: a 12px text label, the 32px field, and a muted 11px line with the saved value and the default.
- **Edits:** an edited field gets an amber "edited" word after its label and an amber-tinted outline; an info notice counts unsaved changes. Amber here means unsaved, as caution does elsewhere.
- **Actions:** Reload saved and Load defaults are secondary; Save thresholds is the page's one blue button. Errors name the field by its label, not its config key.

### Reports (Read mode)
- **Index:** one card holding the filters and the list; each study is a row with its date in a 120px muted column, a 17px bright title link, the summary at 80ch and a muted strategy line.
- **Article:** an 820px reading column beside a 240px sticky "In this report" panel, whose sub-sections are indented 12px items; below 1100px the panel moves above the article and drops its sub-sections. The title is 24px, the lede 15px, document headings 19px over a hairline, section headings 15px. Prose is 14px/1.7 at 72ch, with code on slate-raised, code blocks and tables framed by hairlines, and quotes on slate-raised (no side stripe). In-page links scroll without changing the address.
- **Variations:** period buttons in the chart-range style, the comparison as a plain table, a labelled select for the variation, and a 320px equity chart: the strategy as a solid text-colour line, SPY with dividends as a dashed muted line, compact dollar axis on the right.

### Backtest
- **Comparable backtest first:** a full-width section card for every strategy. Its head has the 20px title and a meta line (strategy, variant, data date, saved time) with, in the private app, a secondary "Run all" and the primary "Run again" at the right. Then the period as chart-range-style buttons (combined, in sample, out of sample), five 20px figures with SPY beneath (total return in its direction's text shade, CAGR, maximum drawdown, Sharpe, trades with the win rate), the 340px equity chart shared with Reports, a muted note with the dates and the ground rules, and two plain tables side by side (trade measures; each year against SPY with the difference in its direction's shade). The period's trades follow in their own section card with a quiet CSV button, then the rules and saved inputs in a closed section.
- **Older kinds below:** a 15px section heading with a muted policy line ("Approximate and strict backtests", or "Research runs" for the scan strategies) introduces the earlier layout.
- **Layout:** results (fluid) beside a 340px sticky run panel; below 1100px the panel follows the results. Saved results are a results-row list (name over strategy, method and dates; positions, return in its direction's text shade, SPY, drawdown, win rate, exposure, market rule), with the shown result selected.
- **Result:** a 20px title, a meta line, five 20px figures with SPY or context beneath, notices for rule differences, the approximate method, empty results and samples, then a 420px chart: the strategy as a solid text-colour line, SPY dashed muted, and the allowed exposure as a stepped green line over a green tint in a lower panel.
- **Below the result:** filter or holding steps (value, text label, muted note), a rejection table with small red bars for the share failed, trade lists switched by chart-range-style buttons with counts, and the saved inputs in a closed section.
- **Run panel:** the method as range buttons; the strategy as three bordered choices with a muted description, the current one outlined and tinted in Action Blue; stack-label fields in two columns; secondary Check and Prepare buttons and the primary Run. Preparing data, which makes API calls, asks in a dialog first. Progress shows as the run-progress notice.

### Public app
- **Strategies (its home; `/strategies` in the private app):** the Screen layout reused: a results-row list of the strategies (name over what it looks for; signals from, market rule, holdings, candidates, CAGR, the gap to SPY in its direction's text shade, and maximum drawdown; the two descriptive columns drop at 760px, holdings and candidates at 520px) beside the detail panel, which becomes the phone's bottom sheet. Above the rows a toolbar holds a muted "Backtest period" label and chart-range-style buttons for the three periods. The panel holds the identity with the variant as a tag, plain rows, then rules, sizing, the first five candidates as bright symbol links and the comparable backtest as plain rows; links are blue text. A muted footnote under the list states the shared ground rules.
- **Read-only pages:** run, save and record controls are absent rather than disabled; Backtest's results take the full width. The top bar's call count becomes "Read-only demo", and a muted 12px footer over a hairline closes every page.
- **Top bar at 1340px and below:** the right side keeps only the market chip, so every nav link fits from 1101px.

### TradeTest
A blind replay game in both apps (`/tradetest`): ten daily charts to mark up and trade forward bar by bar. Its own stylesheet is `src/stratlib/web/assets/tradetest/tradetest.css` (every class prefixed `tt-`); the chart is a canvas drawn by `chart.js`, so its colours come from the tokens above, set in code.
- **Head:** the page title, then a secondary How to play button with a question-mark icon (Help on phones), quiet Trades CSV and Copy result, secondary New set, and Download report, which stays a plain button until a chart is finished and then becomes the page's one blue button. A one-line running score ("Set so far: ...") appears above the tabs once a chart is finished.
- **Chart tabs:** ten equal steps in the funnel style, number over a 12px status; a finished tab shows its R in the direction's text shade. Two rows of five at 721-1100px; a scrolling strip with an edge fade on phones.
- **Chart card:** a toolbar of 32px icon buttons (drawing tools, Demand and Supply zone presets, Long, Short, magnet, indicators menu, fit, undo, redo; on the right the replay controls and Finish), then the canvas sized to the viewport (420 to 760px, 360px on phones). Candles, volume and moving averages follow the Candle Chart rules; the OHLC legend sits top-left; the replay start and the finish point are dashed vertical lines with muted labels; bars after the finish are dimmed.
- **Marks on the chart:** visitor drawings in a seven-colour palette of desaturated tints (zones fill at 12%); trade levels as lines in the fill shades (stop red, target green, entry text colour, dashed while pending), position boxes at a light tint, entry triangles and exit crosses. Selection handles are Action Blue. Axis tags for levels are outlined, never filled; the last-close tag stays the only filled label.
- **Side panel (340px, stacks below 1100px):** status card with a 4px progress bar, then the reveal card after Finish; the order ticket; trades as keyed rows with status pills; drawings with label field, swatches and Extend right; a closed Rules in detail disclosure. Empty panels say one short line and link to the guide's step; nothing repeats what the toolbar or tabs already show.
- **How to play guide:** a centred dialog (760px, a full-width bottom sheet on phones) with a step list on the left (the current step tinted Action Blue) or "Step 3 of 8" with dots and the step's 17px heading on phones; each step is an inline SVG picture in the site tokens built from the real toolbar icons, numbered actions, and Back / Next (primary) with Show me and, on the first visit, Skip the guide. It opens itself on the first visit; H or the button reopens the last step, ? opens Rules and keys. Show me closes it and rings the real controls with a 2px Action Blue outline (two pulses, still under reduced motion), a tip beside them and a "Back to the guide" toast that stays until used or dismissed.
- **Floating:** toasts (bottom-left of the chart, Float shadow), menus, tooltips and the centred confirm dialogs, whose safe choice takes focus. On touch, every control is at least 32px; swatches keep a 24px dot inside a 32px target.
- **Report:** a standalone HTML download in the same tokens, with white print styles; images of each chart, no script.

### Motion
Short and functional: 90ms row hover, 120ms colour/border transitions, 160ms detail fade, 220ms bottom-sheet slide on `cubic-bezier(0.22, 1, 0.36, 1)`. `prefers-reduced-motion: reduce` removes every transition and animation.

## Do's and Don'ts

### Do:
- **Do** keep text at 4.5:1 or better against the surface it actually sits on, including tints, hover fills and the selected-row tint.
- **Do** set green and red words and figures in up-green-text and down-red-text; keep up-green and down-red for marks and fills.
- **Do** use Action Blue only for the primary action, focus, the active tab or step, and the selected row.
- **Do** carry signal colour as text, strokes or a 14 to 16% tint.
- **Do** set every number in tabular figures and right-align numeric columns.
- **Do** label sections with sentence-case 12 to 13px headings and muted helper text beside them.
- **Do** use inline SVG stroke icons (1.8px, round caps).
- **Do** keep the CANSLIM letter badges on result rows, in their four calm states.

### Don't:
- **Don't** use giant display type; nothing exceeds the 30px price.
- **Don't** use solid colour tiles or filled coloured cards; the last-close tag on the chart is the only filled signal label.
- **Don't** raise saturation or add neon; Metro was rejected as too loud.
- **Don't** use uppercase eyebrows or kicker labels above headings.
- **Don't** put shadows on cards, rows, chips or buttons.
- **Don't** use blue as a data colour, or green/red for anything other than direction and pass/fail.
- **Don't** set text in Faint; it is for chevrons and crosshairs only.
- **Don't** set text in the green or red fill shade, or white text on a green or red fill.
