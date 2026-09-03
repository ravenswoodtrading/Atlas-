# Atlas: retailer feed sourcing (Awin) — ABANDONED, DO NOT BUILD

**Superseded by `atlas-oa-scale-up-spec.md` (28 Aug 2026). Build that instead.**

This plan was to ingest UK retailer product feeds from Awin, match EAN to ASIN via
Amazon's free SP-API, and only spend Keepa tokens on products that cleared a profit
headroom test. The funnel logic was sound. The data source was not available.

Why it was dropped, so nobody re-derives it:

- **Awin** feeds are not available on signing up. Product feeds are per-programme — you
  must join each retailer's programme individually and be approved by that retailer.
  Approval expects promotional activity in return, which isn't what this was for.
- **Shopify's public `/products.json`** is genuinely open and needs no auth, but it omits
  the `barcode` field — that's Admin-API only. You get SKU, not EAN.
- **Sitemap + JSON-LD crawling** yields `gtin13` only when a retailer publishes it, and
  most don't.

The common failure is the same in each case: getting EANs for UK retailer catalogues at
scale isn't practical. Any future revival of catalogue-first sourcing has to solve that
first, not assume it.

**Worth keeping from this spec:** the idea of screening candidates with free SP-API calls
(`searchCatalogItems` for rank, `getItemOffers` for price) before spending Keepa tokens.
That survives in §3.4 of the new spec.
