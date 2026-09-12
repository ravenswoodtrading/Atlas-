# Review notes for commit 7224018 ("Add VA reporting and implement approved scan queue workflow")

Written 2026-09-10 after a code review of the commit plus live testing of the Scan
Queue / Scan Intelligence pages against the running app. Every item below was
verified against the actual current file content, not just the diff. File paths
are relative to the repo root.

## Priority 0 — live, active right now

### The automated Scan Queue appears to be stalled/hung in production

Symptoms observed live today (2026-09-10):
- Every one of the 24 queued brands shows "Not recorded" for both "Products
  after filters" and "Full catalogue pass" — not one brand has ever completed a
  full catalogue traversal since this went live this morning.
- `ScanQueueRun` (the new audit table this commit added — one row per
  completed scan tick) has **zero rows** in the database.
- Only 2 of 25 `ScanCampaignProgress` rows exist at all (wacom, mattel), and
  both show a `last_attempt_at` timestamp from within the last ~10 minutes of
  checking, with no corresponding update to the brand's own `last_run_at` /
  `scanned_count` — i.e. the scan call for those two brands started and had
  not returned.
- The `scan_queue` entry in `SchedulerStatus` has repeatedly shown
  `"manual scan in progress"` as its last summary across multiple checks
  several minutes apart.
- Meanwhile the Token Usage page shows Scan Queue as the single biggest
  spender: 43,360 Keepa tokens today, 521,983 over the last 30 days — so real
  scanning activity (or at least real token spend) is definitely happening
  somewhere, it just isn't completing/recording normally.

**Likely mechanism**: `ScanQueueService._execute_scan_for_item`
(`backend/app/services/scan_queue_service.py`) calls
`BrandScanService.scan(...)` while holding `ScanCoordinator`'s lock (acquired
non-blocking in `run_next_tick` via `try_acquire_for_automated_tick()`). If
that `scan()` call hangs (no timeout on the underlying Keepa/network call, or
an unbounded retry loop), the lock is never released
(`release_after_automated_tick()` sits in a `finally` that can't run until the
call returns), so every subsequent tick's non-blocking acquire fails and
reports "manual scan in progress" — even though no genuine manual scan is
running. This would explain why the rotation essentially never gets past a
couple of brands.

**Ask Codex to**: find where `BrandScanService.scan()` (or whatever it calls
into `KeepaClient`/`ProductFinder`) could block indefinitely with no timeout,
add a bounded timeout, and confirm `ScanCoordinator`'s lock is always released
even if the scan raises or the thread is killed. Also worth adding a
"how long has the current holder had the lock" visibility so this doesn't
silently recur unnoticed.

## Priority 1 — confirmed correctness bugs (verified against current code)

1. **`delete_item` wipes every campaign for a brand, not just the one requested.**
   `backend/app/services/scan_queue_service.py:205` — `delete_item(item_id)`
   looks up the single `ScanQueueItem` by id, then does
   `db.query(ScanQueueItem).filter_by(brand=item.brand).delete(...)`, deleting
   every campaign for that brand. A brand can legitimately have multiple
   queue rows (different `category_ids`), per `add_item`'s own docstring.
   Fix: filter the delete by `id == item_id`, not `brand == item.brand`.

