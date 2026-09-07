"""
20-CASE KNOWN-GOOD / KNOWN-BAD BENCHMARK -- 2026-09-05.

Per Tamara's explicit instruction: "Start by auditing the existing
implementation and running the 20-product known-good/known-bad
benchmark. Do not make production changes until the benchmark results
are reviewed."

This is the full validation run for the cost-reduction pipeline
(text-only triage -> selective verification -> Brave fallback) proven
out in oa_research_agent_cost_benchmark.py's Test A / Test B, now
against a REAL confusion matrix:

  - 10 KNOWN-BAD cases: oa_research_agent_prototype.BENCHMARK_CASES,
    real documented Atlas false-positive incidents (unchanged from the
    original benchmark, per Tamara's own "don't change the benchmark
    products" instruction).
  - 10 KNOWN-GOOD cases: oa_research_agent_known_good_cases.
    KNOWN_GOOD_CASES, independently hand-verified real Atlas products
    (see that file's own docstring for full provenance and the
    difficulty encountered building it).

Reuses run_test_a / run_test_b UNCHANGED from
oa_research_agent_cost_benchmark.py -- same verification rules, same
prompts, same tool budgets, byte-identical judgment logic. The only
thing this script adds is: (a) the known-good cases, (b) a confusion-
matrix report computing the metric Tamara called out as most
important -- false-reject rate on genuinely good matches -- alongside
false-positive rate on the known-bad set.

THIS SCRIPT DOES NOT TOUCH ATLAS'S DATABASE. Same read-only guarantees
as its parent scripts: no Keepa/SP-API, no scheduler, no scan, no
Atlas DB writes, no live opportunity creation.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... python oa_research_agent_20case_benchmark.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from oa_research_agent_cost_benchmark import (  # noqa: E402
    BENCHMARK_CASES as KNOWN_BAD_CASES,
    run_test_a, run_test_b, MODEL,
)
from oa_research_agent_known_good_cases import KNOWN_GOOD_CASES  # noqa: E402

# -----------------------------------------------------------------------
# Outcome interpretation -- what "correct" means for each ground-truth
# label, at each stage. Kept here (not in the shared runner) since this
# is scoring logic specific to having ground truth, not part of the
# pipeline under test.
# -----------------------------------------------------------------------
GOOD_SUCCESS_FINAL = {"TEXT_PASS_VERIFIED", "VERIFIED"}
BAD_SUCCESS_FINAL = {"TEXT_REJECT", "NO_USEFUL_SERPAPI_EVIDENCE", "TEXT_PASS_REJECTED_AFTER_FETCH", "REJECTED_AFTER_FETCH", "FALSE_NEGATIVE_MISSED"}


def _a1_classification(result: dict) -> str:
    stage_a1 = result.get("stage_a1") or {}
    return stage_a1.get("stage_classification") or (
        "TEXT_REJECT" if stage_a1.get("rejected_candidates")
        else "TEXT_PASS" if stage_a1.get("candidates")
        else "NO_USEFUL_SERPAPI_EVIDENCE"
    )


def score_results(results: list, is_known_good: bool) -> list:
    scored = []
    for r in results:
        a1_class = _a1_classification(r)
        a1_wrongly_rejected = is_known_good and a1_class in ("TEXT_REJECT", "NO_USEFUL_SERPAPI_EVIDENCE")
        final = r["final_classification"]
        if is_known_good:
            correct = final in GOOD_SUCCESS_FINAL
        else:
            correct = final in BAD_SUCCESS_FINAL
        scored.append({
            "asin": r["asin"], "a1_classification": a1_class,
            "a1_wrongly_rejected_good_match": a1_wrongly_rejected,
            "final_classification": final, "correct": correct,
            "cost_usd": r["usage"]["cost_usd"], "error": r.get("error"),
        })
    return scored


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    all_cases = [(c, True) for c in KNOWN_GOOD_CASES] + [(c, False) for c in KNOWN_BAD_CASES]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "model": MODEL,
        "n_known_good": len(KNOWN_GOOD_CASES), "n_known_bad": len(KNOWN_BAD_CASES),
        "test_a": [], "test_b": [],
    }

    for test_name, run_fn in (("test_a", run_test_a), ("test_b", run_test_b)):
        print(f"\n{'=' * 100}\nRunning {test_name.upper()} on all 20 cases (10 known-good + 10 known-bad)...\n")
        for i, (case, is_good) in enumerate(all_cases, 1):
            label = "GOOD" if is_good else "BAD "
            print(f"[{test_name} {i}/20] [{label}] {case['asin']}")
            result = run_fn(client, case)
            result["case"] = case
            result["is_known_good"] = is_good
            report[test_name].append(result)
            print(f"    -> {result['final_classification']} | cost: ${result['usage']['cost_usd']}")
            if result.get("error"):
                print(f"    ERROR: {result['error']}")

    for test_name in ("test_a", "test_b"):
        results = report[test_name]
        good_results = [r for r in results if r["is_known_good"]]
        bad_results = [r for r in results if not r["is_known_good"]]
        scored_good = score_results(good_results, is_known_good=True)
        scored_bad = score_results(bad_results, is_known_good=False)

        n_good = len(scored_good)
        n_bad = len(scored_bad)
        n_false_reject_final = sum(1 for s in scored_good if not s["correct"])
        n_false_reject_at_a1 = sum(1 for s in scored_good if s["a1_wrongly_rejected_good_match"])
        n_false_positive = sum(1 for s in scored_bad if not s["correct"])
        total_cost = sum(r["usage"]["cost_usd"] for r in results)

        report[f"{test_name}_confusion_matrix"] = {
            "false_reject_rate_final": round(n_false_reject_final / n_good, 3) if n_good else None,
            "false_reject_rate_at_text_triage_a1": round(n_false_reject_at_a1 / n_good, 3) if n_good else None,
            "false_positive_rate": round(n_false_positive / n_bad, 3) if n_bad else None,
            "true_positive_rate": round((n_good - n_false_reject_final) / n_good, 3) if n_good else None,
            "true_negative_rate": round((n_bad - n_false_positive) / n_bad, 3) if n_bad else None,
            "n_known_good_wrongly_rejected_final": n_false_reject_final,
            "n_known_good_wrongly_rejected_at_a1": n_false_reject_at_a1,
            "n_known_bad_wrongly_verified": n_false_positive,
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_asin_usd": round(total_cost / len(results), 4) if results else 0,
            "known_good_detail": scored_good,
            "known_bad_detail": scored_bad,
        }

    out_path = os.path.join(os.path.dirname(__file__), "oa_research_agent_20case_benchmark_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n" + "=" * 100)
    for test_name in ("test_a", "test_b"):
        cm = report[f"{test_name}_confusion_matrix"]
        print(f"\n{test_name.upper()}:")
        print(f"  False-reject rate (final, most important metric): {cm['false_reject_rate_final']} "
              f"({cm['n_known_good_wrongly_rejected_final']}/{report['n_known_good']} genuinely good matches wrongly thrown away)")
        print(f"  False-reject rate (at text-triage A1 stage specifically): {cm['false_reject_rate_at_text_triage_a1']} "
              f"({cm['n_known_good_wrongly_rejected_at_a1']}/{report['n_known_good']})")
        print(f"  False-positive rate: {cm['false_positive_rate']} "
              f"({cm['n_known_bad_wrongly_verified']}/{report['n_known_bad']} known-bad matches wrongly verified)")
        print(f"  Total cost: ${cm['total_cost_usd']} (avg ${cm['avg_cost_per_asin_usd']}/ASIN)")
    print(f"\nFull report written to: {out_path}")
    print("=" * 100)


if __name__ == "__main__":
    main()
