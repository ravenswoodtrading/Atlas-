# Atlas: OA sourcing agent — implementation brief

> Supersedes `atlas-retailer-feed-sourcing-spec.md` (abandoned — see that file).
>
> **Read §0 before anything else.** Atlas already implements a substantial part of what
> follows. The single most common way to get this wrong is to build a parallel version of
> something that already exists.

## 0. Work with the existing architecture

Before writing code, inspect and understand these. Each already does a job this brief
depends on. **Do not replace any of them without stating a compelling technical reason
first and getting agreement.**

| Component | What it already does |
|---|---|
| `services/oa_source_discovery_service.py` | The whole OA discovery pipeline: candidate selection, query building, match classification, auto-promotion. This is the file being extended, not replaced |
| `build_queries()` | EAN / MPN / Brand+Title queries with exact-phrase quoting, capped per ASIN |
| `classify_match()` / `classify_shopping_match()` | The 5-tier match hierarchy: `ean` > `brand_mpn` > `mpn` > `brand_title` > `title_only`, plus site-restricted verification hits |
| `MATCH_TIER_SCORES` / `TRUSTED_MATCH_TIERS` | Confidence scoring and the auto-promotion trust bar. **A new confidence system must not be invented** |
| `services/oa_domain_classifier.py` | Retailer/marketplace/aggregator classification, deliberately not a fixed allowlist |
| `services/fee_engine.py` | Fees, VAT, `max_source_cost`, `OA_TARGET_ROI_PCT` |
| `services/opportunity_engine.py` + `product_repository.save_opportunity` | Scoring and the promotion path into the Review Queue |
| `_promote_if_qualifying()` | How a verified opportunity becomes a real Atlas lead |
| `database/models.py` → `OaSourceRun`, `OaSourceCandidate` | Run and result persistence, already shaped for this data |
| `sp_api/client.py` | Working LWA auth, pacing, 429 retries. `getItemOffers` implemented |
| `services/keepa_priority.py`, `services/scan_coordinator.py` | Keepa token contention control — see §6, this is not optional |
| `config/exclusions.py`, `ProductRepository.get_gated_brand_pairs()` | Gated brands and exclusions |

**Deliverable before any implementation:** a short written plan naming the files you intend
to change and what you intend to add, for review. Do not modify production behaviour until
that's agreed.

---

## 1. Objective

Make Atlas behave like an experienced OA sourcing VA:

> ASIN → EAN/MPN/title → search UK retailers → find the exact product cheaper → verify
> price, stock and variant → return a structured opportunity → Atlas scores profitability.

Atlas already does most of the first half. The genuinely new capability is **verification**:
opening the retailer's page and confirming the thing is real, in stock, the right pack
size, and actually that price.

---

## 2. Architectural principle

Cheap and deterministic first; AI only where reasoning is genuinely required.

```
Atlas candidate ASIN
   ↓  (free)      SP-API screen: rank, dimensions, Buy Box price → is profit even possible?
   ↓  (cheap)     Search layer: one shopping query, then Brave discovery + verification
   ↓  (cheap)     Match classification via existing 5-tier hierarchy
   ↓  (moderate)  Browser verification — ONLY for candidates that clear the trust bar
   ↓  (free)      FeeEngine + OpportunityEngine → Review Queue
```

The browser agent is an **escalation layer**, not the first tool. It should never run on
an ASIN that the free screen already rejected, and never on a match tier Atlas doesn't
trust.

---

## 3. Search layer — A/B, don't assume

Atlas currently uses SerpAPI (Google Shopping, Path 1) with Brave (web discovery +
site-restricted verification, Path 2 fallback).

**Do not swap SerpAPI for Serper on cost grounds alone.** The goal is deals found, not
cheap searches. SerpAPI is ~$15 per 1,000; Serper is ~$0.30–1.00 — but if Shopping
results materially improve the hit rate, the difference pays for itself many times over.

Instead:

1. Build `services/serper_client.py` implementing the **same signature and return shape**
   as `serpapi_client.search_uk_shopping()`, so it is a drop-in. Note Serper returns price
   as a display string, so the client parses it into `extracted_price` itself.
2. Put the provider behind a config switch — `OA_SHOPPING_PROVIDER=serpapi|serper` — so
   either can be selected without touching the OA engine.
3. Run the §8 test batch through both, on the **same ASINs with the same queries**.
   Compare: match-tier distribution, candidates found, leads promoted, cost per promoted
   lead.
4. Keep the winner. Delete the loser and its quota machinery.

