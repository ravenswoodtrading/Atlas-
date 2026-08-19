# Atlas Feature Spec — Competitor Storefront Watch + Sourcing Classifier

**Status:** ready for implementation
**Target:** Claude Code, working against the existing `Atlas-` repo, `claude-changes` branch
**Depends on:** existing `ProductService`, `FeeEngine`, `OpportunityEngine`, `ProductRepository`, Keepa singleton client

---

## 1. Goal

Track specific competitor seller IDs. When a tracked competitor lists a new ASIN:

1. Detect it (zero manual checking).
2. Run it through Atlas's existing pricing/fee/scoring pipeline.
3. Guess **how they likely sourced it** (UK A2A / EU A2A / Wholesale / OA-unclear).
4. If the sourcing method implies a live, repeatable opportunity (A2A of either kind), surface it as an actionable lead — this is the main point of the feature. Everything else is supporting context.

---

## 2. Data source

Keepa `/seller` endpoint, `storefront=1`, batched up to 100 seller IDs per call. Returns `asinList` (current full inventory) and `asinListLastSeen` per seller. Does not trigger new data collection, so it's cheap relative to per-product pulls — cost is per seller batch, not per ASIN returned.

Seller ID is visible in a storefront URL (`.../sp?seller=A2L77EE7U53NWQ`) or in any offer object.

---

## 3. Database schema

### New table: `tracked_sellers`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| seller_id | TEXT, unique | Keepa/Amazon seller ID |
| nickname | TEXT | user's label |
| active | BOOLEAN, default true | pause without deleting history |
| last_checked_at | DATETIME, nullable | |
| last_asin_snapshot | TEXT (JSON list of ASINs) | for diffing |
| created_at | DATETIME | |

### New table: `seller_new_listings`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| tracked_seller_id | FK → tracked_sellers.id | |
| asin | TEXT | |
| detected_at | DATETIME | |
| product_record_id | FK → product_records.id, nullable | link to full scoring report once scored |
| sourcing_tag | TEXT | `EU A2A` / `UK A2A` / `Wholesale (likely)` / `OA / unclear` |
| currently_buyable | BOOLEAN | true if the underlying spread/dip is still live *right now* |
| sourcing_reasoning_json | TEXT (JSON) | the numbers behind the tag — see §5 |
| dismissed | BOOLEAN, default false | user can clear from feed without deleting |

**Migration reminder (per project convention):** `Base.metadata.create_all()` only creates new tables, it will not alter existing ones. Since both tables above are new, a plain `create_all()` covers this feature — no `ALTER TABLE` needed *unless* a later iteration adds columns to `product_records` (see §7 optional extensions), in which case follow the existing `migrate_db.py` manual-migration pattern.

---

## 4. Service: `SellerWatchService`

Mirrors the shape of `overnight_sweep_service` — runs as a background-thread job on a timer (default: every 2 hours, configurable), not continuously, since storefronts don't change minute-to-minute and this keeps it token-cheap.

