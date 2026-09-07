"""
KNOWN-GOOD ground truth for the 20-case OA Research Agent benchmark --
2026-09-05.

Companion to oa_research_agent_prototype.py's BENCHMARK_CASES (the 10
KNOWN-BAD documented SerpAPI mismatches). These 10 are the other half:
real Atlas-scanned products where a genuine UK retail source has been
independently verified BY HAND -- exact product/variant/condition and
current price checked directly on the retailer's own page via a plain
read-only browser session, with NO involvement from the AI pipeline
this benchmark exists to test. That independence is the whole point:
grading the researcher's homework with an answer key it helped write
would prove nothing.

Where these came from matters, so `provenance` is explicit on every
case:
  - "real_oa_pipeline": ASIN was already a live Atlas oa_source_
    candidates row (i.e. SerpAPI itself found it); independently
    re-verified by hand before being trusted as ground truth.
  - "independently_constructed": ASIN is a real Atlas-scanned product
    (real_oa_pipeline's candidate pool for this ASIN was empty or
    unrelated); the retailer match was found and verified from scratch
    via Brave search + a manual page read, with NO SerpAPI/AI
    involvement at all.

Finding, in the course of building this list (reported to the user
before running the benchmark): of ~12 real Atlas oa_source_candidates
rows checked by hand for a possible known-good case, only ONE
(B08VN7CSTJ, kept below) turned out to be a genuinely clean match on
close reading -- the other ~11 had hidden variant/spec/pack/condition
mismatches invisible without reading exact title text, or (twice) had
"Amazon.co.uk" itself as the "source" despite that already being on
the exclusion list. That is itself a significant finding about how
dominated Atlas's real brand_title-tier candidate pool is by false
positives -- see the accompanying audit note. It is also why 9 of the
10 cases below are independently_constructed rather than pulled from
the real pipeline: real known-good SerpAPI matches are scarce.

Per Tamara's explicit request, this set deliberately spans three
difficulty bands rather than only clean textbook matches:
  - "obvious_clean": exact EAN/MPN in the retailer's own title, no
    ambiguity at all.
  - "messy_but_correct": the retailer's title genuinely differs in
    wording/branding from Amazon's, but is confirmed the same product
    (MPN/spec match) -- the case that most risks a false reject from a
    text-only triage that pattern-matches on title similarity.
  - "borderline_but_correct": something about the case looks risky on
    the surface (a suspiciously large discount, a stock-status
    complication, a price that's actually ABOVE Amazon) that could
    tempt a triage into over-rejecting or a verifier into wrongly
    calling it non-actionable -- while the underlying PRODUCT MATCH
    itself is genuinely correct. Two of these are deliberately priced
    at or above Amazon: testing "does the system correctly say this
    is a real match but NOT an opportunity" is exactly the source-
    found-vs-opportunity distinction the whole project is about.

All GBP prices below were read directly off each retailer's live page
by hand (see the note on each case for exactly when/how) -- these are
NOT re-fetched at benchmark run time, so a real production run may see
different current prices/stock than recorded here. Amazon prices are
straight from Atlas's own product_records.buy_box_now for that ASIN.
"""

