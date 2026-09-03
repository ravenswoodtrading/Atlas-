# ATLAS — REVIEW QUEUE BACKEND CHANGES V1

## Objective

Improve the Atlas Review Queue so that it reflects how we actually make sourcing decisions.

The goal is NOT to redesign the UI yet.

The goal is to improve the backend/data/decision logic so that the future UI can reliably answer:

1. What should I buy?
2. What needs my review?
3. Is the opportunity still actually buyable?
4. Why was something rejected?
5. What can Atlas learn from my review decisions?

DO NOT rebuild Atlas.
DO NOT change unrelated sourcing logic.
DO NOT change the UI as part of this task except where a very small change is required to expose backend functionality for testing.

---

# 1. IMPORTANT CONTEXT

A major problem with the current Review Queue is that Atlas can identify a profitable opportunity correctly when it checks it, but the Amazon source offer can disappear very quickly afterwards.

For example:

10:00 — Atlas checks Germany:
- Source price = £10
- UK selling price = £30
- Profit = £10
- Atlas recommends BUY

10:20 — The Amazon Germany offer sells out.

10:25 — User clicks the source link.

There is now:
- no buyable offer
- no Featured Offer
- or the price has changed significantly

The user therefore rejects the lead.

This does NOT necessarily mean Atlas made a bad sourcing decision. It may simply mean the opportunity became stale between Atlas's check and the user's purchase.

This distinction is important.

---

# 2. DO NOT TREAT USER REJECTION AS PROOF THAT THE ORIGINAL ATLAS DECISION WAS WRONG

A rejection such as "No buyable offer" should NOT automatically mean "Atlas incorrectly recommended BUY."

Atlas needs to distinguish between:

### Decision at time of analysis

and

### Current state when the user reviews it

Example:

Original Atlas analysis:
- BUY
- Source price £10.00
- Profit £12.50
- Offer confirmed

User review:
- NO BUYABLE OFFER

Record conceptually as:
- Original decision: BUY
- Review outcome: REJECT
- Review reason: NO_BUYABLE_OFFER
- Interpretation: OPPORTUNITY_EXPIRED

This is different from:
- Original decision: BUY
- Review outcome: REJECT
- Reason: WRONG_MATCH

The first may indicate timing/offer volatility. The second may indicate a sourcing/matching problem.

---

# 3. OFFER FRESHNESS

Introduce a concept of offer freshness.

The system already performs source/product checks. Inspect the existing implementation and reuse existing timestamps/status fields wherever possible.

Do NOT create duplicate data unnecessarily.

We need to be able to determine:
- when the source was last checked
- what source price was found
- whether the offer was buyable
- whether a Featured Offer existed where relevant
- whether the opportunity was profitable at that point
- how old that check is

The Review Queue should be able to classify an opportunity approximately as:

### FRESH
Recently checked and source offer confirmed.

### AGING
Offer was confirmed but the check is becoming old.

### STALE
The source check is old enough that the user should not rely on the original offer.

### UNAVAILABLE
The most recent check indicates that the source offer is no longer available.

Use sensible thresholds based on the existing Atlas architecture. Do NOT invent arbitrary thresholds without first inspecting how Atlas currently performs source checks. Make thresholds configurable where practical.

---

# 4. RECHECK BEFORE PURCHASE

This is one of the most important future capabilities.

When the user opens a BUY opportunity, Atlas should eventually be able to perform a lightweight current source check.

Example:

Original:
£10.00
BUY

Current check:
£10.00
BUYABLE
→ proceed

OR:
£14.99
No longer profitable
→ tell user immediately

OR:
No buyable offer
→ tell user immediately

The system should not silently change the historical decision.

Instead show conceptually:

ORIGINAL ATLAS DECISION
BUY

CURRENT CHECK
NO BUYABLE OFFER

If implementing a live recheck requires APIs or infrastructure that are not currently available, do not invent it. Document the required capability and implement the data structures/service interface so it can be added safely.

---

# 5. STRUCTURED REVIEW REASONS

The current database contains many free-text review reasons, including:
- No buyable offer
- No buyable offer in france
- price has gone up
- Too much stock
- Already bought
- Too expensive to buy from EU
- Over import duty
- wrong match
- gated
- not enough profit
- frequently returned item
- EU plug

These must remain compatible with existing data.

