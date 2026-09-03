# Atlas — Full Lead/Opportunity Criteria Summary

Generated 2026-09-03 for external review. Covers every rule that decides whether a product
becomes a lead, what label it gets, and what makes it onto (or off) the Review Queue, across
both of Atlas's two lead-generation pipelines:

1. **Scan pipeline** — automated brand/category scans and Competitor Watch, scored by
   deterministic code (`OpportunityEngine`).
2. **Verdict Checker pipeline** — manually-entered ASINs and VA-sheet leads, scored by Claude
   against a written prompt (`generate_verdict`).

These two pipelines share the same **hard numeric floors** but reach a verdict differently: one
by arithmetic, one by an LLM reading a criteria prompt. Both feed into the same Review Queue.

---

## 1. The hard floors — shared by everything

A product/lead must clear **all four** of these simultaneously, at whichever is better of
*today's* price or the *90-day average* price, before it can be recommended at all:

| Floor | Value | Constant |
|---|---|---|
| ROI | ≥ 17% | `OpportunityEngine.MIN_VIABLE_ROI` |
| Margin (profit as % of sale price, not the same as ROI) | ≥ 13% | `MIN_VIABLE_MARGIN_PCT` |
| Absolute profit per unit | ≥ £2 | `MIN_VIABLE_PROFIT_GBP` |
| Sale price | ≥ £10 | `MIN_SALE_PRICE_GBP` |

25%+ ROI is a separate, higher bar used specifically for "star buy" / Discord-alert trust (see
§4) — not a replacement for the floor above, an addition on top of it.

Failing this floor doesn't necessarily mean immediate rejection — see §3 (PEAK_WINDOW) for the
one exception: a speculative "was viable at a recent price peak" fallback.

These same four numbers are also stated explicitly inside the Verdict Checker's Claude prompt
(§6) and in `criteria.md`, so both pipelines are working from the identical bar.

---

## 2. Scan pipeline: how score and confidence are computed

Every scanned product gets a **score** (0-100) and a **confidence** (0-100), computed
independently, then combined into a recommendation (§3).

### 2a. Score (`ScoringEngine`, max raw total 125, capped at 100)

| Factor | Points | Condition |
|---|---|---|
| Demand improving | +10 | Sales rank better than 90 days ago |
| High sales velocity | +10 | 30+ Keepa rank drops in 30 days |
| Monthly sales — tiered | +25 / +18 / +10 / +5 | 50+/20+/5+/1+ confirmed monthly sales |
| Monthly sales — no confirmed figure, rank-drop proxy | +12 / +6 | 15+ / 5+ rank drops in 30d (weaker, always labelled an estimate) |
| No sales evidence at all | −25 (fails) | No confirmed sales AND no rank-drop evidence |
| Competition easing | +15 | Fewer offers than 90 days ago |
| Price stable | +15 | Within 10% of the 90-day average |
| ROI — tiered | +30 / +22 / +14 / +6 | 50%+ / 35%+ / 20%+ / 10%+ (best of today/90d) |
| ROI below 10% | fails (0, no negative) | — |
| Profit — tiered | +20 / +14 / +6 | £15+ / £8+ / £3+ (best of today/90d) |
| No real profit | fails (0) | — |
| Hazmat | −100 | Flat penalty |
| Adult product | −100 | Flat penalty |

Sales evidence and ROI are the two largest single factors (25 and 30 points respectively) —
deliberately weighted highest so a product can't coast to a good score on demand/competition/
price-stability alone while being a genuinely bad buy.

### 2b. Confidence (`ConfidenceEngine`, starts at 100, floor 0)

| Factor | Penalty | Condition |
|---|---|---|
| Large price swing | −30 | >±15% price change vs. trend baseline |
| Competition surging | −20 | Offer count up >50% vs. 90 days ago |
| Low sales velocity | −20 | Under 10 Keepa rank drops in 30 days |

Confidence measures *volatility/reliability of the numbers*, not profitability — a product can
have an excellent score and still have low confidence if its price has been swinging wildly or
competition just spiked. Two of the three penalties combining lands exactly on 50 (30+20), which
is a common real value.

---

## 3. Scan pipeline: how a recommendation is decided (`OpportunityEngine.analyse`)