Note both providers offer a shopping endpoint — they are two vendors for the same call,
not complementary layers. Brave is already the discovery layer. Atlas needs two search
providers, not four.

---

## 4. Candidate intake — widen the funnel

Today `_eligible_candidate_rows` only sees ASINs from Competitor Watch detections tagged
`"OA / unclear"`. Lead volume is capped by how many competitors are tracked.

Add a second source: `ProductFinder.find_oa_candidates()`, modelled on the existing
`find_signal_candidates` (criteria-based, no brand restriction) rather than `find_brand`.
Filter on rank ceiling, price floor and category. Reuse that module's conventions —
`perPage: 100` always, `wait=False`, return `None` on failure (never `[]`), record spend
via `TokenUsageService`.

Store candidates in a new `oa_candidate_pool` table (`asin`, `source`, `status`,
`screened_out_reason`, `amazon_price_gbp`, `sales_rank`, `target_price_gbp`, timestamps).
`_eligible_candidate_rows` then **unions** this with Competitor Watch detections — both
feed one queue.

### 4.1 Screen for free before spending anything

`searchCatalogItems` (needs adding to `SPAPIClient`; follow `get_item_offers`'s
`None`-vs-real-result convention exactly) takes 20 ASINs per call at 2 req/sec and returns
rank and dimensions together. `getItemOffers` gives the Buy Box price at ~4/sec.

Screen order:

1. Rank inside threshold, dimensions captured — free, batched
2. Buy Box price — free
3. FBA fee from dimensions via `config/fees.py` size tiers, then
   `FeeEngine.max_source_cost` — is there room for *any* profitable purchase? Free.
   (If `fees.py` is category-keyed only and can't compute from dimensions, either extend
   it or fall back to `getMyFeesEstimates` — free but only ~10 ASINs/sec)
4. Not gated, not excluded — reuse the existing checks

Only survivors cost a Keepa token for sales evidence; only those reach the search layer.
`getItemOffers` is the slowest free step, so an overnight window screens roughly 115,000
ASINs at no cost. **Intake width is bounded by run time, not budget.**

---

## 5. Browser verification agent

### 5.1 Scope — verification, not discovery

The agent runs on candidates that have already cleared the free screen, been matched at a
trusted tier, and priced. Its job is to confirm what search claimed:

1. Exact product — EAN/GTIN on page where available, else MPN/model
2. **Pack size and variant** — the classic OA loss, and exactly what a search result
   can't tell you (`OaLookupService.search_candidates`' docstring already records hitting
   this)
3. Current price, including multibuy or basket-level promotions that feeds and search
   results never show
4. Stock/availability
5. Sold by the retailer directly, not a marketplace seller on their site
6. New, not used or refurbished

That's tens of pages a night, not thousands — which is what makes it affordable.

### 5.2 Technology

Use **Stagehand's Python SDK** (`pip install stagehand`, Python 3.9+) rather than
`browser-use`. Reason: verification is a known, repeatable flow, so `extract()` against a
fixed JSON schema is cheaper and more predictable than an autonomous agent loop that
rediscovers the page every night. `browser-use`'s autonomy is the right tool for
open-ended exploration, which this isn't.

Run against **local Chrome initially, not Browserbase.** Atlas is self-hosted; at this
volume from a UK residential IP, Browserbase's proxy rotation and anti-fingerprinting
solve problems that don't exist yet. Add it if blocking actually occurs. On Windows,
`CHROME_PATH` may need setting.

### 5.3 Tier it — don't send every page to an LLM

1. Plain fetch + JSON-LD parse. Many retailers publish `offers.price` and `availability`
   as structured data. If that answers the question, stop — cost zero.
2. Stagehand `extract()` against a schema, for pages where step 1 fails.
3. A stronger model only for genuinely ambiguous cases (pack-size wording, bundle vs
   single).

### 5.4 Limits — all configuration, not hard-coded

```
OA_MAX_SEARCH_QUERIES_PER_ASIN
OA_MAX_RETAILER_PAGES_PER_ASIN
OA_BROWSER_MAX_ACTIONS
OA_BROWSER_TIMEOUT_SECONDS
MAX_CONCURRENT_OA_AGENTS        # start at 3–5, never unbounded
```

Stop on success (verified exact product, UK retailer, in stock, below Amazon price, at a
trusted match tier) or on exhaustion. Never search indefinitely.

### 5.5 Matching — use the existing system

