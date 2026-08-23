from datetime import datetime, timezone

from sqlalchemy import String, Float, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProductRecord(Base):
    """
    A saved snapshot of one opportunity from a scan -- one row per
    (ASIN, scan run). This is what lets the Products page show
    history across restarts without re-querying Keepa, and gives you
    a record of what a product looked like when you saw it, even if
    the numbers change later.

    NOTE: this only saves opportunities that made it into a scan's
    results list (i.e. had a real UK price and a real EU source) --
    excluded/filtered-out products are not persisted.
    """
    __tablename__ = "product_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    asin: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")

    # First EAN Keepa has on file, or "" -- see Product.ean/KeepaParser.ean
    # for why this is reference/display only. Used by the Competitors
    # page's OA search links (title + EAN gives a more precise match
    # than title alone) -- "" for any record scanned before this
    # column existed, same backfill-via-rescan pattern as other columns.
    ean: Mapped[str] = mapped_column(String, default="")

    # What brand search produced this record, e.g. "philips"
    brand_query: Mapped[str] = mapped_column(String, default="")

    buy_box_now: Mapped[float] = mapped_column(Float, default=0.0)
    buy_box_90d: Mapped[float] = mapped_column(Float, default=0.0)

    best_source_marketplace: Mapped[str] = mapped_column(String, default="")
    best_source_cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    fba_fee: Mapped[float] = mapped_column(Float, default=0.0)
    referral_fee: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)

    # Same as profit/roi but using the 90-day typical UK price instead
    # of today's -- catches opportunities where today's price is a
    # temporary discount. See ScoringEngine/FeeEngine for how these
    # are used together with profit/roi.
    profit_90d: Mapped[float] = mapped_column(Float, default=0.0)
    roi_90d: Mapped[float] = mapped_column(Float, default=0.0)

    # Human-readable category name and the referral/VAT rates FeeEngine
    # actually applied for this record -- see Product.category_name /
    # FeeEngine.calculate. Lets the UI explain a given roi/profit
    # instead of just displaying it.
    category_name: Mapped[str] = mapped_column(String, default="")
    referral_rate_used: Mapped[float] = mapped_column(Float, default=0.0)
    uk_vat_rate_used: Mapped[float] = mapped_column(Float, default=0.0)
    eu_vat_rate_used: Mapped[float] = mapped_column(Float, default=0.0)

    score: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    recommendation: Mapped[str] = mapped_column(String, default="")

    # Full report (trend + score_breakdown + confidence_breakdown) as
    # JSON text -- lets the Products page show exactly why a score was
    # given, as it was AT SCAN TIME, without needing to re-run scoring
    # logic later (which could drift if the scoring rules change).
    report_json: Mapped[str] = mapped_column(String, default="")

    # Keepa's confirmed monthly sales count (real Amazon sales data,
    # not an estimate). 0 means Keepa has no confirmed figure, not
    # necessarily that the product doesn't sell.
    monthly_sales: Mapped[int] = mapped_column(Integer, default=0)

    # When Keepa last updated monthly_sales -- can be well in the past.
    # Nullable: None means Keepa has never had a confirmed figure.
    monthly_sales_as_of: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )

    # Keepa's sales-rank-drop count over the last 30 days
    # (salesRankDrops30) -- each drop is Keepa's own proxy for "a sale
    # probably happened". Used as secondary evidence of sales velocity
    # when monthly_sales has no confirmed figure -- see
    # ProductRepository.is_notable.
    sales_drops_30d: Mapped[int] = mapped_column(Integer, default=0)

    scanned_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # "up", "down", or NULL/None (not reviewed yet). Set via the
    # thumbs buttons on Discovery/Products/Watchlist -- deliberately
    # tied to this specific scan snapshot, not the ASIN generally, so
    # a re-scan with fresh numbers naturally comes back unreviewed
    # rather than carrying over a verdict based on stale data.
    review: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Optional free-text "why not" captured alongside a "down" review
    # (2026-08-23, sourcing-agent brief section 8.4) -- the raw
    # material a future rejection-retrieval/pattern-review step reads
    # back, so a reject isn't just a bare verdict with no record of
    # what was actually wrong with the lead.
    review_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class KnownProduct(Base):
    """
    Static catalog metadata imported from a Keepa CSV export (e.g. a
    Product Finder export downloaded manually) -- ASIN, title, brand,
    category, hazmat/adult flags. This does NOT include price/rank
    data, which changes constantly and would go stale; only fields
    that stay true regardless of when you look.

    The point: this lets category/brand exclusion checks happen
    BEFORE spending a single Keepa token, for any ASIN that's been
    imported this way -- not just after the UK lookup like before.
    """
    __tablename__ = "known_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    manufacturer: Mapped[str] = mapped_column(String, default="")

    category_root: Mapped[str] = mapped_column(String, default="")
    category_sub: Mapped[str] = mapped_column(String, default="")
    category_tree: Mapped[str] = mapped_column(String, default="")

    model: Mapped[str] = mapped_column(String, default="")
    ean: Mapped[str] = mapped_column(String, default="")
    upc: Mapped[str] = mapped_column(String, default="")

    is_hazmat: Mapped[bool] = mapped_column(default=False)
    adult_product: Mapped[bool] = mapped_column(default=False)

    imported_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class WatchedProduct(Base):
    """
    A product the user has explicitly marked as interesting, to track
    over time. Independent of any particular scan/brand -- watched
    products can be re-checked anytime via the Watchlist page, which
    reuses the same ASIN-scan pipeline as file uploads.
    """
    __tablename__ = "watched_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(String, default="")

    watched_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ExcludedProduct(Base):
    """
    A specific ASIN the user has explicitly marked as not interesting,
    checked BEFORE spending any tokens on future scans -- same idea as
    the static exclusions.py file, but user-controlled from the page
    itself rather than requiring a code edit.
    """
    __tablename__ = "excluded_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(String, default="")

    excluded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ExcludedCategory(Base):
    """
    A whole category the user has explicitly marked as not
    interesting -- same idea as ExcludedProduct, but for a category
    instead of one ASIN. Checked the same "before spending tokens"
    way as the static EXCLUDED_CATEGORIES/EXCLUDED_CATEGORY_NAMES sets
    in app/config/exclusions.py, just user-controlled from the
    Exclusions page instead of requiring a code edit + restart.

    category_id is Keepa's numeric category ID, matched against a
    live product's categoryTree (see is_excluded()). category_name is
    the human-readable name, matched against known_products' imported
    CSV category text (see is_excluded_by_name()) -- those are two
    different identifier spaces the rest of the app already keeps
    separate, so a row can carry either or both, whichever the user
    actually has on hand when excluding it (e.g. an ID copied from the
    /categories page, or just a name typed by hand).
    """
    __tablename__ = "excluded_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    category_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    category_name: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    reason: Mapped[str] = mapped_column(String, default="")

    excluded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class GatedBrand(Base):
    """
    A brand (optionally scoped to one category) Atlas is currently
    gated on -- can't get Amazon approval to sell, even though the
    product itself might be a genuinely good A2A opportunity. This is
    the DB-backed, user-editable companion to the static
    GATED_BRAND_CATEGORIES set in app/config/exclusions.py (see
    is_gated()), same relationship ExcludedCategory has to
    EXCLUDED_CATEGORIES -- edit from the Exclusions page instead of a
    code change + restart.

    Deliberately a SEPARATE table from ExcludedCategory/ExcludedProduct
    rather than reusing them, because a gated brand is handled
    differently everywhere it's checked: it blocks brand-search-DRIVEN
    scanning entirely (BrandScanService.scan Step 1 -- no point
    spending tokens hunting for MORE of a brand you can't sell), but
    an incidentally-discovered ASIN (e.g. via Competitor Watch) is NOT
    dropped the way a real exclusion is (Step 3) -- it still gets
    fully priced and scored, just tagged Product.gated=True /
    recommendation="GATED", so a strong opportunity from a gated brand
    is tracked as a data point for deciding whether pursuing ungating
    is worth it (see the Gated Brand Opportunities page). A plain
    exclusion has no such "still track it" behaviour, so folding this
    into ExcludedCategory would have meant overloading one row shape
    with two different meanings.

    category_id/category_name work exactly like ExcludedCategory's --
    leave both blank to gate the WHOLE brand (e.g. "gated on HP,
    period"), or set one to gate only that category (e.g. gated on
    Philips lighting specifically, but fine for Philips kitchen
    appliances).
    """
    __tablename__ = "gated_brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    brand: Mapped[str] = mapped_column(String)
    category_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    category_name: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    reason: Mapped[str] = mapped_column(String, default="")

    gated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ScanQueueItem(Base):
    """
    One entry in the automated scan queue -- a brand (optionally
    category-filtered) with a target number of products to work
    through, scanned a page at a time by the background scheduler
    (see ScanQueueService) in strict priority order (lowest `position`
    first). Progress persists across app restarts and across however
    many ticks it takes to reach target_count.
    """
    __tablename__ = "scan_queue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    brand: Mapped[str] = mapped_column(String)

    # Comma-separated Keepa root category IDs, or "" for no filter --
    # matches how ProductFinder.find_brand expects category_ids.
    category_ids: Mapped[str] = mapped_column(String, default="")

    target_count: Mapped[int] = mapped_column(Integer)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)

    # Next Product Finder page to fetch for this brand/category combo
    # -- advances by 1 each tick so repeated runs walk deeper into the
    # catalog instead of only ever seeing the first ~100 results.
    next_page: Mapped[int] = mapped_column(Integer, default=0)

    # Lower position = higher priority = scanned first. The scheduler
    # always works the lowest-position non-"done" item to completion
    # (or exhaustion) before moving to the next one.
    position: Mapped[int] = mapped_column(Integer, default=0)

    # "pending" (not started yet), "in_progress", "done" (hit
    # target_count OR the brand/category ran out of new results).
    status: Mapped[str] = mapped_column(String, default="pending")

    # Set when status becomes "done" without reaching target_count --
    # i.e. the brand/category genuinely has no more results, not that
    # the user chose to stop it. Lets the status page distinguish
    # "reached your target" from "ran out of catalog".
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class AutomationSettings(Base):
    """
    Singleton (always id=1) row holding small app-wide settings that
    don't belong to any single record -- the scan queue scheduler's
    global pause switch, plus (despite the name) the Dashboard star-buy
    counter's reset point. Not worth a separate table for one more
    nullable field.
    """
    __tablename__ = "automation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    # Dashboard's "unreviewed star buys" count only considers scans
    # AFTER this timestamp -- None means "since forever" (no reset
    # applied yet). Lets old/historical star buys the user doesn't
    # intend to review be cleared from the count without touching
    # their `review` field (which represents an actual up/down
    # verdict, not "seen for counting purposes").
    star_buys_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # Round-robin cursor for ScanQueueService.run_next_tick -- the id
    # of the ScanQueueItem processed on the LAST tick, so the next
    # tick can advance to the next not-done item in position order
    # instead of always restarting from the highest-priority item.
    # Without this, a single high-priority item with a large
    # target_count would occupy every tick until it finished (or the
    # catalog ran out), starving every other queued brand for
    # potentially days -- see run_next_tick's docstring.
    last_scan_queue_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class NotifiedOpportunity(Base):
    """
    Tracks which ASINs have already triggered a Discord ping (see
    DiscordNotifier) -- purely for de-duplication, not a scan record
    itself. The continuous Scan Queue rescans the same ASINs
    constantly, so without this a still-profitable product would ping
    again every single tick forever. Keyed by ASIN (not tied to any
    one ProductRecord row, which gets a brand new row on every rescan)
    so de-dup persists across rescans the way a single flag on a scan
    row couldn't.

    Brand new table -- created automatically by Base.metadata.
    create_all() on next app startup, same as any other new table.
    No migrate_db.py run needed (that script is only for adding a
    column to an EXISTING table).
    """
    __tablename__ = "notified_opportunities"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    first_notified_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_notified_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ReplenItem(Base):
    """
    One ASIN worth watching for a repeat buy -- either imported from a
    join of the purchase history buy sheet (where it was sourced) and
    Seller Toolkit's actual sales export (the real ROI it achieved,
    not just what was projected at purchase time), or added manually.

    "Check now" (see ReplenService) re-prices it via the normal scan
    pipeline and records the CURRENT ROI/profit, which is what
    actually drives the buy-again decision -- achieved_roi is proof
    it's a genuine past winner, not the trigger itself.
    """
    __tablename__ = "replen_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    asin: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")

    # Where it was bought before, e.g. "Amazon.de", "Amazon.co.uk", or
    # "Manual" if added directly rather than via import.
    source_store: Mapped[str] = mapped_column(String, default="")

    # Real achieved figures from Seller Toolkit's actual sales data --
    # None for a manually-added item with no purchase/sales history.
    achieved_roi: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    achieved_units: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # Populated by the most recent "check now" -- None until checked
    # at least once.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    current_roi: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    current_profit: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    current_source_marketplace: Mapped[str] = mapped_column(String, default="")

    notes: Mapped[str] = mapped_column(String, default="")

    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Lead(Base):
    """
    A single ASIN worth a BUY/WATCH/AVOID verdict -- created either by
    a manual /verdict check or by the Google Sheet webhook a VA's row
    lands in (see LeadAnalysisService). `raw_sheet_data` preserves the
    full sheet row as submitted, even for columns Atlas doesn't map to
    a named field, so nothing a VA entered is ever silently dropped.

    va_roi/va_profit/va_cost_price/va_sale_price are ground truth when
    present (SAS-verified, from the VA's own row) -- Keepa is used for
    everything else but must never recompute/override these. They're
    None for a manual check with no cost supplied, or a sheet row
    where SAS didn't populate that column.
    """
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    asin: Mapped[str] = mapped_column(String, index=True)

    # "manual" | "sheet"
    source: Mapped[str] = mapped_column(String)

    # "OA" | "A2A", or NULL if not known/provided
    sourcing_type: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Full sheet row as submitted, JSON text -- NULL for manual leads.
    raw_sheet_data: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    va_roi: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    va_profit: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    va_cost_price: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    va_sale_price: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)

    # "queued" -> "analyzed" -> "reviewed"
    status: Mapped[str] = mapped_column(String, default="queued")

    # "BUY" | "WATCH" | "AVOID", NULL until analyzed (or if analysis
    # never succeeded -- see analysis_attempts).
    verdict: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Full Keepa-derived metric set (see VerdictService), JSON text.
    keepa_metrics: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # How many times LeadAnalysisService has tried and failed to
    # analyze this lead (e.g. Keepa has no data for the ASIN) -- caps
    # retries at MAX_ANALYSIS_ATTEMPTS so a permanently-bad ASIN can't
    # loop forever; once the cap is hit the lead is still flipped to
    # "analyzed" (verdict=None, rationale explains the failure) so a
    # human sees it instead of it vanishing from the queue silently.
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0)

    # "approved" | "rejected", NULL until reviewed
    decision: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Optional free-text "why not" captured alongside a "rejected"
    # decision -- same purpose as ProductRecord.review_reason, see its
    # own comment.
    decision_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class TrackedSeller(Base):
    """
    A competitor's Amazon seller ID being watched via Keepa's
    storefront lookup (see SellerWatchService). `last_asin_snapshot`
    is the seller's full inventory ASIN list as of the last check --
    stored so the next check can diff against it to find NEW listings,
    without needing a separate history table just for that.
    """
    __tablename__ = "tracked_sellers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    seller_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String, default="")

    # Pause without losing history -- SellerWatchService only checks
    # sellers where this is true, but past seller_new_listings rows
    # stay untouched either way.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # JSON list of ASINs, e.g. '["B0EXAMPLE1", "B0EXAMPLE2"]'. Empty
    # string (not "[]") until the first check, same "blank means never
    # populated yet" convention as ProductRecord.report_json.
    last_asin_snapshot: Mapped[str] = mapped_column(String, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class SellerNewListing(Base):
    """
    One detection event: a tracked seller's storefront gained an ASIN
    it didn't have at the previous check. `sourcing_tag` starts NULL
    (SellerWatchService writes the row before SourcingClassifier runs
    against it) and gets filled in once classified -- see
    SourcingClassifier for the possible tag values and what each means.
    """
    __tablename__ = "seller_new_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tracked_seller_id: Mapped[int] = mapped_column(ForeignKey("tracked_sellers.id"), index=True)
    asin: Mapped[str] = mapped_column(String, index=True)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Set once the ASIN has been run through the standard pricing/
    # scoring pipeline -- links to the full "Why?" breakdown
    # (ProductRecord.report_json) rather than duplicating it here.
    # Nullable: a detection can exist before scoring completes (e.g.
    # if it got excluded/zero-token-filtered before ever producing a
    # ProductRecord).
    product_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_records.id"), nullable=True, default=None
    )

    # "EU A2A" / "UK A2A" / "Wholesale (likely)" / "OA / unclear" --
    # NULL until SourcingClassifier has actually run (see build order:
    # detection/diffing ships before classification does).
    sourcing_tag: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # True if the underlying spread/dip looks live RIGHT NOW, not just
    # historically -- the single highest-value flag on the Competitors
    # page. Meaningless (False) until classified.
    currently_buyable: Mapped[bool] = mapped_column(Boolean, default=False)

    # The numbers behind sourcing_tag (spread %, dip %, seller count,
    # etc, depending on which tag matched) as JSON text -- so the "why"
    # is inspectable, not just the label. Same idea as
    # ProductRecord.report_json.
    sourcing_reasoning_json: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Soft delete -- lets the user clear a detection from the feed
    # without losing the historical record.
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    # "up", "down", or NULL/None (not reviewed yet) -- same semantics
    # as ProductRecord.review, but scoped to THIS detection event, not
    # the product generally (a competitor listing something is a
    # separate thing to judge from whether the product itself is a
    # good buy) -- and works even when product_record_id is None (a
    # detection that never made it through scoring still gets its own
    # reviewable row).
    review: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Optional free-text "why not" captured alongside a "down" review --
    # same purpose as ProductRecord.review_reason, see its own comment.
    review_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # When this row was last run through SellerWatchService.reclassify_all()
    # -- separate from detected_at (fixed at first detection). NULL means
    # never reclassified since that backfill was added. Lets reclassify_all
    # resume from wherever it left off (oldest/never-done first) across
    # repeated calls, since one call only gets as far as the Keepa token
    # budget allows.
    sourcing_reclassified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )


class SignalQuery(Base):
    """
    A saved "find opportunities like this" definition -- e.g. "Stock-outs
    in Kitchen + Electronics". Run on demand from the Signals page's
    "Run now" button for now (see SignalService.run_check) -- deliberately
    NOT wired to an automatic scheduler yet, since the underlying Keepa
    Product Finder filters for stock_out/price_spike couldn't be
    verified against a live account from this environment; see the
    project doc for what to check before automating.

    signal_type: "stock_out" | "price_spike" | "ceiling_recheck". The
    first two run a category-scoped Keepa Product Finder query
    (ProductFinder.find_signal_candidates); ceiling_recheck needs no
    Product Finder query at all -- it iterates Atlas's own
    CeilingRejected pool instead (see that table).

    category_ids: comma-separated Keepa root category IDs, "" = no
    restriction -- same convention as ScanQueueItem.category_ids.
    Ignored entirely by ceiling_recheck (that iterates whichever
    categories CeilingRejected rows already carry from when they were
    first rejected, not a fresh category filter).

    last_match_snapshot: JSON list of ASINs the last Product-Finder-
    based run matched -- diffed against the NEXT run's result so only
    NEWLY-appearing ASINs get a lookup/saved match, same "only look at
    what changed" idea as TrackedSeller.last_asin_snapshot for
    Competitor Watch. Not used by ceiling_recheck.
    """
    __tablename__ = "signal_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String)
    signal_type: Mapped[str] = mapped_column(String)
    category_ids: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_match_snapshot: Mapped[str] = mapped_column(String, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class SignalMatch(Base):
    """
    One surfaced candidate from a SignalQuery run -- a lightweight,
    UNSCORED lead. Deliberately only ever costs a single UK-only Keepa
    lookup per candidate -- no EU marketplace tokens are spent building
    this list at all (see the Signals page / project doc). Watching one
    (the existing WatchedProduct mechanism) is what triggers Atlas to
    spend the full 5-marketplace check, on the user's own say-so.

    target_buy_price_gbp: the ceiling price (at SignalService.
    TARGET_ROI_PCT, currently 25%) worth paying for stock -- computed
    the same way FeeEngine.max_source_cost already powers the
    Competitors page's OA price guide. The number to take into Keepa
    or SAS and check a real source against, not a confirmed opportunity.

    buy_box_now: the reference UK price used for the target-buy-price
    calculation -- today's live price for price_spike/ceiling_recheck,
    but the 90-day-typical price for stock_out (there IS no current
    price on a stock_out match by definition; see SignalService).

    eu_history_json: a free enrichment from Atlas's OWN past scan data
    (see ProductRepository.get_last_eu_check) -- not a fresh EU lookup.
    "" if Atlas has never scanned this ASIN before.

    signal_reasoning_json: the raw numbers behind why this matched, so
    the "why" is inspectable -- same idea as SellerNewListing.
    sourcing_reasoning_json.
    """
    __tablename__ = "signal_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    signal_query_id: Mapped[int] = mapped_column(ForeignKey("signal_queries.id"), index=True)
    # Denormalized copy of the parent query's signal_type, so the
    # Signals page can filter/count by type with a plain column filter
    # instead of a join on every page load.
    signal_type: Mapped[str] = mapped_column(String)

    asin: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    category_name: Mapped[str] = mapped_column(String, default="")

    buy_box_now: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_sales: Mapped[int] = mapped_column(Integer, default=0)
    sales_drops_30d: Mapped[int] = mapped_column(Integer, default=0)

    target_buy_price_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    signal_reasoning_json: Mapped[str] = mapped_column(String, default="")
    eu_history_json: Mapped[str] = mapped_column(String, default="")

    detected_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Soft-hide, same convention as SellerNewListing.dismissed.
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)