Evaluated in this exact order — first match wins:

1. **`GATED`** — brand is on the Gated Brands list. Overrides everything else regardless of how
   good the numbers are; score/confidence are still computed (for the Gated Brand Opportunities
   page) but this recommendation always wins.

2. **Fails the hard floor (§1) at both today's and the 90-day-average price** →
   check a separate, lower-trust fallback:
   - **`PEAK_WINDOW`** if ALL of: profit/ROI/margin/price clear the *same* four floors but
     evaluated at the 90-day **peak** UK price instead, AND the peak genuinely recurs — at
     least 5 of the last 90 real reconstructed daily prices actually cleared 17% ROI at
     today's source cost (`PEAK_MIN_VIABLE_DAYS_90D = 5`, day-by-day, not just "the price moved
     around"), AND there's real sales evidence (confirmed monthly sales, or 3+ rank drops in
     30d — `PEAK_SALES_DROPS_THRESHOLD`).
   - Otherwise → **`IGNORE`**.

3. **`BUY`** — score ≥ 85 AND confidence ≥ 80.

4. **`CONSIDER`** — score ≥ 65 AND confidence ≥ 60.

5. **`LOW_CONFIDENCE`** *(added 2026-09-03)* — score ≥ 65 (i.e. genuinely CONSIDER-tier or
   better) but confidence < 60. The lead is real and viable at today's/90d price — just flagged
   because of price volatility / competition surge / low velocity. Shown separately on the
   Review Queue with the actual confidence-factor breakdown, rather than disappearing.

6. **`LOW_SCORE`** *(added 2026-09-03)* — score < 65, but real sales evidence exists (confirmed
   monthly sales, or 3+ rank drops in 30d). The product clears the hard floor (real profit/ROI/
   margin/price) and has proven sales, but the *composite* score — which also weighs demand
   trend, competition stability, and velocity, not just profitability — came in under 65. Shown
   separately with the actual score-factor breakdown.

7. **`IGNORE`** — none of the above (viable at the floor, but no sales evidence and score < 65).

**Why LOW_CONFIDENCE and LOW_SCORE exist**: before 2026-09-03, anything that reached step 5/6's
conditions fell straight to IGNORE with zero visibility anywhere — confirmed hiding real leads,
including one with 336-434% ROI, £1,138 profit, and genuine sales evidence. Both are deliberately
**excluded** from "notable" (§4) — they never count as a full-trust star buy or trigger a Discord
ping; the whole point is "worth a look, but check why before trusting it."

---

## 4. What counts as a "star buy" / Discord-worthy (`ProductRepository.is_notable`)

The trust bar shared by: the Review Queue's "notable" filter, the Dashboard's star-buy/BUY
counters, Discord auto-notifications, and `SellerWatchService.list_notable_buyable` (competitor
finds).

```
if recommendation in (IGNORE, GATED, LOW_CONFIDENCE, LOW_SCORE): False
else: recommendation == "BUY" OR (has_sales_evidence AND (roi > 25% OR roi_90d > 25%))
```

`has_sales_evidence` = confirmed monthly sales > 0, OR 3+ Keepa rank drops in 30 days
(`SALES_DROPS_NOTABLE_THRESHOLD`).

Note the second branch is **recommendation-agnostic apart from the exclusion list** — any
CONSIDER-tier (or better) product with real sales evidence and >25% ROI counts as notable, not
just BUY-recommended ones. This is deliberately narrow to avoid a known bug class: a record can
otherwise satisfy this branch via *two different paths at once* (e.g. a PEAK_WINDOW record whose
regular, non-peak ROI also clears 25%) and get double-counted — this is why LOW_CONFIDENCE and
LOW_SCORE are explicitly excluded up front now, rather than relying on their ROI numbers happening
not to overlap.

---

## 5. What actually lands on the Review Queue (`ReviewQueueService`)

### Main tab (`list_leads`)
Merges, de-duplicated by ASIN:
- **Scan-sourced "notable" records** (§4)
- **PEAK_WINDOW records** that ALSO clear a stricter "worth the risk" gate: peak ROI ≥ 35%
  **AND** score ≥ 65 (`PEAK_WORTH_IT_ROI` / `PEAK_WORTH_IT_SCORE`) — both required as of
  2026-09-03 (was OR until then; that let a score-22, currently-loss-making product through
  purely on a speculative peak ROI number — real bug, not a design choice).
