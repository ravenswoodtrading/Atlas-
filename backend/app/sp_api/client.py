import csv
import gzip
import io
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
EU_ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"

# Marketplace IDs for this account's UK + EU (Unified European Account)
# marketplaces -- confirmed live via GET /sellers/v1/marketplaceParticipations
# (2026-08-21), not guessed from Amazon's docs. Also confirmed live
# that getItemOffers returns REAL competitive pricing for third-party
# ASINs on this account (not just the seller's own listings) across
# all 5 of these -- see brand_scan_service.py Step 4's own comment for
# how that was validated before being wired into the real pipeline.
MARKETPLACE_IDS = {
    "UK": "A1F83G8C2ARO7P",
    "DE": "A1PA6795UKMFR9",
    "FR": "A13V1IB3VIYZZH",
    "ES": "A1RKKUPIHCS9HS",
    "IT": "APJ6JRA9NG5V4",
}

# Refresh the cached LWA access token this many seconds before its
# real expiry (Amazon issues them with a 3600s lifetime) -- avoids a
# request firing with a token that expires mid-flight.
TOKEN_REFRESH_MARGIN_SECONDS = 120

# getItemOffers' rate limit is tight (a handful of requests/second
# with a small burst). On a 429, back off and retry rather than
# treating a rate-limit hit as "no data" -- that would wrongly make
# BrandScanService's caller fall through to Keepa thinking SP-API
# genuinely had nothing, when it just hadn't been asked yet.
# MAX_RETRIES bounds the worst case so one stubborn rate-limited ASIN
# can't stall an entire scan.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Proactive pacing between requests -- checking one ASIN can mean up
# to 4 sequential calls (DE/FR/ES/IT) before either finding a viable
# price or giving up, and a scan can check many ASINs, so this adds up
# to real wall-clock time even though it costs no Keepa tokens. A
# fixed minimum gap keeps this well under the documented rate limit
# proactively, rather than firing as fast as possible and eating 429
# retries reactively -- steadier latency, and doesn't waste the retry
# budget on self-inflicted rate-limiting.
MIN_REQUEST_INTERVAL_SECONDS = 0.25


