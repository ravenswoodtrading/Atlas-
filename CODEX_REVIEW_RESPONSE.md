# Assessment and repair plan — 10 September 2026

Reviewed CODEX_REVIEW_NOTES.md against current code, read-only live SQLite records, and isolated in-memory reproductions. No application changes or live scans were made during this assessment.

## Assessment

| Finding | Verdict | Recommended treatment |
|---|---|---|
| P0 scan stall | Serious throughput/observability problem; permanent hang and proposed mechanism not established | Investigate first; instrument lock ownership and stages, then bound work safely |
| 1 whole-brand removal | Intentional for the agreed one-row-per-brand UI | Retain behaviour; rename service method to remove_brand and distinguish any future campaign-only control |
| 2 BUY bypass removed | Confirmed behaviour change, with stale documentation; not enough evidence to call the stricter qualification a bug | Preserve until business-rule audit confirms intended Buy Now/Verify mapping; update documentation and tests together |
| 3 daily Replen | Confirmed 24h cadence; consistent with requested regular Replen scanning, not automatically an error | Give it a named daily interval and separate explanation; do not silently restore weekly cadence |
| 4 cursor held while busy | Intentional: no scan ran, so the waiting brand retains its turn | Retain; advancing the cursor cannot release the lock or restore scanning |
| 5 stale edited lead | Reproduced for analyzed, undecided leads | Requeue when analysis-relevant fields change; preserve decisions and historical decision evidence |
| 6 webhook duplication | Reproduced after submission tracking was initialized | Unify webhook and scheduled submission identity; avoid ASIN-only matching, which would merge legitimate repeat submissions |
| 7 silent numeric zeroing | Reproduced | Strict field-aware parsing, finite-number validation, row/column errors, atomic import preservation |
| 8 decided lead edits | Intentional decision preservation, but a genuine evidence consistency risk | Keep decision-time inputs/verdict as an immutable snapshot and flag later source changes; never silently reopen a decided lead |
| 9 schedule queries | Two small reads confirmed; no demonstrated performance problem | Scope to active campaigns if needed; avoid caching rapidly changing progress without invalidation |
| 10 broken characters | Confirmed in Scan Intelligence tier symbols | Fix UTF-8 handling and verify rendered output |

## Live scan evidence

At approximately 20:19 UTC, there were 25 queue campaigns, two progress rows, and one completed scan run. The matching attempt began at 19:52:08 and completed at 20:09:37: 98 products checked, 16 opportunities. The latest scheduler message at 20:18:37 was still "manual scan in progress". Zero completed runs is therefore no longer current, although throughput remains concerning.

The scheduler awaits its own worker before scheduling another tick. One instance cannot both remain blocked inside that worker and repeatedly publish subsequent busy ticks. Other automated jobs, high-priority waiters, manual work, or multiple application processes can explain contention; the current message does not identify which.

ScanCoordinator has no owner or held-since metadata. Ordinary scan exceptions already release its lock via finally. Cancelling an asyncio waiter does not stop the underlying synchronous worker; force-releasing that worker's lock could allow overlapping scans. Arbitrary thread termination cannot be made safe by promising finally will always run.

The locally installed test copy of Keepa has a default 10-second HTTP timeout and an unbounded token-refill retry when wait=True. Product queries and Product Finder normally pass wait=False; Product Finder's TypeError fallback omits that flag. The running app's actual installed version still needs verification. HTTP timeouts alone are not whole-scan deadlines.

BrandScanService also iterates ASINs and up to four EU markets using serial SP-API calls without an overall deadline or priority-yield check inside that phase. This is a credible source of long lock holds, not yet a proven attribution of the observed 17-minute run.

## Repair order and acceptance checks

1. **Diagnose and bound scan work.** Record lock owner/job, acquisition time, waiting callers, current stage, processed products and last progress. Persist started/completed/failed/deferred outcomes. Check running process count and the live dependency version. Add finite request/retry limits plus cooperative deadlines and priority yields between product/market calls. Checkpoint partial work before yielding; preserve catalogue coverage and avoid skipping or double-paying for unfinished products. If hard termination is required, isolate work in a process and confirm it has exited before another scan starts. Verify ordinary exceptions release the lock, stalled calls expire, priority work receives a turn, and several brands complete ticks without overlapping scans.

2. **Repair lead identity and analysis freshness together.** Both transports must resolve the same submission transactionally, while allowing real repeat submissions of the same ASIN. Only meaningful analysis-input edits requeue undecided leads; audit-only edits do not. Preserve decided-lead snapshots and show subsequent edits separately. Test webhook-before-sync, sync-before-webhook, replay, edited dates, repeat submissions, changed costs, unchanged inputs and decided leads.

3. **Make Summary imports strict.** Accept explicitly supported currency/percentage formatting; reject invalid/non-finite values and non-whole counts with row/column messages. Never convert missing financial evidence to zero. Validate everything before replacing existing imports. Test malformed percentages, N/A, NaN/infinity, blank values and failed-import rollback. Audit existing saved imports for affected values and re-import from source where necessary.

4. **Resolve documentation/UI mismatches.** Fix encoding; rename brand-removal semantics; name the daily Replen constant; document the intended BUY/Verify rules after checking their approval history. Keep the agreed brand removal and cursor behaviour. Add targeted regression checks for each confirmed fix. Only optimize schedule reads if measurements justify it.

The existing suite missed these cross-path cases. New regressions should first demonstrate each reproduced failure, then pass after the corresponding fix. No production rules should be reverted solely because an older comment describes previous behaviour.