DO NOT delete historical review_reason text.

Instead, introduce a structured reason/category where appropriate.

Suggested categories:

NO_BUYABLE_OFFER
PRICE_CHANGED
NO_LONGER_PROFITABLE
ALREADY_BOUGHT
TOO_MUCH_STOCK
TOO_EXPENSIVE
IMPORT_DUTY
WRONG_MATCH
GATED
INSUFFICIENT_SALES
INSUFFICIENT_PROFIT
PRODUCT_RISK
EU_PLUG
EBAY
OTHER

The exact database field/name should follow Atlas conventions after inspecting the existing models.

Existing free text should continue to be stored.

Example:
- review_reason_category: NO_BUYABLE_OFFER
- review_reason: "No buyable offer in France"

---

# 6. REVIEW OUTCOME VS REVIEW REASON

These should be treated as separate concepts.

Examples:

Outcome: REJECT
Reason: NO_BUYABLE_OFFER

Outcome: REJECT
Reason: WRONG_MATCH

Outcome: BUY
Reason: none

Outcome: WATCH
Reason: PRICE_RECOVERY

Do not overload one field to represent all of these concepts.

Inspect the current schema before adding fields. Reuse existing concepts where they already exist.

---

# 7. REVIEW QUEUE STATES

The backend should distinguish between:

BUY
CONSIDER
WATCH
REJECTED
EXPIRED / STALE
OUT_OF_STOCK

Do not necessarily create a completely new database enum if the current system already has equivalent concepts.

Map the new behaviour onto the existing architecture where possible.

Important:

An opportunity becoming unavailable should not necessarily be treated as a permanent rejection.

For example:

BUY → source offer disappears

should become something closer to:

EXPIRED / TEMPORARILY_UNAVAILABLE

rather than destroying the original BUY decision.

This matters because the opportunity may become viable again.

---

# 8. RE-SCAN / RE-EVALUATION

Atlas already has scanning and source checking infrastructure.

Inspect:
- review_queue_service
- scan_queue_service
- scan_coordinator
- oa_lookup_service
- source checking services
- Keepa-related services
- relevant routes/models

before implementing anything.

Where an opportunity becomes stale, Atlas should be able to re-evaluate it rather than requiring a completely new lead.

Desired lifecycle:

DISCOVERED
↓
ANALYSED
↓
BUY
↓
WAITING FOR USER
↓
SOURCE CHANGED / UNAVAILABLE
↓
STALE
↓
RECHECK
↓
BUY AGAIN

or:

STALE
↓
RECHECK
↓
NO LONGER PROFITABLE
↓
REJECT / REMOVE

---

# 9. IMPORTANT: PRESERVE ORIGINAL ANALYSIS

Never overwrite the original analysis simply because the current market has changed.

Example:

30 August:
- Germany source £10
- UK price £30
- BUY

2 September:
- Germany £18
- UK £20
- not profitable

The record should retain the original analysis.

We need to know:

Original source price
Original UK price
Original profit
Original recommendation
Original check timestamp

and separately:

Current source price
Current UK price
Current profitability
Current availability
Current check timestamp

This is essential for later analysis of Atlas accuracy.

---

# 10. REVIEW HISTORY

Every user review should be traceable.

We need to be able to determine:
- what Atlas originally recommended
- when it recommended it
- what the user decided
- when the user decided
- the user's structured reason
- the user's free-text comment
- whether the source was subsequently rechecked
- what happened afterwards

Do not destroy previous review history when a product is rescanned.

If Atlas currently updates records in place, inspect this carefully before changing it.

---

# 11. LEARNING FROM REJECTION REASONS

The immediate task is NOT to create machine learning.

However, the data should be structured so Atlas can eventually answer:

- What percentage of BUY recommendations are rejected because the source offer disappeared?
- What percentage are rejected because the price changed?
- How often is Atlas wrong on matching?
- Which retailers frequently lose offers?
- Which source countries have the highest offer volatility?
- How often does a BUY become a genuine purchase?

This will allow Atlas to improve based on evidence rather than guesses.

---

# 12. CURRENT DATA OBSERVATION

A recent analysis of the Atlas database showed many BUY records marked as:

review = down
recommendation = BUY

with review reasons frequently blank.

There were also numerous historical free-text rejection reasons.