2. **`is_notable()` lost its unconditional "BUY recommendation" bypass.**
   `backend/app/services/product_repository.py:87` — used to
   `return recommendation == "BUY" or (has_sales_evidence and ...)`; now
   `return has_sales_evidence and (...)` with no BUY bypass at all. The
   `SALES_DROPS_NOTABLE_THRESHOLD` was also raised 3→12 in the same commit.
   The function's own docstring (unchanged) still says "...OR a BUY
   recommendation outright." Real callers: `SellerWatchService.
   list_notable_buyable`, the Dashboard BUY counter, Discord notifications.
   A product Atlas itself classifies BUY can now silently disappear from all
   three if it lacks confirmed sales evidence. Fix: restore the bypass, or
   confirm with whoever wrote this that dropping it was intentional and
   update the docstring either way.

3. **Replen re-check hardcoded to 24h instead of the weekly 168h.**
   `backend/app/main.py:170` — `ReplenService.check_stale(24)` — the two
   sibling calls in the same scheduler function
   (`WatchlistService.check_stale`, `ReviewQueueService.recheck_stale_items`)
   both still use `WEEKLY_RECHECK_STALE_HOURS` (168). No comment explains the
   deviation. Since this scheduler tick itself fires every 24h, a 24h
   threshold means Replen items are "stale" and re-checked on almost every
   tick — roughly 7x more Keepa spend than intended. Fix: use
   `WEEKLY_RECHECK_STALE_HOURS` here too, unless there's a real reason Replen
   needs a different cadence (in which case name it as its own constant with
   a comment).

4. **Scan rotation cursor freezes for the whole duration of any manual scan.**
   `backend/app/services/scan_queue_service.py:421` (`run_next_tick`) — the
   cursor advance (`settings.last_scan_queue_item_id = item.id`) moved to
   *after* the `ScanCoordinator.try_acquire_for_automated_tick()` check
   succeeds. A deleted comment on the old code explicitly warned: "even a
   skipped/errored tick should still advance the rotation past this item, or
   it would keep getting re-picked every tick." Now, whenever the acquire
   fails, the function returns early without ever touching the cursor, so
   `_next_round_robin_item` keeps resolving to the exact same brand every
   tick until whatever's holding the lock releases it. This directly compounds
   with the Priority 0 issue above — see the connection.

5. **Editing an already-analyzed VA lead leaves it permanently stale.**
   `backend/app/routes/leads.py:270` — `ingest_sheet_lead_row` only resets
   `status`/`verdict`/`rationale`/`keepa_metrics` (forcing re-analysis) when
   `is_new` is true. A matched *existing* lead whose sheet row content
   genuinely changed no longer gets this reset, even though
   `va_submission_sync.py` calls this exact function with `existing_lead` set
   specifically for that case. Since `lead_analysis_service.py` only picks up
   `status == "queued"` leads, an edited lead's price/cost/notes update but
   its old, now-inaccurate verdict/rationale are shown forever and it never
   re-enters the analysis queue. Fix: reset analysis state whenever the
   matched row's content actually changed, not only when the Lead is brand
   new.

6. **Webhook-created leads can end up duplicated by the new scheduled sync.**
   `backend/app/services/va_submission_sync.py:83` — `sheet_lead_webhook`
   (`backend/app/routes/leads.py`) creates Leads without ever writing a
   `SheetLeadSubmission` row (the new table the scheduled sync now uses as its
   sole "have we seen this before" source of truth). The scheduled sync can
   then fail to match that sheet row to any known submission, classify it
   "new", and call `ingest_sheet_lead_row(..., new_submission=True)` — which
   forces a brand-new `Lead` regardless of whether one already exists for
   that ASIN. Net effect: a lead that arrived via the (still-live, "kept alive
   in case Apps Script is revived") webhook can get duplicated on the next
   scheduled sync. Fix: either have the webhook also write a
   `SheetLeadSubmission` baseline row, or have the sync path fall back to an
   ASIN-based Lead lookup before assuming "new".

7. **Unparseable numbers in the Summary import silently become £0.**
   `backend/app/services/actual_performance_service.py:27` (`_number()`) —
   catches any parse failure and returns `0.0` with no error, no warning, no
   flag anywhere in the output. The Summary-import path (RoI/Margin/CoG) uses
   this with zero validation, while the sibling Daily-import path explicitly
   re-raises on the same class of bad input. A cell like `"25.5%"` or `"N/A"`
   silently zeroes out and can misclassify a genuinely good SKU as a
   financial failure (`financial_pass = roi_pct >= 25 or margin_pct >= 14`)
   in the VA actuals report, with nothing to catch it. Fix: either raise (like
   the Daily path does) or surface an explicit "unparsed"/"needs review" flag
   on the row instead of silently defaulting to 0.

8. **Worth confirming, may be intentional**: webhook can overwrite an
   already-decided lead's raw data. `backend/app/routes/leads.py:238` — the
   existing-lead lookup dropped its `Lead.decision.is_(None)` filter, so the
   still-live webhook can now match and update an already-DECIDED lead in
   place instead of only matching pending ones. A nearby comment frames this
   as possibly deliberate ("so a retrospective edit cannot reopen a decided
   lead") — worth asking whoever wrote it rather than assuming it's a bug.

## Priority 2 — real but lower-severity

9. **A new scheduler function reintroduces a full-table-scan pattern this
   same commit explicitly removed elsewhere.**
   `backend/app/services/scan_schedule_service.py:16` (`schedule_maps`) —
   runs two unfiltered, uncached full-table queries (`ScanBrandSchedule`,
   `ScanCampaignProgress`) on every ~60-second scheduler tick. This exact
   commit *deleted* `ScanQueueService._brand_tier_map()`'s 300-second TTL
   cache at the same call site, whose own removed comment said recomputing
   on every 60s tick is "pure overhead for no real gain." A comment in
   `scan_economics_service.py` still cites that old cache as the canonical
   pattern for this exact situation. Fix: give `schedule_maps` a short TTL
   cache the same way, or restructure the caller to avoid a full reload every
   tick.

10. **Mojibake — a few characters render as garbage.** Already partly fixed
    (commit `23c34eb`) for `£`, `·`, `→`, `—` in `scan_queue.html`,
    `scan_intelligence.html`, `scan_queue_service.py`, and
    `test_va_actuals.py`. **Still remaining**: `backend/app/templates/
    scan_intelligence.html:26` — the priority-emoji dict
    (`{"HIGH": ("success", "🔥"), "MEDIUM": ("warning", "🟡"), "LOW":
    ("secondary", "⚪")}`) is still double-encoded and renders as "ðŸ”¥ HIGH",
    "ðŸŸ¡ MEDIUM", "âšª LOW" on the live Scan Intelligence page. Same class of
    fix as the earlier commit — just replace the three literal emoji
    characters on that line.

## How this list was produced

8 independent review passes (line-by-line diff scan, removed-behavior audit,
cross-file caller/callee tracing, code-reuse check, simplification check,
efficiency check, root-cause/altitude check, and a CLAUDE.md conventions
check — none applied, no CLAUDE.md exists in this repo) surfaced ~50
candidate findings; each of the 9 numbered code findings above was then
independently re-verified by reading the actual current file content (not
just the diff) before being included here. The Priority 0 live-stall finding
came from directly querying the running app's database and Token Usage page,
not from reading code.

Not included here: a handful of lower-confidence duplication/simplification
notes (repeated date-parsing and currency-parsing logic across
`actual_performance_service.py`, `va_price_drop_audit.py`, and
`va_submission_sync.py`; a few copy-pasted ternary chains; some
inefficient-but-not-wrong per-row loops in the new reporting services). Happy
to write those up too if useful, but they're maintainability concerns rather
than bugs.

## Product decision (2026-09-10, Tamara) — simplify repeat-ASIN purchase matching

Separate from the bug list above: while reviewing the VA Actuals numbers,
Tamara decided how repeat-ASIN purchases should be handled, which resolves
the single biggest "can't be measured" cause found in that data (48 of 215
purchase batches, 22%).

**Current behaviour**: `backend/app/services/va_performance_service.py`,
`purchase_rows()` (~line 39). When a VA lead's own row has no explicit
purchase date, it tries to find exactly one matching Buy Sheet order for
that ASIN within the date window up to the next VA submission of the same
ASIN. If more than one Buy Sheet candidate falls in that window (and their
quantities don't cleanly disambiguate — see the `earliest`/`unique_candidates`
logic around lines 71-89), the batch is marked
`'Multiple Buy Sheet purchases need a date match'` and excluded from
ROI/profit/sell-through reporting entirely, rather than guessed at.

**Tamara's instruction**: "The repeat ASIN tracking shouldn't be an issue. If
the ASIN appears more than once just attribute to the item that sells first."
i.e. don't try to prove which specific VA write-up caused which specific Buy
Sheet order — treat repeat purchases of one ASIN as a single ordered queue
(by purchase date) and let sales absorb against that queue oldest-first, the
same FIFO convention `va_sales_allocation.py` already documents for
sales-to-batch allocation once a purchase date IS known. This needs no new
data entry from the VA or purchasing side — it's a change to how Atlas
resolves the match, not a new process.

**Ask Codex to**: work out the exact implementation with this in mind — most
likely, when a VA submission's purchase date can't be uniquely resolved via
the existing date-window match, fall back to pairing the Nth chronological
VA submission for that ASIN with the Nth chronological Buy Sheet order for
that ASIN (both sorted by date), rather than bailing out to "needs review."
Worth confirming with Tamara whether this should still fall back to
"needs review" in the genuinely unresolvable case (e.g. more submissions
than orders, or vice versa) or whether some other rule should apply there.

**Important caveat, also from Tamara**: a VA can submit a lead for an ASIN
that was already purchased earlier (e.g. a repeat/replenishment write-up on
something already in stock from a prior order). The ordinal/FIFO pairing
above must never match a submission to a Buy Sheet order — or attribute a
sale — that happened *before* that submission's own date. Concretely: when
resolving an ambiguous repeat-ASIN batch, only consider Buy Sheet orders (and
the sales that follow them) dated on or after the VA lead's own submission
date as eligible candidates for that lead. Otherwise the queue can silently
attribute an already-decided older purchase to a brand-new submission, which
runs the ordering backwards.
