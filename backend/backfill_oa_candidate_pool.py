"""
One-off run of the step-2 free screen (see
app/services/oa_candidate_screener.py) against ASINs the existing
Competitor Watch pipeline already scored -- populates oa_candidate_pool
and prints a summary so you can judge whether the free screen rejects
sensibly BEFORE step 4 lets it gate brand-new ASINs with no Keepa
fallback. Run with `python backfill_oa_candidate_pool.py`.

Safe to re-run -- each run adds new rows rather than overwriting an
old run, so you can compare runs over time as MAX_SALES_RANK_UK gets
tuned (see oa_candidate_screener.py).

Needs SP_API_CLIENT_ID/SP_API_CLIENT_SECRET/SP_API_REFRESH_TOKEN
configured in .env -- without them, every row screens out with reason
"sp_api_unavailable" (get_sp_api_client() returns None), which is
itself a useful signal that this hasn't actually run for real yet.
"""
from app.database.base import Base
from app.database.database import engine
from app.database import models  # noqa: F401 -- registers every table (incl. OaCandidatePool) with Base.metadata, same convention as app/main.py
from app.services.oa_candidate_screener import screen_backfill_candidates

if __name__ == "__main__":
    # Normally the Atlas server does this at startup (see main.py) --
    # a standalone script like this one never triggers that, so
    # oa_candidate_pool (a brand-new table) would otherwise not exist
    # yet the first time this runs. Only CREATES missing tables, never
    # touches existing ones -- same call, same guarantee, as main.py's.
    Base.metadata.create_all(bind=engine)

    summary = screen_backfill_candidates(limit=200)

    print(f"\nScreened {summary['total']} ASINs already known to Competitor Watch:")
    print(f"  screened_in:  {summary['screened_in']}")
    print(f"  screened_out: {summary['screened_out']}")

    if summary["reasons"]:
        print("\nscreened_out reasons:")
        for reason, count in sorted(summary["reasons"].items(), key=lambda r: -r[1]):
            print(f"  {reason}: {count}")

    print("\nFull detail is in the oa_candidate_pool table. Check screened_in ASINs")
    print("against what you already know about them, and screened_out ones against")
    print("whether you'd actually have wanted them found -- especially any")
    print("'rank_too_high' or 'no_headroom' rows, since those are the free screen")
    print("disagreeing with a real, already-scored competitor detection. Adjust")
    print("MAX_SALES_RANK_UK in oa_candidate_screener.py once you've seen the real")
    print("rank distribution, per the spec's own 'start loose' guidance (§11).")