- **LOW_CONFIDENCE records** (§3.5), unfiltered further — the score≥65 bar built into the
  classification IS the gate.
- **LOW_SCORE records** (§3.6), same — the viability + sales-evidence conditions built into the
  classification are the gate.
- **Buyable, notable competitor detections** (`SellerWatchService.list_notable_buyable`)
- **Pending Verdict Checker / VA-sheet Leads with a `BUY` verdict** (§6-7)

### "Consider" tab (`list_consider_leads`)
- Scan-sourced records with recommendation `CONSIDER`, profitable (today or 90d), real sales
  evidence, that do **not** already qualify as "notable" (so nothing appears on both tabs).
- Pending Leads with a `WATCH` verdict.
- Deliberately **not** expected to reach zero — a genuine backlog is fine here, unlike the main
  tab.

### Never appears anywhere
- `AVOID`-verdict Leads.
- `IGNORE`/`GATED` scan records.
- Anything already reviewed (a human already actioned it).

---

## 6. Verdict Checker / VA-sheet pipeline (Leads) — Claude-driven

Every manually-checked ASIN or VA-sheet row gets a `Lead` row, analyzed once by
`generate_verdict` (Claude), which outputs exactly `BUY`, `WATCH`, or `AVOID` plus 3-5 bullet
reasons. This is **not** threshold arithmetic the way the scan pipeline is — it's an LLM reading
a structured prompt containing:

- The same four hard floors (§1), stated explicitly, with an instruction that a sub-£10 sale
  price is an automatic AVOID "regardless of how strong every other figure is."
- 25%+ ROI framed as "what this app treats as a strong lead."
- Keepa-derived metrics (profit/ROI/margin at today's and 90-day price, PLUS a speculative
  **peak** figure mirroring PEAK_WINDOW for "sawtooth" leads).
- VA/SAS-verified ground-truth figures when present (never second-guessed against Keepa's own
  estimate).
- **Source marketplace buyability check** (A2A leads only) — Amazon can only reclaim VAT on a
  purchase from Amazon itself or an FBA seller, never a merchant-fulfilled (FBM) seller. If the
  EU buy box is FBM-held, the verdict must be AVOID "no matter how good the ROI is." Amazon
  being temporarily out of stock is treated as a park/revisit case, not a hard reject.
- **Gated brand status** — a strong-looking gated lead is capped at WATCH, never BUY.
- **Past rejection history for this exact ASIN** — weighed heavily unless something concrete has
  genuinely changed (price, stock, competition); a same-brand/category rejection is softer
  evidence, not automatic.
- **`criteria.md`** (§8) — the user's own free-text judgment notes, read fresh on every call, no
  restart needed.
- A deep-dive SP-API live price cross-check, when the baseline came back BUY/WATCH (free, no
  extra Keepa cost).

Four explicit instruction rules also constrain the prompt: never call a lead "not profitable" if
profit is actually positive; state figures plainly rather than editorializing over them; judge
Amazon's presence by buy-box *share*, not mere presence; state review counts exactly, never round
down to "no reviews" when the real count isn't zero.

---

## 7. Watchlist — the "not profitable now, but recently was" tracker

`WatchlistService.maybe_auto_watch` runs on every scan (not on Leads today — see the open item
below). Auto-adds a product to the Watchlist when:

