# Atlas: ASIN verdict checker + lead review queue

## 1. Overview

Two connected features:

1. **ASIN verdict checker** — a webpage where a VA (or you) types in a single ASIN and gets back a Keepa-driven BUY / WATCH / AVOID verdict with reasoning, plus the full metric set.
2. **Lead review queue** — leads land in Atlas either manually (via the same ASIN checker) or automatically (VA posts a row to a Google Sheet). Every lead gets auto-analyzed and shows up in a review queue for approve/reject.

Leads may come from **OA (online arbitrage)** or **A2A (Amazon-to-Amazon)** sourcing — the system should treat both the same way once an ASIN is present.

---

## 2. Data model

Single `leads` table:

| Field | Type | Notes |
|---|---|---|
| `id` | UUID/int PK | |
| `asin` | string | required |
| `source` | enum | `manual` \| `sheet` |
| `sourcing_type` | enum, nullable | `OA` \| `A2A` if known/provided |
| `raw_sheet_data` | JSON, nullable | full row as submitted by the VA, preserved as-is even if some columns are unused today |
| `va_roi` | decimal, nullable | VA-supplied ROI (from SAS), if present |
| `va_profit` | decimal, nullable | VA-supplied profit, if present |
| `va_cost_price` | decimal, nullable | VA-supplied cost, if present |
| `va_sale_price` | decimal, nullable | VA-supplied sale price, if present |
| `status` | enum | `queued` → `analyzed` → `reviewed` |
| `verdict` | enum, nullable | `BUY` \| `WATCH` \| `AVOID` |
| `rationale` | text, nullable | Claude's written reasoning |
| `keepa_metrics` | JSON, nullable | full metric set (see §4) |
| `added_at` | timestamp | |
| `analyzed_at` | timestamp, nullable | |
| `reviewed_at` | timestamp, nullable | |
| `decision` | enum, nullable | `approved` \| `rejected` |

**Important rule:** when `va_roi` / `va_profit` / `va_cost_price` / `va_sale_price` are present on a lead (i.e. it came from the VA and was sourced via SAS on their end), those figures are treated as ground truth for profit/ROI. Keepa is used for everything else (price history, stability, competition, demand signals) but **never overrides VA-supplied profit numbers.** This applies to both OA and A2A leads.

For manually-entered ASINs with no VA profit data, profit/ROI can either be left blank (VA fills in later) or computed from Keepa as a rough estimate — flag clearly in the UI which case it is, so nobody mistakes a Keepa estimate for a verified SAS number.

---

## 3. Endpoints

### `POST /api/verdict`
Manual single-ASIN check (existing spec, unchanged).
Body: `{ "asin": "B0..." }`
→ Pulls Keepa, computes metrics, calls Claude for verdict + rationale, returns full result. Also writes a `leads` row with `source=manual`.

### `POST /api/webhook/sheet-lead`
Called by the Google Apps Script trigger the moment a VA adds a row.
Body: the row as JSON (ASIN + whatever other columns exist — passed through, not hardcoded to a fixed schema).
→ Inserts a `leads` row with `status=queued`, `source=sheet`, `raw_sheet_data` = full payload, and `va_roi`/`va_profit`/etc. populated from whichever columns match. Enqueues the lead for analysis (see §5 on rate limiting) rather than analyzing synchronously.

### `GET /api/leads?status=analyzed`
Returns the review queue — all leads with a verdict, awaiting a human decision.

### `POST /api/leads/{id}/review`
Body: `{ "decision": "approved" | "rejected" }`
→ Sets `status=reviewed`, records `decision` and `reviewed_at`.

---

## 4. Keepa metrics to compute/store (per ASIN)

**Profitability (Keepa-derived, used only when no VA figures present):**
- Net profit / unit, Net ROI