class CeilingRejected(Base):
    """
    One ASIN whose UK price, the last time ANY scan looked at it,
    wasn't even enough to clear Amazon's own fees (BrandScanService.
    scan Step 3b's ceiling check) -- upserted here BY ASIN (one row per
    ASIN, not one per scan) so the ceiling_recheck SignalQuery can
    periodically ask "has this graduated since we last said no", using
    a price Atlas already fetched as part of a normal scan rather than
    a new Product Finder query. See SignalService.run_check.

    Populated from BrandScanService.scan's Step 3b for EVERY caller
    (Discovery, Scan Queue, Watchlist, Replen, Competitor Watch, an
    uploaded list) -- costs nothing extra, it's just persisting data
    that scan already paid Keepa tokens for and would otherwise throw
    away. Removed once it clears the ceiling and becomes a SignalMatch
    (signal_type="ceiling_recheck") -- no point re-checking something
    that's already been surfaced -- or once a later scan finds it DOES
    clear the ceiling through the normal pipeline (see Step 3b).
    """
    __tablename__ = "ceiling_rejected"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    # Keepa root category ID -- for the is_excluded/is_gated recheck
    # before spending a token refreshing this row.
    category: Mapped[str] = mapped_column(String, default="")
    category_name: Mapped[str] = mapped_column(String, default="")
    fba_fee: Mapped[float] = mapped_column(Float, default=0.0)

    buy_box_at_reject: Mapped[float] = mapped_column(Float, default=0.0)

    first_rejected_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class OaSourceRun(Base):
    """
    One batch execution of the OA Source Discovery pipeline (see
    OaSourceDiscoveryService) -- tracks the run-level metrics the
    user's own MVP spec asked to measure across a batch of "OA /
    unclear" competitor ASINs (see SellerWatchService.SOURCING_TAG_BY_TAB):
    how many were targeted/actually searched, how many turned up a
    candidate UK retailer, how many of those were an exact EAN/MPN
    match (not just a similar title), how many turned out genuinely
    profitable once a price was confirmed, and the estimated Brave
    Search API cost for the run (search_count is exact -- every Brave
    call this run made, discovery + verification together --
    estimated_cost_usd is an ESTIMATE against Brave's stated $5/1000
    pricing, not a real billing figure).

    asins_profitable counts candidates that were both PRICED (either
    automatically via Google Shopping, or manually confirmed/overridden
    by the user -- see OaSourceCandidate.price_source) AND actually
    promoted into a real Review Queue lead (cleared both
    TRUSTED_MATCH_TIERS and FeeEngine.OA_TARGET_ROI_PCT -- see
    OaSourceDiscoveryService._promote_if_qualifying). Since a Google
    Shopping auto-price can now happen during run_batch itself
    (2026-08-19), this can be non-zero immediately after a run
    completes, not only after the user later works through results
    manually via update_candidate.
    """
    __tablename__ = "oa_source_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # "running" | "done" | "error" -- lets the page show a run still
    # in progress without the request having to stay open.
    status: Mapped[str] = mapped_column(String, default="running")
    error: Mapped[str] = mapped_column(String, default="")

    asins_targeted: Mapped[int] = mapped_column(Integer, default=0)
    asins_searched: Mapped[int] = mapped_column(Integer, default=0)
    asins_with_retailer: Mapped[int] = mapped_column(Integer, default=0)
    asins_with_exact_match: Mapped[int] = mapped_column(Integer, default=0)
    asins_profitable: Mapped[int] = mapped_column(Integer, default=0)

    search_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Google Shopping searches this run made via SerpApi (one per ASIN
    # -- see OaSourceDiscoveryService.run_batch, 2026-08-19), counted
    # separately from Brave's search_count/estimated_cost_usd since
    # SerpApi bills per-account monthly quota, not per-1000-searches.
    # serpapi_searches_left is a snapshot of the account's remaining
    # quota taken once at the END of the run (get_account_status()
    # doesn't cost a search credit) -- shown so the user notices before
    # a batch run fails partway through a future month for running out.
    serpapi_search_count: Mapped[int] = mapped_column(Integer, default=0)
    serpapi_searches_left: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # True when this run stopped calling SerpApi partway through
    # because remaining account quota dropped to/below
    # SERPAPI_QUOTA_SAFETY_BUFFER (see oa_source_discovery_service.py,
    # 2026-08-19) -- every ASIN still left in the batch after that point
    # fell back to the free Brave pipeline instead of SerpApi. Surfaced
    # on the results page so a small/no auto-priced count on a big
    # batch is explained rather than looking like SerpApi just stopped
    # working.
    serpapi_quota_stopped: Mapped[bool] = mapped_column(Boolean, default=False)

    # How many ASINs got an automatically-priced, trusted-match-tier
    # Google Shopping result this run (a subset of asins_with_retailer
    # -- Brave-only finds still need a manual price confirm and aren't
    # counted here). Distinct from asins_profitable (see below), which
    # additionally requires clearing OA_TARGET_ROI_PCT.
    asins_auto_priced: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class OaSourceCandidate(Base):
    """
    One ASIN's result from a single OaSourceRun -- one row per ASIN
    per run (not upserted across runs), so historical batches stay
    independently comparable rather than a later run silently
    overwriting an earlier one's findings. Mirrors the exact output
    shape from the user's own MVP spec: ASIN / Amazon price / EAN /
    MPN / Retailer / URL / retailer price / stock / match confidence /
    source confidence / estimated profit / ROI.

    retailer_price_gbp is auto-populated at run_batch time whenever
    Google Shopping found a trusted-tier match (see price_source
    below and OaSourceDiscoveryService.run_batch, 2026-08-19) -- NULL
    otherwise, until the user manually confirms or overrides it via
    OaSourceDiscoveryService.update_candidate (replaces the original
    MVP's confirm_price). Either way, estimated_profit_gbp/
    estimated_roi_pct are computed the SAME way, via FeeEngine with a
    "UK-OA" marketplace convention -- the same one OaLookupService.
    search_candidates already uses for a manual OA source.
    """
    __tablename__ = "oa_source_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("oa_source_runs.id"), index=True)

    asin: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    ean: Mapped[str] = mapped_column(String, default="")
    mpn: Mapped[str] = mapped_column(String, default="")
    amazon_price_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    category_name: Mapped[str] = mapped_column(String, default="")

    # Highest price payable for stock and still clear
    # FeeEngine.OA_TARGET_ROI_PCT, computed at run_batch time via
    # FeeEngine.max_source_cost against TODAY's Amazon price
    # (amazon_price_gbp) -- the same "buy <= £X" figure already shown
    # on the Competitors page and the "next up to scan" preview table,
    # persisted here too so it survives on the RESULTS table after a
    # run, not just before one.
    target_price_today_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    # The 30-day average Amazon buy-box price Keepa reports for this
    # ASIN (stats.avg30, via KeepaParser.price_avg(30)) -- shown
    # alongside target_price_30d_avg_gbp so the user can see WHY that
    # figure differs from target_price_today_gbp. 0.0 if Keepa had no
    # 30-day average for this ASIN.
    buy_box_avg_30d_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    # Same max_source_cost calculation as target_price_today_gbp, but
    # against buy_box_avg_30d_gbp instead of today's price -- per the
    # user's own reasoning (2026-08-19): a competitor may have made
    # their purchasing decision based on where the price has been
    # recently, which can be higher than today's price, so a target
    # based only on today's (possibly temporarily low) price can be
    # more conservative than what actually explains a competitor
    # already selling this successfully. 0.0 if buy_box_avg_30d_gbp is
    # 0.0 (no 30-day data).
    target_price_30d_avg_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    # "no_retailer_found" | "candidate_found" -- see
    # OaSourceDiscoveryService.run_batch for the full pipeline outcome
    # states this reflects.
    outcome: Mapped[str] = mapped_column(String, default="no_retailer_found")

    retailer_domain: Mapped[str] = mapped_column(String, default="")
    retailer_url: Mapped[str] = mapped_column(String, default="")
    retailer_title: Mapped[str] = mapped_column(String, default="")
    retailer_price_gbp: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    retailer_stock_text: Mapped[str] = mapped_column(String, default="")

    # "ean" | "mpn" | "brand_mpn" | "brand_title" | "title_only" | ""
    # (no candidate at all) -- see the user's own 5-tier hierarchy in
    # OaSourceDiscoveryService.classify_match. Never "confirmed" on
    # title_only alone -- see that method's docstring.
    match_tier: Mapped[str] = mapped_column(String, default="")
    match_confidence_pct: Mapped[int] = mapped_column(Integer, default=0)
    # "High" | "Medium" | "Low"
    source_confidence: Mapped[str] = mapped_column(String, default="")

    estimated_profit_gbp: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    estimated_roi_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)

    # "" (never priced) | "serpapi_auto" (retailer_price_gbp came from
    # an automated Google Shopping match at run_batch time, see
    # OaSourceDiscoveryService's SerpApi integration, 2026-08-19) |
    # "manual" (the user typed/overrode it via the results page,
    # whether confirming the auto-found price as-is or replacing the
    # retailer entirely with one they found themselves -- e.g. a
    # cheaper genuine price at a different retailer than the one Atlas
    # auto-picked). Shown on the results page so it's clear which
    # prices are machine-found vs. human-verified.
    price_source: Mapped[str] = mapped_column(String, default="")

    # Every Google Shopping candidate considered for this ASIN (JSON
    # list of {title, source, price, extracted_price, product_link}),
    # not just the winning one -- kept so the results page can show
    # "other retailers found" even when they didn't clear the
    # auto-trust match-tier bar, since a human reviewing the page may
    # still recognise one as correct (this is exactly the scenario the
    # user described: Atlas auto-picked Argos, but Halfords -- also
    # found by the same search, just not auto-trusted -- was actually
    # the cheaper genuine match). "" if no Google Shopping search ran
    # (e.g. SERPAPI_API_KEY not configured) or it returned nothing.
    shopping_candidates_json: Mapped[str] = mapped_column(String, default="")

    # Whether this candidate's confirmed price cleared FeeEngine.
    # OA_TARGET_ROI_PCT at a trusted match tier and was automatically
    # promoted into a real ProductRecord via OpportunityEngine.analyse
    # + ProductRepository.save_opportunity -- i.e. it now behaves
    # exactly like any other scan lead everywhere in Atlas (Review
    # Queue, Dashboard counters, Discord). Guards against promoting the
    # same candidate twice (e.g. if the user re-confirms an unchanged
    # price) -- see OaSourceDiscoveryService._promote_if_qualifying.
    added_to_review_queue: Mapped[bool] = mapped_column(Boolean, default=False)

    # Every query string actually sent to Brave for this ASIN (discovery
    # + verification), JSON list -- lets the results page show "what
    # was searched" per row, and feeds the test protocol's "which
    # search queries worked" metric.
    queries_used_json: Mapped[str] = mapped_column(String, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class OaSourceExcludedAsin(Base):
    """
    An ASIN the user has manually pruned from the OA Source Discovery
    "next up to scan" preview (see OaSourceDiscoveryService.
    preview_candidates/get_candidate_asins) -- deliberately a SEPARATE,
    module-scoped table from Atlas's existing app-wide ExcludedProduct
    list, not a reuse of it.

    Why separate: excluding something here means "don't spend OA
    Discovery's Brave/Keepa budget searching for a UK retailer for
    this specific ASIN" -- a narrower, cheaper judgment than "this
    isn't a product I want anywhere in Atlas at all" (ExcludedProduct),
    which also skips it on Discovery/Products/Watchlist/Review Queue
    scans generally. Reusing ExcludedProduct would mean a quick "skip
    this one for now" click here silently also pulling the ASIN out of
    every other part of the app -- surprising, and not what the user
    asked for when they confirmed "OA Discovery only" as the intended
    scope for this feature.
    """
    __tablename__ = "oa_source_excluded_asins"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(String, default="")

    excluded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

class ActivityLogEntry(Base):
    """
    One row per completed unit of real work Atlas has done -- a brand
    search, a competitor check, a replen re-price, etc (2026-08-19,
    added for the Dashboard's "Today's activity" section). Written at
    the natural completion point of each pipeline (see ActivityLog in
    app/services/activity_log.py), read back grouped by day/type so
    the Dashboard can answer "what has Atlas actually done today"
    without guessing from incidental per-item timestamps scattered
    across other tables. Purely additive and starts empty on a fresh
    DB -- there's no way to backfill activity that was never recorded
    before this table existed.
    """
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "brand_search" / "competitor_check" / "replen_check" /
    # "watchlist_check" / "signal_check" / "oa_discovery_run" /
    # "lead_analysis" -- see ActivityLog.record's callers for the
    # full, authoritative list of values actually written.
    activity_type: Mapped[str] = mapped_column(String, index=True)

    detail: Mapped[str] = mapped_column(String, default="")

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class SchedulerStatus(Base):
    """
    One row per background scheduler loop in app/main.py, updated at
    the end of every tick that actually did (or genuinely attempted)
    its work -- NOT every wake-up, since a tick can legitimately skip
    itself (e.g. Seller Watch yielding to a manual scan in progress)
    without that counting as "ran". Lets the Dashboard show "last ran
    X ago" / "next due ~Y" for each automated task (2026-08-19) without
    guessing from whatever individual items happened to get touched --
    a global per-scheduler timestamp is the only thing that's honest
    about a scheduler that found nothing to do this tick.
    """
    __tablename__ = "scheduler_status"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # BUG FIX (2026-08-21): missing from this model even though
    # ActivityLog.mark_tick/scheduler_overview already write and read
    # it -- a real DB-loaded row (any request after the process that
    # set it in-memory has restarted) had no such attribute at all,
    # crashing the Dashboard homepage with a bare AttributeError.
    last_summary: Mapped[str] = mapped_column(String, default="")


class TokenUsageEvent(Base):
    """
    One row per measured Keepa spend (or SP-API-avoided spend) --
    2026-08-21, for the Settings > Token Usage page. Written at the
    same low-level call sites that already talk to Keepa/SP-API
    directly (ProductService.get_products, ProductFinder.find_brand/
    find_signal_candidates, SellerWatchService._fetch_storefronts, and
    BrandScanService's SP-API pre-check in Step 4) rather than derived
    after the fact from ProductRecord timestamps -- those can't
    distinguish WHICH feature spent the tokens, or separate a
    Product Finder call's cost from a query() call's, the way this
    can.

    category: which Atlas feature triggered the call -- "scan_queue",
    "replen", "watchlist", "competitor_watch", "discovery", "verdict",
    "signals", "oa_lookup", "oa_discovery", "category_survey",
    "manual_api", "debug", or "other" (the default for any call site
    that hasn't been given an explicit usage_category yet -- see each
    service's own docstring for which ones still fall back to this).

    call_type: "keepa_query" (ProductService.get_products), "keepa_
    product_finder" (ProductFinder), "keepa_seller_query"
    (SellerWatchService), or "sp_api_saved" -- an ESTIMATE of tokens a
    Keepa EU call would have cost, credited (not actually spent) when
    BrandScanService's SP-API pre-check in Step 4 finds a viable price
    for free and skips that Keepa call entirely. Summed separately
    from real spend (see TokenUsageService.savings_totals), never
    netted directly against it -- an estimate deserves to look like
    one, not get silently blended into a real measured number.

    marketplace: "UK"/"DE"/"FR"/"ES"/"IT" for a query()/sp_api_saved
    row, "" for a Product Finder or seller-storefront row (neither is
    scoped to one marketplace the way a product query is).
    """
    __tablename__ = "token_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )

    category: Mapped[str] = mapped_column(String, default="other", index=True)
    call_type: Mapped[str] = mapped_column(String, default="")
    marketplace: Mapped[str] = mapped_column(String, default="")
    asins_count: Mapped[int] = mapped_column(Integer, default=0)

    # Real measured Keepa tokens spent (call_type="keepa_*"), or the
    # ESTIMATED tokens a skipped Keepa call would have cost
    # (call_type="sp_api_saved") -- see this class's own docstring.
    tokens: Mapped[float] = mapped_column(Float, default=0.0)
    last_summary: Mapped[str] = mapped_column(String, default="")