This means we should be careful not to assume that the current database structure fully captures why a BUY was rejected.

Previous diagnostic scripts also attempted to use a `scan_date` column which does not exist in the current `product_records` schema.

Therefore:

DO NOT assume column names from diagnostic scripts.

Inspect the actual database schema/models first.

---

# 13. TESTING REQUIREMENTS

Before changing production behaviour, create or update tests covering at least:

### Test 1 — Fresh profitable offer
Source £10, UK £30, offer buyable → BUY / FRESH.

### Test 2 — Offer becomes unavailable
Original BUY, current no buyable offer → original BUY preserved; current status UNAVAILABLE / STALE.

### Test 3 — Source price rises
Original £10, current £18, current profit below threshold → original BUY preserved; current status NO_LONGER_PROFITABLE.

### Test 4 — User rejects because no offer
Original BUY, user REJECT, reason NO_BUYABLE_OFFER → structured review reason stored.

### Test 5 — User rejects because wrong match
Original BUY, user REJECT, reason WRONG_MATCH → structured reason stored.

### Test 6 — Recheck restores profitability
Original BUY → source unavailable → source later returns at profitable price → opportunity can return to BUY/actionable state.

Do not duplicate the lead unnecessarily if the existing record can safely be reactivated.

---

# 14. BACKWARDS COMPATIBILITY

This is critical.

Existing Atlas data must continue to work.

Historical records must not be deleted.

Existing free-text review reasons must remain available.

Existing Review Queue functionality must continue to work.

Existing BUY/CONSIDER/WATCH workflows must not be broken.

Existing VA leads must not be broken.

Existing competitor leads must not be broken.

Existing scan leads must not be broken.

---

# 15. IMPLEMENTATION PROCESS

Before writing code:

1. Inspect the current database schema.
2. Inspect the relevant SQLAlchemy models.
3. Inspect `review_queue_service.py`.
4. Inspect `review_queue.py`.
5. Inspect existing source/offer checking services.
6. Inspect existing review persistence.
7. Inspect current tests.
8. Identify what functionality already exists.
9. Produce a short implementation plan.
10. Only then make changes.

Do not make speculative changes.

---

# 16. FILES TO INVESTIGATE FIRST

Start with:

backend/app/services/review_queue_service.py
backend/app/routes/review_queue.py
backend/app/database/models.py
backend/app/services/oa_lookup_service.py
backend/app/services/oa_source_discovery_service.py
backend/app/services/verdict_service.py
backend/app/services/verdict_run_service.py
backend/app/services/scan_queue_service.py
backend/app/services/scan_coordinator.py

Also inspect the existing Review Queue / verdict tests and the actual SQLite schema for `atlas.db`.

Do not assume the schema based on old scripts.

---

# 17. DO NOT DO YET

Do NOT:
- redesign the Review Queue UI
- redesign the Home page
- rebuild competitor watch
- change the competitor A2A classification
- change VA logic
- change scoring thresholds
- change Keepa token strategy
- rewrite the database
- remove old routes
- remove old fields
- introduce machine learning
- refactor unrelated services

Those are separate changes.

---

# 18. SUCCESS CRITERIA

### For the user

A BUY opportunity clearly represents:

"Atlas found this as a BUY when it checked."

and separately:

"This is what is happening now."

The user should not have to guess whether Atlas made a bad decision or the Amazon offer simply disappeared.

### For Atlas

Atlas can distinguish:
- original recommendation
- current source state
- freshness
- user review outcome
- structured review reason
- historical review information

### For future development

The data is clean enough to support:
- better Review Queue UI
- purchase-time rechecks
- offer volatility analysis
- Atlas accuracy analysis
- automated reactivation of temporarily unavailable opportunities
- improved sourcing decisions

---

# 19. FINAL INSTRUCTION

DO NOT implement everything in this specification blindly.

First inspect the current Atlas implementation and tell me:

1. What already exists.
2. What is missing.
3. Which files need changing.
4. Which database changes are genuinely necessary.
5. What risks there are to existing functionality.
6. What tests already cover this.
7. What new tests should be added.

Then implement the smallest safe change set.

After implementation, run the relevant tests and report:
- files changed
- database changes
- tests run
- test results
- remaining limitations

Do not move onto the UI redesign until this backend change has been tested and confirmed stable.