class SPAPIClient:
    """
    Minimal Amazon SP-API client -- LWA (Login with Amazon) token
    exchange/caching plus getItemOffers, the one endpoint
    BrandScanService's Stage 2 free-first-check (see brand_scan_service.py
    Step 4) actually needs.

    Deliberately narrow -- this is not a general SP-API SDK, just the
    one call Atlas needs. No AWS SigV4 signing: Amazon phased that
    requirement out for Pricing/Catalog in favour of LWA-only auth, so
    a bearer access token in the x-amz-access-token header is enough.
    """

    # Max ASINs searchCatalogItems accepts per call (Amazon's own
    # documented limit for the `identifiers` param).
    CATALOG_ITEMS_BATCH_SIZE = 20

    # Proactive pacing for searchCatalogItems specifically -- separate
    # from MIN_REQUEST_INTERVAL_SECONDS (tuned for getItemOffers). 2
    # req/sec per atlas-oa-scale-up-spec.md §4.1 -- NOT yet verified
    # against Amazon's actual published rate card for this endpoint,
    # same "read off a live call, don't trust docs" caution the spec
    # itself gives Serper's response shape (§11). Confirm against real
    # 429s (or their absence) on first live run and adjust here if so.
    CATALOG_MIN_REQUEST_INTERVAL_SECONDS = 0.5

    # Proactive pacing for the FBA Inventory API (2026-09-01, Out of
    # Stock Cleanup) -- Amazon's documented rate card is ~2 req/sec
    # burst 2; this stays comfortably under that. Not yet verified
    # against a live 429 (or its absence), same "confirm on first real
    # run" caution as CATALOG_MIN_REQUEST_INTERVAL_SECONDS above.
    INVENTORY_MIN_REQUEST_INTERVAL_SECONDS = 1.0

    # Reports API pacing (createReport/getReport/getReportDocument) --
    # Amazon's create-report endpoint is the tightest of the three
    # (documented around 0.0167 req/sec, i.e. ~1/minute, refilling);
    # this file only ever creates ONE report per sweep, so a flat 1s
    # gap is just a sane floor between the create/poll/download calls
    # themselves, not an attempt to model that refill rate.
    REPORTS_MIN_REQUEST_INTERVAL_SECONDS = 1.0
    REPORT_POLL_INTERVAL_SECONDS = 10
    REPORT_POLL_TIMEOUT_SECONDS = 300

    # Listings Items API pacing -- documented around 5 req/sec burst
    # 10, but this file only ever deletes one human-approved SKU at a
    # time (see InventoryCleanupService.approve_and_delete), so there's
    # no real batching pressure here either.
    LISTINGS_MIN_REQUEST_INTERVAL_SECONDS = 0.5

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, seller_id: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        # Only needed for delete_listing_item (Listings Items API path
        # includes the seller/merchant ID) -- every other method in
        # this file works fine without it, so an empty default keeps
        # get_item_offers/search_catalog_items working exactly as
        # before for anyone who hasn't set SP_API_SELLER_ID yet.
        self.seller_id = seller_id
        self._access_token = None
        self._access_token_expires_at = 0.0
        self._last_request_at = 0.0

    def _pace(self, min_interval: float = MIN_REQUEST_INTERVAL_SECONDS):
        elapsed = time.monotonic() - self._last_request_at
        remaining = min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get_access_token(self) -> str:
        now = time.monotonic()

        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()

        self._access_token = body["access_token"]
        self._access_token_expires_at = (
            now + body.get("expires_in", 3600) - TOKEN_REFRESH_MARGIN_SECONDS
        )

        return self._access_token

    def get_item_offers(self, asin: str, marketplace: str, item_condition: str = "New") -> dict | None:
        """
        Returns {"status": str, "price": float_or_None, "offer_count": int_or_None}
        for the CURRENT Buy Box price on `marketplace` (one of
        MARKETPLACE_IDS' keys), or None if the call itself failed to
        produce a usable answer at all (network error, exhausted
        retries on rate-limiting, an invalid ASIN for that
        marketplace, unconfigured marketplace).

        That None-vs-real-dict distinction matters exactly like
        ProductFinder.find_brand's None-vs-[] one: a failed call must
        never be read as "confirmed no source here", or a caller would
        wrongly skip a Keepa check it still genuinely needs to make.
        price is None (inside a real dict) whenever there's no
        genuinely buyable offer right now (out of stock, FBM-only, or
        just not sold on this marketplace) -- that IS real information,
        unlike a failed call.
        """
        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return None

        for attempt in range(MAX_RETRIES):
            try:
                access_token = self._get_access_token()
            except Exception as exc:
                print(f"SP-API token refresh failed: {exc}")
                return None

            self._pace()

            try:
                resp = requests.get(
                    f"{EU_ENDPOINT}/products/pricing/v0/items/{asin}/offers",
                    headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                    params={"MarketplaceId": marketplace_id, "ItemCondition": item_condition},
                    timeout=15,
                )
            except Exception as exc:
                print(f"SP-API getItemOffers request failed for {asin}/{marketplace}: {exc}")
                return None

            if resp.status_code == 429:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if resp.status_code != 200:
                # Includes a real 400 "invalid ASIN for marketplace" --
                # a genuine "not sold here" answer, but still returned
                # as None (not a fabricated status/price dict) so a
                # future auth/config regression can't silently masquerade
                # as normal-looking empty data.
                return None

            payload = resp.json().get("payload", {})
            buy_box_prices = payload.get("Summary", {}).get("BuyBoxPrices", [])
            price = buy_box_prices[0]["LandedPrice"]["Amount"] if buy_box_prices else None

            return {
                "status": payload.get("status"),
                "price": price,
                "offer_count": payload.get("Summary", {}).get("TotalOfferCount"),
            }

        # Exhausted retries, still rate-limited.
        return None

    def search_catalog_items(self, asins: list[str], marketplace: str = "UK") -> dict | None:
        """
        Free (no Keepa token) sales-rank + package-dimension lookup for
        up to CATALOG_ITEMS_BATCH_SIZE ASINs in ONE call -- the first,
        cheapest step of the free screen in atlas-oa-scale-up-spec.md
        §4.1 (rank ceiling, before spending anything). Callers with
        more ASINs than that must chunk themselves and call this once
        per chunk -- same division of labour as get_item_offers (one
        ASIN per call, caller loops): this method owns pacing/retry for
        its OWN call, not cross-call batching strategy.

        Returns {asin: {"rank": int_or_None, "rank_category": str,
        "dimensions_cm": {"length","width","height","weight_kg"}_or_None}}
        for every ASIN Amazon's catalog actually returned data for -- an
        ASIN simply absent from the returned dict means Amazon had
        nothing for it (a real, meaningful answer, same as
        get_item_offers' price=None-inside-a-real-dict case), NOT a
        failure.

        Returns None (not {}) if the CALL ITSELF failed (network error,
        exhausted 429 retries, auth failure, unconfigured marketplace).
        Callers MUST check `is None` before treating an empty/partial
        result as "genuinely no data" -- exactly the get_item_offers/
        ProductFinder convention (see those docstrings for why
        conflating the two is a real bug class, not theoretical: a
        failed call read as "no rank data" would wrongly screen an ASIN
        out for a reason that never actually applied).

        UNVERIFIED AGAINST A LIVE CALL (2026-08-29): the response field
        names parsed below (salesRanks/classificationRanks/rank,
        dimensions/package) are from Amazon's published Catalog Items
        API 2022-04-01 docs, not a live response. Confirm the actual
        shape on the first real call (print(resp.json()) once) and
        adjust the parsing below before trusting this for real
        screening decisions -- same discipline the spec demands of
        Serper's response shape (§11), for the same reason.
        """
        if not asins:
            return {}

        asins = asins[: self.CATALOG_ITEMS_BATCH_SIZE]

        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return None

        for attempt in range(MAX_RETRIES):
            try:
                access_token = self._get_access_token()
            except Exception as exc:
                print(f"SP-API token refresh failed: {exc}")
                return None

            self._pace(self.CATALOG_MIN_REQUEST_INTERVAL_SECONDS)

            try:
                resp = requests.get(
                    f"{EU_ENDPOINT}/catalog/2022-04-01/items",
                    headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                    params={
                        "identifiers": ",".join(asins),
                        "identifiersType": "ASIN",
                        "marketplaceIds": marketplace_id,
                        "includedData": "salesRanks,dimensions",
                    },
                    timeout=15,
                )
            except Exception as exc:
                print(f"SP-API searchCatalogItems request failed for {len(asins)} ASINs/{marketplace}: {exc}")
                return None

            if resp.status_code == 429:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if resp.status_code != 200:
                return None

            body = resp.json()
            results = {}

            for item in body.get("items", []):
                asin = item.get("asin")
                if not asin:
                    continue

                rank = None
                rank_category = ""
                for rank_entry in item.get("salesRanks", []):
                    class_ranks = rank_entry.get("classificationRanks") or rank_entry.get("displayGroupRanks") or []
                    if class_ranks:
                        rank = class_ranks[0].get("rank")
                        rank_category = class_ranks[0].get("title", "")
                        break

                dimensions_cm = None
                for dim_entry in item.get("dimensions", []):
                    package = dim_entry.get("package")
                    if package:
                        dimensions_cm = {
                            "length": (package.get("length") or {}).get("value"),
                            "width": (package.get("width") or {}).get("value"),
                            "height": (package.get("height") or {}).get("value"),
                            "weight_kg": (package.get("weight") or {}).get("value"),
                        }
                        break

                results[asin] = {
                    "rank": rank,
                    "rank_category": rank_category,
                    "dimensions_cm": dimensions_cm,
                }

            return results

        # Exhausted retries, still rate-limited.
        return None

    def get_inventory_summaries(self, marketplace: str = "UK", seller_skus: list[str] | None = None) -> dict | None:
        """
        Returns {sku: {"asin","title","fulfillable","inbound_working",
        "inbound_shipped","inbound_receiving","reserved","total"}} for
        FBA SKUs on `marketplace` -- the real-time current-stock half
        of Out of Stock Cleanup's detection (see
        InventoryCleanupService.detect_out_of_stock_candidates). The
        other half, confirmed past sales, comes from
        get_fba_fulfilled_shipments_units below -- a SKU is only a
        genuine candidate for deletion when BOTH say so.

        seller_skus (optional, max 50 -- Amazon's documented cap, same
        division of labour as CATALOG_ITEMS_BATCH_SIZE: callers with
        more must chunk themselves) filters to specific SKUs -- used by
        InventoryCleanupService.approve_and_delete to re-confirm ONE
        SKU's live stock immediately before deleting it, rather than
        trusting a slightly older bulk snapshot. Omit for the full
        nightly sweep.

        Returns None (not {}) if the call itself failed outright
        (network error, auth failure, exhausted 429 retries,
        unconfigured marketplace) -- same None-vs-real-dict convention
        as get_item_offers/search_catalog_items above. An empty {} is a
        real answer (genuinely zero FBA SKUs matched); None never is.
        Callers MUST check `is None` before reading an empty/partial
        result as "no stock anywhere" -- a failed call read that way
        could wrongly flag a SKU that Amazon simply didn't answer for.

        UNVERIFIED AGAINST A LIVE CALL (2026-09-01): field names below
        (inventorySummaries/inventoryDetails/fulfillableQuantity/
        reservedQuantity/pagination.nextToken) are from Amazon's
        published FBA Inventory API v1 docs, not a live response --
        same caution search_catalog_items' own docstring gives its own
        fields. Confirm the actual shape via GET /debug/sp-api/
        inventory-check (added alongside this) before trusting this for
        a real delete decision.
        """
        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return None

        results: dict[str, dict] = {}
        next_token = None

        while True:
            for attempt in range(MAX_RETRIES):
                try:
                    access_token = self._get_access_token()
                except Exception as exc:
                    print(f"SP-API token refresh failed: {exc}")
                    return None

                self._pace(self.INVENTORY_MIN_REQUEST_INTERVAL_SECONDS)

                if next_token:
                    params = {"nextToken": next_token}
                else:
                    params = {
                        "details": "true",
                        "granularityType": "Marketplace",
                        "granularityId": marketplace_id,
                        "marketplaceIds": marketplace_id,
                    }
                    if seller_skus:
                        params["sellerSkus"] = ",".join(seller_skus[:50])

                try:
                    resp = requests.get(
                        f"{EU_ENDPOINT}/fba/inventory/v1/summaries",
                        headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                        params=params,
                        timeout=30,
                    )
                except Exception as exc:
                    print(f"SP-API getInventorySummaries request failed for {marketplace}: {exc}")
                    return None

                if resp.status_code == 429:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue

                if resp.status_code != 200:
                    print(
                        f"SP-API getInventorySummaries returned {resp.status_code} "
                        f"for {marketplace}: {resp.text[:300]}"
                    )
                    return None

                body = resp.json()
                payload = body.get("payload", {})

                for item in payload.get("inventorySummaries", []):
                    sku = item.get("sellerSku")
                    if not sku:
                        continue

                    details = item.get("inventoryDetails", {}) or {}
                    reserved = details.get("reservedQuantity", {}) or {}

                    results[sku] = {
                        "asin": item.get("asin", ""),
                        "title": item.get("productName", ""),
                        "fulfillable": details.get("fulfillableQuantity", 0) or 0,
                        "inbound_working": details.get("inboundWorkingQuantity", 0) or 0,
                        "inbound_shipped": details.get("inboundShippedQuantity", 0) or 0,
                        "inbound_receiving": details.get("inboundReceivingQuantity", 0) or 0,
                        "reserved": reserved.get("totalReservedQuantity", 0) or 0,
                        "total": item.get("totalQuantity", 0) or 0,
                    }

                next_token = (payload.get("pagination", {}) or {}).get("nextToken")
                break
            else:
                # Exhausted retries, still rate-limited on this page.
                return None

            if not next_token:
                break

        return results

    def _run_report(
        self,
        report_type: str,
        marketplace: str,
        data_start: datetime | None = None,
        data_end: datetime | None = None,
    ) -> str | None:
        """
        Shared create-report / poll-status / download-document /
        decompress sequence for the Reports API -- extracted 2026-09-02
        (Storage Fee Watch) from what was originally
        get_fba_fulfilled_shipments_units's own body, so that method and
        the two new get_storage_fee_charges/get_longterm_storage_fee_
        charges below all reuse this instead of duplicating the same
        ~80-line create/poll/download dance three times.

        Returns the decoded report text (Amazon's FBA flat-file reports
        are all tab-delimited) or None (not "") on ANY failure at any
        stage -- creation, polling, timing out, downloading, or
        decompressing -- same None-vs-real-data convention as every
        other method in this file. Callers own their own CSV parsing
        since column names differ per report_type; this helper only
        owns the HTTP mechanics.

        data_start/data_end become dataStartTime/dataEndTime on the
        createReport call when BOTH are given; omitted entirely when
        either is None, for report types that don't take (or reject) a
        date range.
        """
        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return None

        try:
            access_token = self._get_access_token()
        except Exception as exc:
            print(f"SP-API token refresh failed: {exc}")
            return None

        self._pace(self.REPORTS_MIN_REQUEST_INTERVAL_SECONDS)

        report_spec = {"reportType": report_type, "marketplaceIds": [marketplace_id]}
        if data_start is not None and data_end is not None:
            report_spec["dataStartTime"] = data_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            report_spec["dataEndTime"] = data_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            create_resp = requests.post(
                f"{EU_ENDPOINT}/reports/2021-06-30/reports",
                headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                json=report_spec,
                timeout=30,
            )
        except Exception as exc:
            print(f"SP-API createReport request failed for {report_type}: {exc}")
            return None

        if create_resp.status_code not in (200, 202):
            print(f"SP-API createReport returned {create_resp.status_code} for {report_type}: {create_resp.text[:300]}")
            return None

        report_id = create_resp.json().get("reportId")
        if not report_id:
            return None

        # Poll until DONE, FATAL, or CANCELLED -- report generation is
        # genuinely async and can take anywhere from seconds to a few
        # minutes depending on account size/date range, unlike every
        # other call in this file. REPORT_POLL_TIMEOUT_SECONDS bounds
        # the worst case so a stuck report can't hang the nightly sweep
        # forever.
        deadline = time.monotonic() + self.REPORT_POLL_TIMEOUT_SECONDS
        report_document_id = None

        while time.monotonic() < deadline:
            try:
                access_token = self._get_access_token()
            except Exception as exc:
                print(f"SP-API token refresh failed: {exc}")
                return None

            self._pace(self.REPORTS_MIN_REQUEST_INTERVAL_SECONDS)

            try:
                status_resp = requests.get(
                    f"{EU_ENDPOINT}/reports/2021-06-30/reports/{report_id}",
                    headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                    timeout=15,
                )
            except Exception as exc:
                print(f"SP-API getReport request failed for {report_id}: {exc}")
                return None

            if status_resp.status_code != 200:
                print(f"SP-API getReport returned {status_resp.status_code} for {report_id}: {status_resp.text[:300]}")
                return None

            status_body = status_resp.json()
            processing_status = status_body.get("processingStatus")

            if processing_status == "DONE":
                report_document_id = status_body.get("reportDocumentId")
                break

            if processing_status in ("CANCELLED", "FATAL"):
                print(f"SP-API report {report_id} ({report_type}) ended as {processing_status}")
                return None

            time.sleep(self.REPORT_POLL_INTERVAL_SECONDS)

        if not report_document_id:
            print(f"SP-API report {report_id} ({report_type}) did not finish within {self.REPORT_POLL_TIMEOUT_SECONDS}s")
            return None

        try:
            access_token = self._get_access_token()
        except Exception as exc:
            print(f"SP-API token refresh failed: {exc}")
            return None

        self._pace(self.REPORTS_MIN_REQUEST_INTERVAL_SECONDS)

        try:
            doc_resp = requests.get(
                f"{EU_ENDPOINT}/reports/2021-06-30/documents/{report_document_id}",
                headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                timeout=15,
            )
        except Exception as exc:
            print(f"SP-API getReportDocument request failed for {report_document_id}: {exc}")
            return None

        if doc_resp.status_code != 200:
            print(f"SP-API getReportDocument returned {doc_resp.status_code}: {doc_resp.text[:300]}")
            return None

        doc_body = doc_resp.json()
        download_url = doc_body.get("url")
        if not download_url:
            return None

        try:
            # The download URL is a pre-signed S3 link -- no auth headers.
            file_resp = requests.get(download_url, timeout=60)
            file_resp.raise_for_status()
            raw_bytes = file_resp.content
        except Exception as exc:
            print(f"SP-API report document download failed: {exc}")
            return None

        if doc_body.get("compressionAlgorithm") == "GZIP":
            try:
                raw_bytes = gzip.decompress(raw_bytes)
            except Exception as exc:
                print(f"SP-API report document gzip decompress failed: {exc}")
                return None

        try:
            return raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw_bytes.decode("latin-1")

    def get_fba_fulfilled_shipments_units(self, marketplace: str = "UK", lookback_days: int = 30) -> dict | None:
        """
        Returns {sku: {"units_shipped": int, "last_shipment_date":
        "YYYY-MM-DD"}} aggregated from the GET_AMAZON_FULFILLED_
        SHIPMENTS_DATA report over the last `lookback_days` -- the "has
        this SKU actually sold before" half of Out of Stock Cleanup's
        detection. A SKU simply absent from the returned dict has NO
        confirmed FBA sale in the lookback window (a real, meaningful
        answer -- e.g. a listing created but never stocked/sold, exactly
        what should be excluded from auto-flagging for deletion), not a
        failure.

        Returns None (not {}) if the report itself failed to produce a
        usable result -- see _run_report's own None-vs-real-data
        convention. Callers MUST check `is None` before reading an
        empty dict as "confirmed no sales ever" -- a failed report read
        that way would wrongly flag a genuinely-selling SKU for
        deletion.

        VERIFIED AGAINST A LIVE CALL (2026-09-01): the bare reportType
        "GET_AMAZON_FULFILLED_SHIPMENTS_DATA" used in the original
        docs-based guess doesn't exist -- Amazon splits it into three
        suffixed variants (_GENERAL / _INVOICING / _TAX), and requesting
        the nonexistent bare name got a 403 Unauthorized rather than a
        clearer "invalid report type" error, which looked exactly like a
        missing-role problem and cost real time to track down. Fixed to
        "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL" -- the
        non-PII/non-tax variant, authorized by the same "Inventory and
        Order Tracking" role get_inventory_summaries already uses. The
        column names below (sku/quantity-shipped/shipment-date) were
        correct in the original guess and confirmed against a real
        report download.

        REAL WINDOW CONFIRMED NARROW (2026-09-02, via /debug/sp-api/
        sales-check): this report's actual request window is much
        narrower than the original 730-day default assumed -- live
        testing found 30 days succeeds, 45/60/90/180/365 all end the
        report as FATAL (Amazon returns no reason text, just the FATAL
        status). The 403 Unauthorized this used to return (see the
        2026-09-01 note above) is gone now that the pending role
        approval came through, which is what exposed this separate,
        previously-masked problem. lookback_days now defaults to 30 --
        the confirmed-working ceiling -- rather than the old, always-
        failing 730.

        A single call to this method genuinely cannot answer "has this
        SKU EVER sold" beyond the last ~30 days -- it can no longer be
        Out of Stock Cleanup's primary "ever sold" signal the way the
        original design assumed. See InventoryCleanupService.
        _is_confirmed_sold for how that's handled now: Tamara's own
        SKUs mostly carry an embedded listing date (2026-09-02: "The SKU
        name includes a date field so anything recent has likely never
        been stocked but anything old will have sold through"), which
        is the primary signal now; this report is the fallback ONLY for
        SKUs that don't carry that date pattern at all.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)

        text = self._run_report("GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL", marketplace, start, now)
        if text is None:
            return None

        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        results: dict[str, dict] = {}

        for row in reader:
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue

            try:
                qty = int(float(row.get("quantity-shipped") or 0))
            except (ValueError, TypeError):
                qty = 0

            ship_date = (row.get("shipment-date") or "")[:10]

            entry = results.setdefault(sku, {"units_shipped": 0, "last_shipment_date": ""})
            entry["units_shipped"] += qty
            if ship_date and ship_date > entry["last_shipment_date"]:
                entry["last_shipment_date"] = ship_date

        return results

    def get_storage_fee_charges(self, marketplace: str = "UK") -> dict | None:
        """
        Returns {asin: {"fnsku", "title", "average_quantity_on_hand",
        "storage_fee_amount", "month_of_charge", "currency"}} from
        GET_FBA_STORAGE_FEE_CHARGES_DATA -- Amazon's monthly storage-fee
        snapshot report (Storage Fee Watch, 2026-09-02, Tamara: "identify
        which items in my inventory are contributing lots to my storage
        fees"). The raw report is one row per ASIN+fulfilment-centre
        combination; rows are summed per ASIN here since callers care
        about "how much does this ASIN cost in storage total", not a
        per-FC breakdown.

        Keyed by ASIN, not SKU -- this report has no seller-SKU column
        at all (only asin/fnsku), unlike get_inventory_summaries/
        get_fba_fulfilled_shipments_units above. StorageFeeService is
        responsible for joining this back to SKU via
        get_inventory_summaries' own asin field.

        Returns None (not {}) on any failure -- see _run_report's own
        None-vs-real-data convention, same as every other method here.

        STILL UNVERIFIED (2026-09-02): both the column names below and
        the required SP-API role are read off Amazon's docs
        (report-type-values-fba), not a live response. Retested across
        TWO separate days (2026-09-01 and 09-02, the second AFTER
        Amazon's own confirmation email that a pending role approval had
        come through) -- every single attempt on both days came back
        CANCELLED, NEVER Unauthorized, which strengthens (but still
        doesn't prove) the theory that this is Amazon's documented
        once-per-day-per-seller quota for this report type rather than a
        role gap: a role problem should show as Unauthorized regardless
        of which day it's tried, and it never has. Every attempt so far
        has also had the misfortune of racing this account's own nightly
        scheduler requesting the exact same report type at the same
        startup (see StorageFeeService.refresh -- every fresh process
        start fires it immediately), so a fully clean, uncontested
        single attempt still hasn't actually happened. That's a
        genuinely different failure mode from get_longterm_storage_fee_
        charges' sibling report below, which succeeded cleanly on the
        same credentials/roles on both test days. A live GitHub bug
        report (amzn/selling-partner-api-models #4934) shows a developer
        with "Finance and Accounting"/"Selling Partner Insights"/
        "Inventory and Order Tracking" ALL authorized still getting
        Unauthorized on this exact report type in THEIR case, so a role
        gap isn't ruled out either -- just still not confirmed. Next
        attempt should avoid the scheduler collision (e.g. call this
        directly in a one-off script, not via a freshly-started server).

        Amazon's docs say this report's content refreshes at least every
        72 hours and instruct dataStartTime >= 72h before now,
        dataEndTime = now, to get current data -- this method uses 76h
        for a small margin.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=76)

        text = self._run_report("GET_FBA_STORAGE_FEE_CHARGES_DATA", marketplace, start, now)
        if text is None:
            return None

        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        results: dict[str, dict] = {}

        for row in reader:
            asin = (row.get("asin") or "").strip()
            if not asin:
                continue

            try:
                qty = int(float(row.get("average_quantity_on_hand") or 0))
            except (ValueError, TypeError):
                qty = 0

            try:
                fee = float(row.get("estimated_monthly_storage_fee") or 0)
            except (ValueError, TypeError):
                fee = 0.0

            entry = results.setdefault(asin, {
                "fnsku": row.get("fnsku", ""),
                "title": row.get("product_name", ""),
                "average_quantity_on_hand": 0,
                "storage_fee_amount": 0.0,
                "month_of_charge": row.get("month_of_charge", ""),
                "currency": row.get("currency", ""),
            })
            entry["average_quantity_on_hand"] += qty
            entry["storage_fee_amount"] += fee

        return results

    def get_longterm_storage_fee_charges(self, marketplace: str = "UK") -> dict | None:
        """
        Returns {sku: {"asin", "fnsku", "title", "condition",
        "quantity_charged", "amount_charged", "surcharge_age_tier",
        "rate_surcharge", "currency", "snapshot_date"}} from GET_FBA_
        FULFILLMENT_LONGTERM_STORAGE_FEE_CHARGES_DATA -- the aged-
        inventory/long-term-storage-surcharge half of Storage Fee Watch
        (Tamara, 2026-09-02: "identify products that are getting close
        to long term storage or that we should really try and shift").

        VERIFIED AGAINST A LIVE CALL (2026-09-02, via GET /debug/sp-api/
        storage-fee-check): the 2023-era column names in the original
        pre-research (a GitHub bug thread reporting split "short-time-
        range"/"long-time-range" charge columns) are STALE -- the real,
        current schema is a single qty-charged/amount-charged pair plus
        an explicit surcharge_age_tier column (e.g. "241-270", a
        day-range string) that IS the "days in FC" signal the
        pre-research wasn't sure existed. This report call also worked
        cleanly with whatever SP-API role/authorization this account
        already has (no permission error at all) -- the earlier concern
        that this might need a new, separately-approved role turned out
        not to apply to THIS report; see get_storage_fee_charges'
        docstring, which is still unconfirmed.

        surcharge_age_tier is passed through EXACTLY as Amazon returns
        it -- deliberately never parsed into a hardcoded threshold
        (the one live sample seen, "241-270", doesn't even cleanly match
        either the historical 271/365-day cutoffs or the pre-research's
        guessed ~180-day one, which is exactly why this file never
        hardcodes any of those numbers). A SKU absent from the returned
        dict has NO surcharge on the most recent snapshot -- a real,
        meaningful answer, not a failure.

        Returns None (not {}) on any failure -- see _run_report's own
        None-vs-real-data convention.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)

        text = self._run_report(
            "GET_FBA_FULFILLMENT_LONGTERM_STORAGE_FEE_CHARGES_DATA", marketplace, start, now
        )
        if text is None:
            return None

        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        results: dict[str, dict] = {}

        for row in reader:
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue

            try:
                qty = int(float(row.get("qty-charged") or 0))
            except (ValueError, TypeError):
                qty = 0

            try:
                amount = float(row.get("amount-charged") or 0)
            except (ValueError, TypeError):
                amount = 0.0

            results[sku] = {
                "asin": row.get("asin", ""),
                "fnsku": row.get("fnsku", ""),
                "title": row.get("product-name", ""),
                "condition": row.get("condition", ""),
                "quantity_charged": qty,
                "amount_charged": amount,
                "surcharge_age_tier": row.get("surcharge-age-tier", ""),
                "rate_surcharge": row.get("rate-surcharge", ""),
                "currency": row.get("currency", ""),
                "snapshot_date": (row.get("snapshot-date") or "")[:10],
            }

        return results

    def delete_listing_item(self, sku: str, marketplace: str = "UK") -> dict:
        """
        Calls the Listings Items API's deleteListingsItem -- REMOVES the
        listing for `sku` on `marketplace` entirely (a real delete, not
        a deactivation -- Tamara's own choice, 2026-09-01). This is the
        one call in this file that changes something real and hard to
        undo on Amazon. Only ever call this for a row a human has
        explicitly approved on the /inventory-cleanup page -- see
        InventoryCleanupService.approve_and_delete, which also
        re-confirms current stock is still zero immediately before
        calling this, to guard against a restock landing between review
        and click.

        Returns {"success": bool, "error": str} -- never raises, and
        never returns a bare truthy/falsy value, so a caller can't
        mistake "the call itself failed" for "Amazon rejected the
        delete" -- both are success=False here, but `error` says which.
        success=True means Amazon ACCEPTED the delete submission;
        Amazon's own processing is asynchronous, so a rare
        accepted-but-failed-downstream case won't be caught here --
        there's no cheap way to confirm from this call alone. A later
        sweep finding the SKU still listed would be the real signal,
        not handled by this method.

        Requires self.seller_id (SP_API_SELLER_ID in .env -- Seller
        Central > Settings > Account Info > "Merchant Token") --
        every other method in this file works without it.

        UNVERIFIED AGAINST A LIVE CALL (2026-09-01): response shape
        (status/issues) is from Amazon's published Listings Items API
        2021-08-01 docs. Test against one real, unimportant SKU first
        if at all possible, or at minimum read the printed response on
        the very first real delete and confirm it matches before
        trusting this for anything that matters.
        """
        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return {"success": False, "error": f"Unknown marketplace: {marketplace}"}

        if not self.seller_id:
            return {"success": False, "error": "SP_API_SELLER_ID not configured in .env"}

        try:
            access_token = self._get_access_token()
        except Exception as exc:
            return {"success": False, "error": f"Token refresh failed: {exc}"}

        self._pace(self.LISTINGS_MIN_REQUEST_INTERVAL_SECONDS)

        try:
            resp = requests.delete(
                f"{EU_ENDPOINT}/listings/2021-08-01/items/{self.seller_id}/{sku}",
                headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                params={"marketplaceIds": marketplace_id, "issueLocale": "en_GB"},
                timeout=30,
            )
        except Exception as exc:
            return {"success": False, "error": f"Request failed: {exc}"}

        if resp.status_code not in (200, 202):
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}

        try:
            body = resp.json()
        except Exception:
            body = {}

        issues = body.get("issues") or []
        error_issues = [i for i in issues if (i.get("severity") or "").upper() == "ERROR"]

        if error_issues:
            return {"success": False, "error": "; ".join(i.get("message", "") for i in error_issues)}

        return {"success": True, "error": ""}


_cached_client = None
_credentials_missing_logged = False


def get_sp_api_client():
    """
    Cached singleton, same convention as app/keepa/client.py's
    get_keepa_client() -- avoids re-doing the LWA token exchange on
    every call. Returns None (never raises) if SP_API_* isn't fully
    configured in .env -- SP-API is an OPTIONAL cost-saving layer on
    top of Keepa (see brand_scan_service.py Step 4), not a hard
    dependency, so a missing/incomplete credential set makes Atlas
    fall back to Keepa-only behaviour rather than crash.
    """
    global _cached_client, _credentials_missing_logged

    if _cached_client is not None:
        return _cached_client

    client_id = os.getenv("SP_API_CLIENT_ID")
    client_secret = os.getenv("SP_API_CLIENT_SECRET")
    refresh_token = os.getenv("SP_API_REFRESH_TOKEN")
    # Only required for delete_listing_item (Out of Stock Cleanup,
    # 2026-09-01) -- left unset, every existing SP-API feature
    # (getItemOffers/searchCatalogItems, and the new
    # getInventorySummaries/fulfilled-shipments-report calls) keeps
    # working exactly as before.
    seller_id = os.getenv("SP_API_SELLER_ID", "")

    if not (client_id and client_secret and refresh_token):
        if not _credentials_missing_logged:
            print("SP-API credentials not configured -- skipping SP-API pre-checks, Keepa-only.")
            _credentials_missing_logged = True
        return None

    _cached_client = SPAPIClient(client_id, client_secret, refresh_token, seller_id)
    return _cached_client
