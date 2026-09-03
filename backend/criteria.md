# Sourcing criteria

This file is read fresh on every Verdict Checker call (`app/services/anthropic_client.py`)
and given to Claude as context alongside the Keepa metrics for the ASIN being scored. Edit it
directly to change what gets weighed — no restart needed, no code change needed for a
judgment-call adjustment.

Keep entries in plain language, the way you'd explain a rule to a VA. Claude reads this
alongside hard numeric gates that are enforced in code regardless of what this file says (see
below) — this file is for judgment calls and exceptions that don't reduce to a single number.

## Hard floors (enforced in code, not just this file — listed here for reference)

A lead needs to clear ALL of these to be recommended at all:
- ROI ≥ 17%
- Margin ≥ 13% (profit as a % of sale price)
- Profit ≥ £2 per unit
- Sale price ≥ £10 -- a genuinely cheap item doesn't leave enough absolute headroom for fees
  to be worth sourcing, however good the ROI/margin numbers look

25%+ ROI is treated as a strong lead, on top of the floor above — not a separate gate.

Other standing rules, unchanged from before this file existed:
- Gated brands are excluded from anything actionable regardless of numbers.
- Sales evidence is required, not just a profitable price: confirmed monthly sales, or 3+
  Keepa rank-drops in 30 days.
- PEAK leads (only profitable at a 90-day price high) need a stricter bar: 35%+ peak ROI or a
  65+ score.
- No hard stock-holding-time gate yet — confirmed monthly sales velocity is reported in the
  verdict rationale so you can judge holding time yourself against whatever quantity you're
  actually planning to buy.

## Judgment notes

Add a line here whenever a rejection reason turns out to reflect a real, repeatable preference
rather than a one-off. The `/criteria/review` page proposes additions here based on recent
rejection reasons, for your approval — it only ever writes between the markers below, and never
without your explicit approval on that page. You can also just edit this section by hand.

<!-- BEGIN JUDGMENT NOTES -->
- EU A2A leads can only be bought from Amazon itself or from an FBA seller. If the EU buy box is merchant-fulfilled (FBM), or Amazon is out of stock on the source marketplace, the lead is not buyable no matter how good the spread looks -- there's no Amazon VAT invoice to reclaim against. Treat "Amazon out of stock" as an OOS park (revisit on restock), and "FBM seller only" as a plain reject.
<!-- END JUDGMENT NOTES -->