**Per run:**
1. Load all `tracked_sellers` where `active = true`.
2. One batched Keepa `sellers()` call, `storefront=1`, for all seller_ids (chunk at Keepa's 100-seller batch limit if the list ever grows that large).
3. For each seller, diff returned `asinList` against `last_asin_snapshot`:
   - ASINs present now but not in the snapshot → new listing event.
   - (Optional, see §7) ASINs in the snapshot but no longer present → delisting event.
4. For each new ASIN: hand off to the existing product pipeline (`ProductService` → `ProductMapper` → `FeeEngine` → `OpportunityEngine`) exactly as an ASIN-list upload does today — no new pipeline needed, just a new *trigger* for the existing one.
5. Pass the resulting product + fee data into `SourcingClassifier` (§5).
6. Write a `seller_new_listings` row with the classification.
7. Overwrite `last_asin_snapshot`, update `last_checked_at`.

**Zero-token pre-checks still apply** before spending anything on a newly-detected ASIN: check `known_products` exclusions and `excluded_products` first, same as regular discovery — a competitor listing something you've already excluded shouldn't cost tokens to re-evaluate.

---

## 5. Module: `SourcingClassifier`

Runs once per newly-detected ASIN, after the standard pipeline has already fetched UK `stats=90` and EU current prices (no new tokens spent — this reuses data the pipeline already pulled).

**Classification order (first match wins):**

### 1. `EU A2A`
Condition: a live EU marketplace price (today or 90d-typical, whichever is more favorable — same rule as the existing ceiling check) + fees is still profitable against current UK price right now.
`currently_buyable = true`.
Reasoning stored: which marketplace, spread %, margin at current fees.

### 2. `UK A2A`
Condition: no EU spread, but the UK 90-day price series shows a dip-and-recovery signature:
- **Dip depth:** 90-day minimum price ≤ ~70% of 90-day average (tunable threshold, call it `DIP_THRESHOLD = 0.70` as a named constant so it's easy to recalibrate later, same spirit as the existing ROI-cutoff recalibration note).
- **Recovery:** current price has climbed back to ≥ ~90% of the 90-day average (`RECOVERY_THRESHOLD = 0.90`). If it hasn't recovered — price is still near the dip — set `currently_buyable = true` and surface this as a live opportunity in its own right, not just a historical inference.
Reasoning stored: dip %, recovery %, whether still buyable.

### 3. `Wholesale (likely)`
Condition: no A2A pattern in either direction, and the offer/seller structure suggests a standing supply relationship rather than an opportunistic find:
- Small, stable seller count on the listing (e.g. ≤ 3 distinct sellers)
- Multipack/case-pack signal in title/variation (e.g. "3 Pack", "Case of 6")
- Same brand recurring across this competitor's other recent detections (requires looking at the competitor's own listing history, which you're now accumulating in `seller_new_listings`)
Reasoning stored: seller count, multipack flag (bool), brand-repeat count for this seller.

### 4. `OA / unclear`
Default fallback — no pattern matched. Reasoning stored: explicitly note which checks were run and came back negative, so the "why" is honest rather than implying false confidence.

**Important framing for the UI:** label everything below `EU A2A`/`UK A2A` as a *best guess*, not a confirmed sourcing method — Keepa has no visibility into a seller's actual purchase history, so `Wholesale (likely)` and `OA / unclear` are structural inferences only.

---

## 6. UI

New sidebar page: **Competitors**

- **Add/manage tracked sellers**: seller ID + nickname input, active/paused toggle, remove.
- **Tracked sellers table**: nickname, last checked, total new listings detected (all time), new listings in last 24h.
- **Detections feed**: reverse-chronological list of `seller_new_listings`, each row showing:
  - ASIN, title, which competitor, detected_at
  - `sourcing_tag` as a badge, colour-coded (A2A tags green/actionable, Wholesale amber/manual-followup, OA/unclear grey/low-priority)
  - `currently_buyable` badge if true — this is the highest-value flag on the page
  - Link into the existing "Why?" score breakdown (reuses `report_json` via `product_record_id`)
  - Dismiss button (soft delete via `dismissed`)
- Filter/sort by sourcing tag and by "currently buyable only" — the latter is likely the first thing to check each session.

---

## 7. Extensions worth considering (not required for v1, flagged for later discussion)

A few things adjacent to this feature that could surface additional leads with data you're already pulling or paying for:

- **Delisting detection**: track ASINs that *disappear* from a competitor's `asinList` between checks. A sudden delist right after a price crash may mean the opportunity dried up — useful negative signal to avoid chasing dead leads, and worth cross-referencing against your own Watchlist if you've been tracking the same ASIN.
- **Multi-competitor corroboration**: if two or more independently tracked sellers add the same ASIN within a short window, that's a stronger signal than either alone — worth a "hot" flag when `seller_new_listings` shows ≥2 distinct sellers on the same ASIN recently.
- **Post-listing rank velocity**: check whether the ASIN's sales rank actually improved after the competitor's listing appeared (using the same rank-drop data `ScoringEngine` already reads) — this validates that the product is genuinely selling for them, not just listed.
- **Confidence scoring on the sourcing tag itself**, mirroring the existing `ConfidenceEngine` pattern — e.g. an EU A2A tag backed by a large, stable spread across multiple recent price points is higher-confidence than one based on a single momentary reading.
- **Manual feedback loop**: let the user tag a detection with what the sourcing method actually turned out to be (if they find out via other means), and store it — this builds a small labelled dataset that could eventually validate or retune the `DIP_THRESHOLD`/`RECOVERY_THRESHOLD`/seller-count constants, the same way the existing ROI/score thresholds are flagged as due for recalibration.
- **Brand-level rollup**: if a tracked competitor repeatedly sources from the same brand, surface that brand as a candidate for Atlas's own brand-search Discovery flow — closing the loop back into the tool's original core pipeline.

None of these block v1 — the seller watch + basic 4-way classification is a complete, useful feature on its own. These are candidates for a v2 pass once real detection data exists to tune against.

---

## 8. Build order suggestion for Claude Code

1. Migrations: `tracked_sellers`, `seller_new_listings`.
2. `SellerWatchService` — detection + diffing only, writing rows with `sourcing_tag = NULL` first, to confirm the Keepa storefront call and diffing logic work before layering classification on top.
3. `SourcingClassifier` as its own module — easy to unit-test against known ASINs with manually-verified sourcing methods, if any are on hand.
4. Wire classifier into the service.
5. Competitors page (routes + template), starting with the tracked-sellers table and add/remove, then the detections feed.
6. Background-thread scheduling, following the existing sweep-thread pattern.
