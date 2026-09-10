# VA actuals — first implementation

Open `/reports/va/performance` after restarting Atlas. Its existing startup creates the two new reporting tables.

## Uploads

Use `/reports/uploads` to upload Seller Toolkit Sales Summary by SKU and Daily Sales by SKU together, ad hoc. Supply the full daily export start/end dates. Each upload replaces both exports and retained daily rows in one transaction; a failed import leaves the previous upload intact. Existing collapsed imports need re-uploading to supply detail. Use a complete date range beginning on or before the earliest VA purchase being assessed.

## Attribution and drill-down

Purchased VA Lead Sheet rows from April 2026 onward supply the quantity cap, expected Sale Price, Expected Profit per unit, Actual CoG/CoG (unit), Client Rating, Client Notes and VA Notes. Purchase dates come from an explicit date on the VA row, otherwise a unique Buy Sheet row with the same ASIN between submission and the next VA submission. Duplicate Buy Sheet headers use the first purchasing block.

Sales on or after purchase are allocated oldest purchase first within each ASIN, capped at purchased quantity. A daily export row can split across batches; its sales/profit totals are apportioned by units. Allocation across SKUs follows the agreed ASIN rule and can include existing stock. Each source row appears in expandable purchase details with the original export row number. A daily row is not an individual transaction.

Summary results exclude unresolved purchases, but those purchases remain visible with a reason. Missing or ambiguous dates, incomplete coverage and nonpositive unit rows (returns/adjustments) require review. An unresolved batch prevents uncertain allocation to other batches of the same ASIN.

The report shows expected vs actual selling price, expected profit on the allocated units vs actual allocated profit, first allocated sale, sell-through date/days and remaining unallocated units. Elapsed time ends at the export end date, not today. Remaining units are not Amazon inventory. The agreed target and product insights are described below; filters expose negative profit, below-expected prices, partial and completed batches.

## Known boundaries

Comments/ratings/expectations are current sheet values, not immutable historical originals. If the sheet was edited, earlier text cannot be reconstructed by this implementation. Returns cannot be linked to original transactions from these daily aggregates and are flagged rather than silently credited or subtracted. Same-day ordering is daily export row order, not an inferred transaction time. Currency amounts use the export's Sales and Profit_Loss fields as supplied.

## Verification

`python -m unittest test_va_actuals test_command_centre_counts -v`

Tests use an in-memory database, synthetic sales and template rendering. They cover FIFO/caps, pre-purchase exclusions, partial-row allocation, missing coverage, returns, ambiguous date matches, rollback, repeated imports, CSV/TSV handling, comment escaping and command centre/review queue count agreement. Live sheet data has not been verified. Browser layout and interactions were checked with synthetic examples as described below.

## Date periods and product insights

Choose all dates, a month, a quarter (`2026-Q2`) or an inclusive custom period. This selects VA purchase dates; sales continue through the uploaded coverage end. Allocation runs before filtering, so earlier purchases retain their sales. Undated batches are counted separately when a date filter is active.

The agreed target is full sell-through within 30 days from purchase AND either allocated ROI >=25% OR allocated profit margin >=14%. ROI is allocated Profit_Loss / allocated CoG; margin is allocated Profit_Loss / allocated Sales. Partial daily rows share all three values by quantity. Missing/zero costs are never treated as a proven zero ROI. A valid margin pass can still satisfy the OR rule. Target categories distinguish met, still within 30 days, late sell-through, below both financial targets and insufficient data.

Top 10 is by ASIN across the selected purchases. All selected batches must meet the target. Sort order is shortest maximum batch sell-through, then price uplift over expected, then profit. Problem products show late/unallocated units, below-plan prices and financial misses. Replenishments require Buy Sheet quantity evidence for the initial batch and later dated purchases of the same ASIN through the export end. Later orders are displayed individually and do not enlarge the original batch's sales allocation.

## Shared Report Uploads

`/reports/uploads` is the single upload page, linked under Reporting. It shows each report's last successful upload time (UTC), filename, row count and coverage. Both files and their metadata update atomically. The VA and overall actuals pages use the same database dataset; they do not offer file inputs. An additive startup migration preserves older sales rows while adding nullable cost detail. Re-upload older exports to supply CoG.

SellerToolKit source: https://sellertoolkit.com/ links to https://app.sellertoolkit.co.uk/. Exact report menu labels could not be verified in an authenticated account. The upload page explicitly identifies its names as descriptive and gives required groupings, columns and export scope rather than inventing a click path.

Validation now includes 17 offline tests and browser checks using `preview_va_actuals.py` with synthetic data only: target-card filtering, top-product purchase drill-down, month filtering and the central upload page. Live business data has not been refreshed or replaced during these checks.

## Profit comparison and overall returns

All financial summary metrics follow the selected purchase cohort. May includes only VA leads purchased in May, with their allocated sales through the latest uploaded export date, including June or later sales. Sales from April purchases are excluded even if sold in May. FIFO allocation runs across all purchases before the cohort is selected.

Expected sold profit is VA expected profit per unit multiplied by allocated units sold. Actual sold profit follows the same units. Percentage difference is (actual minus expected) / expected × 100, available only when total expected profit is positive. Overall ROI is total actual sold profit / total allocated sold cost × 100; margin is total actual sold profit / total allocated sales revenue × 100. These are ratios of totals, not averages of lead percentages. Unsold units do not contribute. Missing expectations disable the overall profit comparison; missing or zero sold costs disable overall ROI. The report shows the affected unit counts.