- Not already profitable today or at the 90-day average, AND
- A real 90-day EU-source-cost **low** exists, AND
- The **hypothetical** scenario (today's UK price, EU cost swapped to that 90-day low) would
  score CONSIDER or BUY under the normal `OpportunityEngine` logic.

This is a genuinely separate, lower-trust list from the Review Queue — items sit here to be
periodically rechecked (`check_stale`, weekly), not acted on immediately.

**Documented self-correction already built in**: `prune_stale_auto_adds` removes an auto-added
watch after 14+ days and 2+ genuine rechecks if it has *never once* gone profitable either way.
This exists because a 2026-08-21 review found the watchlist was 94% auto-added off a single
historical-low snapshot, and 88% of it never went profitable in up to 23 days of rechecking — a
single low day proved almost nothing. This is the precedent for the day-count-evidence work
currently in progress (see Open Items below).

---

## 8. `criteria.md` — the user's own editable rules

Read fresh on every Verdict Checker call, given to Claude as additional context. Currently
states:
- The same four hard floors (§1).
- 25%+ ROI = strong lead.
- Gated brands excluded from anything actionable.
- Sales evidence required (confirmed sales, or 3+ rank drops in 30d).
- **PEAK leads need 35%+ peak ROI OR a 65+ score** — ⚠️ **this line is now stale**: the actual
  code was fixed 2026-09-03 to require BOTH (AND), not OR. This file was not updated to match at
  the time (deliberately — the user asked to pause further changes before this could be
  corrected). LOW_CONFIDENCE and LOW_SCORE (§3.5-3.6) are also new as of 2026-09-03 and aren't
  documented here yet either.
- One standing judgment note: EU A2A leads are only buyable from Amazon itself or an FBA seller
  (VAT invoice requirement) — FBM-only or Amazon-out-of-stock both block a BUY.

---

## 9. Competitor lead sourcing classification: OA vs. A2A vs. Wholesale

Separate from the profitability recommendation entirely — this guesses **how a competitor
likely sourced** a newly-detected listing (Competitor Watch), from price history alone (Keepa
has no actual purchase records). Checked in this order, first match wins:

1. **`EU A2A`** — a genuinely viable EU-sourced margin (≥17% ROI, `RECENT_VIABLE_ROI_PCT`) on at
   least one of the last **10 calendar days** (`RECENT_WINDOW_DAYS`), computed day-by-day against
   *that specific day's* real UK price and *that specific day's* real EU cost — not today's
   snapshot, not a 90-day average, not "somewhere in the last 90 days." The single best day is
   shown as a "best guess" for when/what they likely paid.

2. **`UK A2A`** — the UK price dipped to ≤70% of its 90-day average (`DIP_THRESHOLD`) on at
   least one of the last 10 days. Still flagged as a **live** opportunity if the price hasn't
   recovered back to ≥90% of the 90-day average (`RECOVERY_THRESHOLD`) yet.

   *(If both EU A2A and UK A2A evidence exist, EU A2A is reported as primary — more specific
   evidence of how/where they bought — with the UK dip folded in as corroborating context.)*

3. **`Wholesale (likely)`** — no recent A2A evidence in either direction, but at least one
   structural signal: ≤3 current sellers on the listing, a multipack/case-of-N title pattern, or
   this seller has been seen listing this same brand 2+ times before. Any ONE signal is enough
   to tag it (each raw value still stored so the guess can be judged, not taken on faith).

4. **`OA / unclear`** — fallback when nothing above matched. Explicitly logged as "checked and
   found nothing," not silently defaulted.

10-day window is deliberately much narrower than the 90-day windows used elsewhere in Atlas —
Competitor Watch detects a listing roughly when it was *added* to a seller's inventory, so only
recent evidence actually explains *that* listing; a margin from months ago says nothing about
why it showed up now.

---

## Open items (in progress, paused before this report was written)

At the user's request ("before we make any changes"), work in progress was paused mid-build and
is **not yet live**:

- A new mechanism (`VerdictService.compute_source_drop_evidence`, `WatchlistService.
  maybe_auto_watch_lead`) to extend the Watchlist's "not profitable now, but recently was"
  tracking (§7) to VA-sheet/Verdict-Checker Leads — currently this ONLY runs for scan-pipeline
  products, so an AVOID-verdict A2A lead from a VA just dies with no future monitoring at all,
  unlike a scan-sourced equivalent.
- Requirement from the user: day-count evidence (how many of the last 90 days the EU source cost
  would actually have been viable) must be a real factor, not just a single historical-low
  snapshot — directly informed by the Watchlist's own documented 88%-never-profitable problem
  (§7) — and "profitable now" must always outrank "may be profitable in X days."
- `criteria.md` needs updating to reflect the AND-not-OR peak gate fix and the two new
  LOW_CONFIDENCE/LOW_SCORE tiers (§8).