**Do not build a new confidence score.** `classify_match` / `classify_shopping_match`
already produce the tier; `MATCH_TIER_SCORES` maps it to `match_confidence_pct` and
`source_confidence`. The browser agent's job is to supply *evidence* that upgrades or
refutes a tier — an EAN found on the page promotes a match to tier `ean`; a pack-size
mismatch refutes it outright.

`title_only` is never a confirmed match and never auto-promotes. That rule already exists
and stays.

---

## 6. Keepa priority — non-negotiable

Candidate screening and any Keepa use here is a **low-priority** consumer.

Atlas has already had a starvation incident: a 30-ASIN Verdict Checker batch squeezed to
"0 of 30 checked" by competing automated ticks. That is why
`ScanCoordinator.try_acquire_for_automated_tick` and the high-priority waiter count exist.

Therefore:

- acquire via `try_acquire_for_automated_tick()`, never `acquire_for_manual_scan()`
- check `KeepaPriority.has_pending()` at every chunk boundary and yield, following
  `BrandScanService._fetch_in_chunks` exactly — stop early, don't advance, resume next tick
- record spend under its own `usage_category` so Token Usage shows it separately

---

## 7. Results go to the existing Review Queue

Atlas remains responsible for profitability. The agent returns a verified retailer price
and nothing else — no second profitability model.

Verified price → `FeeEngine` → `OpportunityEngine.analyse` → `save_opportunity`, following
`_promote_if_qualifying`. Same Review Queue, Dashboard counters and Discord alerts as any
other lead. **No separate "OA agent results" surface.**

Persist to `OaSourceCandidate`, which already carries retailer/url/price/stock/tier/
confidence/profit/ROI. Extend it rather than adding a parallel table; add
`verification_source` (`json_ld` | `browser_agent` | `none`) and
`verification_notes`.

---

## 8. Test harness — build this before production

A `test_mode` run over ~100 real competitor ASINs (supplied), writing to the same tables
but flagged so it can't contaminate real leads.

**Log per ASIN:** every query and provider, candidate URLs, retailer pages opened,
browser actions, match tier reached, verification outcome, final opportunities, time
taken, search spend, LLM tokens, Keepa tokens.

**Report:**

- *Discovery* — % of ASINs producing ≥1 candidate; candidates per ASIN
- *Accuracy* — exact-match rate, price accuracy vs the live page, stock accuracy,
  retailer legitimacy
- *Economics* — time per ASIN, cost per ASIN, **cost per successful deal**
- *Attribution* — which query tier actually found the deals (EAN vs MPN vs title vs
  shopping). This is the number that tells you where to spend next

**On targets:** treat the first run as establishing a baseline, not passing an exam. The
brief's proposed thresholds (80% exact identification, 90% price accuracy) are reasonable
aspirations, but exact-match rate depends on whether retailers publish EANs at all, which
varies by category and is not under our control. Report actuals against those numbers and
say plainly where they landed — do not tune the test to hit them.

---

## 9. Structure for later analysis, don't build ML

Schema should support answering, later: which retailers produce deals, which categories,
which query tiers work, which retailers block the browser, which produce false matches,
success rate and cost per deal by retailer and category.

No machine learning in this build.

---

## 10. Build order

1. **Serper client + provider switch.** Small, self-contained.
2. **`searchCatalogItems` + the free screen + `oa_candidate_pool`.** Backfill from
   existing Competitor Watch ASINs first; confirm the screen rejects sensibly before
   adding Product Finder.
3. **Test harness**, running the existing (non-browser) pipeline over 100 ASINs through
   both search providers. *Checkpoint: pick a search provider on evidence.*
4. **`find_oa_candidates`** to widen intake.
5. **Browser verification**, JSON-LD tier first, Stagehand second.
6. *Checkpoint: re-run the 100-ASIN test with verification on. Did accuracy improve enough
   to justify the cost per deal?*

Do not build: autonomous purchasing, ML, retailer-specific integrations, multi-agent
orchestration, or any new profitability model.

---

## 11. Open decisions to confirm during build

- Serper's exact shopping response field names — read off a live call, don't trust docs.
  Confirm `gl: "gb"` returns genuine UK retailers and GBP.
- Whether `config/fees.py` can compute an FBA fee from dimensions, or needs extending.
- Rank ceiling and price floor for `find_oa_candidates` — start loose, let the first run
  show the distribution.
- Which retailers block automated browsing, and whether that changes the local-Chrome
  vs Browserbase call.
- How long a `screened_out` ASIN waits before re-screening, so moved prices get another
  look without re-checking everything nightly.