KNOWN_GOOD_CASES = [
    {
        "asin": "B08VN7CSTJ",
        "title": 'Milwaukee 2962-20 M18 18V Fuel 1/2" Mid-Torque Impact Wrench with Friction Ring',
        "brand": "Milwaukee", "ean": "0045242568925", "mpn": "2962-20",
        "amazon_price_gbp": 218.75,
        "serpapi_retailer": "CEF", "serpapi_price": 155.94, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": 'Milwaukee 2962-20 M18 Fuel 1/2" Mid-torque Impact Wrench with Friction Ring',
        "provenance": "real_oa_pipeline",
        "case_category": "obvious_clean",
        "known_good_reasoning": "Real Atlas oa_source_candidates row (brand_mpn tier). Re-verified by hand: CEF's title and MPN '2962-20' match Amazon's exactly -- no variant/spec ambiguity. Genuinely ~29% below Amazon.",
    },
    {
        "asin": "B08YMWMW5P",
        "title": "Bosch ErgoMix Hand Blender MS6CA4150G with mini chopper, whisk, 12 speeds, 800 W White/Anthracite",
        "brand": "Bosch", "ean": "4242005207749", "mpn": "MS6CA4150G",
        "amazon_price_gbp": 49.99,
        "serpapi_retailer": "Argos", "serpapi_price": 34.99, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "Bosch MS6CA4150G ErgoMixx Hand Blender - White and Grey",
        "provenance": "independently_constructed",
        "case_category": "obvious_clean",
        "known_good_reasoning": "Manually verified live on argos.co.uk (product 8136921): exact model code MS6CA4150G, same colourway. Also confirmed at John Lewis and Currys under the identical model code. Genuinely ~30% below Amazon.",
    },
    {
        "asin": "B085G1Y8FN",
        "title": 'Wera 416 R T-Handle Bit-Holding S/Driver Rapidaptor - 1/4" x 45mm',
        "brand": "Wera", "ean": "", "mpn": "05023404001",
        "amazon_price_gbp": 20.99,
        "serpapi_retailer": "PowerToolMate", "serpapi_price": 18.99, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": 'Wera 416 R T-handle bitholding screwdriver Rapidaptor, 1/4" x 45 mm, 05023404001',
        "provenance": "independently_constructed",
        "case_category": "obvious_clean",
        "known_good_reasoning": "Manually verified live on powertoolmate.co.uk: exact MPN 05023404001 in the title, in stock. Modest but genuine ~9.5% below Amazon (a small margin worth flagging -- fees could plausibly erase it, which is itself a useful thing for the verifier to notice).",
    },
    {
        "asin": "B088HWR46P",
        "title": "Makita DMR301 DAB/DAB+ Job Site Radio with Bluetooth – Batteries and Charger Not Included",
        "brand": "Makita", "ean": "0088381899550", "mpn": "DMR301",
        "amazon_price_gbp": 249.99,
        "serpapi_retailer": "Power Tools UK", "serpapi_price": 196.00, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "MAKITA DMR301 JOB SITE RADIO DAB/DAB+ CHARGER RADIO 12V 18V BODY ONLY",
        "provenance": "independently_constructed",
        "case_category": "obvious_clean",
        "known_good_reasoning": "Manually verified live on powertoolsuk.co.uk: exact model code DMR301, 'BODY ONLY' config matches Amazon's own 'Batteries and Charger Not Included'. In stock. Genuinely ~22% below Amazon.",
    },
    {
        "asin": "B005C6FHDW",
        "title": "Brother TZe-N241 Labelling Tape Cassette, Black on White, 18mm (W) x 8M (L), Non-Laminated, Brother Genuine Supplies",
        "brand": "Brother", "ean": "4977766052405", "mpn": "TZE-N241",
        "amazon_price_gbp": 16.99,
        "serpapi_retailer": "Ink n Toner UK", "serpapi_price": 14.31, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "Original Brother TZe-N241 Black On White 18mm x 8m Non-Laminated P-Touch Label Tape (TZEN241)",
        "provenance": "independently_constructed",
        "case_category": "obvious_clean",
        "known_good_reasoning": "Manually verified live on inkntoneruk.co.uk: exact SKU TZEN241, single cassette both sides (no pack-size ambiguity -- deliberately picked after a DYMO D1 candidate was rejected during research for exactly this ambiguity, see audit note). Genuinely ~16% below Amazon.",
    },
    {
        "asin": "B009K2QX0U",
        "title": "CTEK 56-870 Comfort Indicator Cig Plug",
        "brand": "CTEK", "ean": "", "mpn": "56-870",
        "amazon_price_gbp": 23.98,
        "serpapi_retailer": "Smarter Chargers", "serpapi_price": 18.71, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "CTEK 56-870 Comfort Connect Cig-Plug",
        "provenance": "independently_constructed",
        "case_category": "messy_but_correct",
        "known_good_reasoning": "Manually verified live on smarterchargers.co.uk: same exact MPN 56-870, but CTEK's own retail branding calls this 'Comfort Connect' where Amazon's listing calls it 'Comfort Indicator' -- genuinely the same accessory under two different marketing names CTEK itself uses inconsistently, not a different product. A title-similarity-only triage could plausibly flag this as a mismatch. Genuinely ~22% below Amazon.",
    },
    {
        "asin": "B07PDBJWQ4",
        "title": "Milwaukee Magnetic REDSTICK Slim Box Level 40cm, Red",
        "brand": "Milwaukee", "ean": "4058546227999", "mpn": "4932464854",
        "amazon_price_gbp": 24.42,
        "serpapi_retailer": "Tradefix Direct", "serpapi_price": 24.08, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "Milwaukee Redstick SLIM Magnetic Box Level – 40cm",
        "provenance": "independently_constructed",
        "case_category": "messy_but_correct",
        "known_good_reasoning": "Manually verified live on tradefixdirect.com: word order differs ('Redstick SLIM Magnetic Box Level' vs Amazon's 'Magnetic REDSTICK Slim Box Level'), same length, both explicitly magnetic (a genuinely non-magnetic 'REDSTICK Non-Magnetic' variant of this same level exists at a different retailer and was deliberately excluded during research -- see audit note). Only ~1.4% below Amazon: correct match, but the margin is thin enough that fees would likely erase it -- a useful non-actionable-but-genuine case.",
    },
    {
        "asin": "B005HVY5I0",
        "title": "DeWalt DS150 Toughsystem Organiser Box (Without Inlay)",
        "brand": "DEWALT", "ean": "0604310245382", "mpn": "1-70-321",
        "amazon_price_gbp": 17.99,
        "serpapi_retailer": "UK Planet Tools", "serpapi_price": 19.99, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "DeWalt 1-70-321 DS150 XR Toughsystem Organiser Stackable Box",
        "provenance": "independently_constructed",
        "case_category": "borderline_but_correct",
        "known_good_reasoning": "Manually verified live on ukplanettools.co.uk: exact MPN 1-70-321 / model DS150, in stock -- a completely genuine match. Deliberately priced ABOVE Amazon (£19.99 vs £17.99): this is a 'verified source, correctly NOT an opportunity' case, testing whether the pipeline correctly stops at source-found rather than inventing a BUY_NOW.",
    },
    {
        "asin": "B00OYVM1S0",
        "title": "M18 B4 REDLITHIUM-ION™ Slide Battery Pack 18V 4.0Ah Li-ion",
        "brand": "Milwaukee", "ean": "4002395377244", "mpn": "M18B4",
        "amazon_price_gbp": 47.50,
        "serpapi_retailer": "UK Planet Tools", "serpapi_price": 41.99, "serpapi_match_tier": "brand_mpn",
        "serpapi_retailer_title": "Milwaukee M18B4 18V M18 Fuel Red Lithium Ion 4.0Ah Battery",
        "provenance": "independently_constructed",
        "case_category": "borderline_but_correct",
        "known_good_reasoning": "Manually verified live on ukplanettools.co.uk: exact SKU M18B4, genuinely ~12% below Amazon -- BUT the listing was OUT OF STOCK at the moment of manual verification. Genuine product match with a real current-availability complication: tests whether the pipeline distinguishes 'wrong product' from 'right product, temporarily unavailable' rather than conflating the two as equally 'no source'.",
    },
    {
        "asin": "B0FG2LVH8Z",
        "title": "Vivactive Slip Maxi XL (4100ml) 15 Pack - Adult Nappies",
        "brand": "Vivactive", "ean": "5056287733464", "mpn": "",
        "amazon_price_gbp": 25.00,
        "serpapi_retailer": "Incontinence Choice", "serpapi_price": 10.65, "serpapi_match_tier": "brand_title",
        "serpapi_retailer_title": "Vivactive Slip Maxi XL (4100ml) 15 Pack",
        "provenance": "independently_constructed",
        "case_category": "borderline_but_correct",
        "known_good_reasoning": "Manually verified live on incontinencechoice.co.uk: title is a byte-for-byte match including the specific absorbency rating (4100ml) AND pack count (15), which together make an accidental same-title-different-product coincidence very unlikely. Genuinely ~57% below Amazon -- a discount large enough that an AI verifier might reflexively distrust it as 'too good to be true' and wrongly reject a real find. No MPN was available to cross-check (niche medical product), which is itself part of what makes this a good borderline case.",
    },
]