**Demand & competition:**
- 30 / 60 / 90 / 180-day ROI trend
- Buy box %, buy box potential
- Offer count — **split into total offers and FBA-specific offer count**
- Offer trend (rising/falling/saturating)
- Sales velocity (est. units/day)
- Rating + review count
- Amazon-selling flag (is Amazon on the listing)

**Price history / stability (the "watch tab" info — don't drop this):**
- Current lowest FBA price
- Price trend (stable/rising/falling)
- Price stability/volatility rating (derived from Keepa's min/max/avg per interval)
- Price drop count (last 30/90/180 days)
- Lowest price ever, lowest price in last 90 days
- Highest price / price range
- Out-of-stock frequency / current stock status (if available from Keepa)

---

## 5. Rate limiting and priority

Keepa refills only 22 tokens/minute, and this budget is shared with Atlas's other Keepa consumers (e.g. the A2A scan pipeline). The sheet webhook must **not** call Keepa synchronously on each row — a burst of VA-added leads would blow through the budget instantly. Instead:

- Webhook just inserts the `queued` row and returns immediately
- A background worker (simple polling loop or task queue) processes `queued` leads at a rate the token budget supports, updating each to `analyzed` as it completes
- Manual single-ASIN checks (from the UI) can jump the queue / run immediately since they're one-off and interactive

**Priority rule: verdict-checker and review-queue lead analysis take priority over other Keepa consumers, especially the A2A scan pipeline.** Concretely:

- Introduce a simple priority tier on whatever dispatches Keepa calls — e.g. `high` (manual ASIN checks, queued VA leads) vs `low` (bulk A2A scanning)
- When both have pending work, the token budget should be spent on `high` priority requests first each cycle; `low` priority (scans) only consumes leftover tokens
- A long-running scan should back off or pause mid-run if a high-priority request comes in, rather than finishing its current batch first, so a VA-submitted lead or a manual check isn't left waiting behind a scan that could take hours
- This likely means the scan pipeline needs to check/yield token budget in smaller increments (e.g. per-ASIN) rather than claiming a large batch of tokens upfront

---

## 6. Google Apps Script (trigger side)

Runs in the Google Sheet's own Apps Script editor. Outline:

```javascript
function onFormSubmit(e) {
  var row = e.values; // or read named columns from e.range
  var payload = {
    asin: row[COLUMN_INDEX_ASIN],
    // include all other columns as-is
  };
  UrlFetchApp.fetch('https://<your-atlas-domain>/api/webhook/sheet-lead', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload)
  });
}
```

Bind this as an installable trigger (`onFormSubmit` if leads come in via a linked Form, or `onEdit` if the VA types directly into the sheet — `onEdit` needs extra logic to detect "a new complete row" vs. mid-edit).

---

## 7. Frontend pages

**`/verdict`** — existing single-ASIN checker (input, verdict banner, metrics grid).

**`/review`** — the queue:
- List/table of `analyzed` leads: ASIN, verdict badge, source (manual/sheet), sourcing type (OA/A2A), profit/ROI (labelled clearly as "VA/SAS" or "Keepa estimate"), approve/reject buttons
- Clicking a row expands to the full metrics + rationale (same layout as `/verdict`)

**Main dashboard** — surface the review queue so it can't be missed:
- A visible count of leads with `status=analyzed` awaiting review (e.g. "5 VA leads waiting to be reviewed"), pulled from `GET /api/leads?status=analyzed`
- Should link straight through to `/review`
- Ideally distinguishes `sheet`-sourced leads from `manual` ones in the count/label, since sheet leads are the ones arriving unattended and most likely to pile up

---

## 8. Open decisions for Claude Code / you to confirm during build

- Exact column names/order in the VA's Google Sheet (needed to map `raw_sheet_data` fields to `va_roi` etc.)
- Background worker mechanism — simple cron/polling loop vs. a proper task queue (Celery/RQ), depending on what's already in Atlas
- Auth on the webhook endpoint (shared secret in the Apps Script payload, at minimum, since it's a public-ish URL)
