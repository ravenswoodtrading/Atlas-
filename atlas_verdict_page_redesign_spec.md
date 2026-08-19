# Atlas Verdict Checker — Redesign Spec

Redesign the verdict result page. Keep all existing data/fields — this is a layout and visual hierarchy pass only, no new data required.

## Problems with the current page
- Verdict badge (BUY/WATCH/AVOID) is small and easy to miss — it's the single most important element on the page and should read that way instantly.
- The reasoning paragraph is one dense block of text with no scannable structure.
- All summary cards (price, profit, sales, rating) have identical visual weight even though profit/ROI is the number that actually drives the decision.
- Demand/competition and price-history sections are plain label/value line lists with no visual differentiation between "this is fine" and "this is a red flag."

## Layout, top to bottom

### 1. Verdict banner (replaces the small pill badge)
Full-width card, colored background tied to verdict:
- BUY → success/green tones
- WATCH → warning/amber tones
- AVOID → danger/red tones

Contents: icon + verdict word (large, bold) on the left, ASIN on the right (monospace, muted). Below that: product title (medium weight) and short attribute line (muted, smaller).

### 2. Metric row — 4 cards in a grid
Price, Profit/ROI, Monthly sales, Rating. All same neutral card style **except Profit/ROI**, which gets a tinted background (green if positive, red/neutral if negative or unclear) and is visually the strongest card in the row — this is the number a seller scans for first.

### 3. Reasoning card — "Why [verdict]"
Convert the current paragraph into a **bulleted list** of concrete red flags / supporting points, not prose. One line per point. This is the biggest single readability fix.

### 4. Two-column detail section
**Left: Demand & competition**
- Turn binary risk fields (FBA offer present, Amazon on listing, Out of stock) into small colored status pills, not plain "Yes"/"No" text — red pill for the risky answer, green for the safe one, regardless of whether that's "Yes" or "No" for that specific field.
- Buy box % gets a small horizontal progress bar under the number.
- Offers trend (rising/falling) gets a small trend arrow icon next to the count.

**Right: Price history**
- Lead with current price and a "% vs 30-day avg" delta (colored red if price dropped a lot / margin risk, green if favorable).
- Add a small inline sparkline (simple SVG polyline, last ~180 days) showing the price trend at a glance — don't need exact data points, a smoothed line is fine.
- 30/90/180-day averages as three small stat columns.
- Lowest-ever / highest as a muted footer line under a thin divider.

## Visual system to use throughout
- Flat design — no gradients, no drop shadows, no glow effects.
- Corner radius: 12px on cards, ~8px on inner elements (pills, buttons).
- Borders: hairline (0.5-1px), low-contrast — cards should be defined mostly by subtle background tint, not heavy borders.
- Color coding is semantic, not decorative: green = good/safe, amber = caution, red = risk/bad. Apply consistently across the verdict banner, the profit card, and every status pill.
- Two font weights only: regular for body/values, medium (500) for labels/headings. Avoid heavy/bold weights — they look out of place next to lighter data text.
- Generous internal card padding (~1-1.25rem) and consistent gaps between cards (~12px) — avoid cramming data lines tightly together.
- Icons: simple line-style icons (not filled/solid) next to section headers (e.g. a people icon for "Demand & competition", a line-chart icon for "Price history") to aid quick scanning — optional but recommended if an icon set is already in the project.

## What NOT to change
- Data fields and values themselves — this is purely presentational.
- The ASIN/cost-price input form above the results — out of scope for this pass.
