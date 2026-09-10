# Atlas — saved Scan Queue brief

Implemented 10 September 2026: compact queue, brand details, manual two-tier cadence, weekly approval decisions, Command Centre alerts and source links in More info. Less regular brands wait four nominal rotations. Catalogue sizes and full-pass dates populate from subsequent scans; existing history is not backdated.

## Scan Queue UI
- Use a genuinely compact queue: one line per brand, no lead summaries or multi-line brand cards.
- Queue columns: brand, tier, products remaining after Keepa filters, last scanned, last full catalogue pass, next scan, Details.
- Show the full queue with minimal scrolling; deeper information belongs in Details.
- Add `Add brand` on the main Scan Queue only.
- Allow manual `Remove from queue` and move brand to the more regular or less regular tier.
- Keep automatic tier suggestions separate and require approval.

## Brand Details UI
- Separate page reached through Details.
- Show scan status: last scan, last full catalogue pass, next scan, products after filters.
- Show BUY leads from scans and borderline leads from scans.
- Show recent scan results and Scan Intelligence recommendation.
- Include manual controls: move more regular, move less regular, remove from queue.
- Do not include Add brand on the brand details page.

## Scan Intelligence model
- Sits behind/alongside the Scan Queue, with clear decisions awaiting approval.
- Weekly suggestions based on brand performance and competitor activity.
- Two tiers: scanning regularly and scanning less regularly.
- Brands are never deleted automatically; approved changes affect future runs.
- Command Centre should alert when Scan Intelligence decisions need review.

## Existing scan rules to preserve
- Product Finder filters before Keepa detail requests where supported: UK buy box £10+, minimum 3 sellers in the past 90 days, exclude listings where one seller has 97%+ of the Buy Box.
- Competitor and own EU A2A performance can inform brand recommendations.
- Replen: A2A only, achieved ROI 20%+, exclude products with recorded customer returns, scan regularly.
