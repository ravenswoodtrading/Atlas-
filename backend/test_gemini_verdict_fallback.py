"""
Regression tests for the Gemini fallback in generate_verdict, 2026-09-06.

Run with `python test_gemini_verdict_fallback.py` (same plain-script
convention as the other test_*.py files, no pytest, no real Claude/
Gemini API calls -- _call_claude_for_verdict/_call_gemini_for_verdict
are monkey-patched directly).
"""
import app.services.anthropic_client as ac

SAMPLE_METRICS = {"title": "Test Product", "brand": "TestBrand"}


def test_falls_back_to_gemini_when_claude_fails():
    original_claude = ac._call_claude_for_verdict
    original_gemini = ac._call_gemini_for_verdict
    try:
        def fake_claude_fails(prompt):
            raise RuntimeError("insufficient_quota: your account has run out of credits")

        def fake_gemini_succeeds(prompt):
            return "BUY\n+ Strong ROI at 30%\n+ Steady sales rank drops"

        ac._call_claude_for_verdict = fake_claude_fails
        ac._call_gemini_for_verdict = fake_gemini_succeeds

        verdict, rationale = ac.generate_verdict(SAMPLE_METRICS)
        assert verdict == "BUY"
        assert "Gemini fallback" in rationale
        assert "Strong ROI at 30%" in rationale
    finally:
        ac._call_claude_for_verdict = original_claude
        ac._call_gemini_for_verdict = original_gemini
    print("PASS: falls back to Gemini when Claude fails, and clearly marks the rationale as Gemini-generated.")


def test_uses_claude_normally_when_it_succeeds():
    original_claude = ac._call_claude_for_verdict
    original_gemini = ac._call_gemini_for_verdict
    try:
        def fake_claude_succeeds(prompt):
            return "WATCH\n! Thin margin at 12% ROI\n+ Consistent demand"

        def fake_gemini_should_not_be_called(prompt):
            raise AssertionError("Gemini should never be called when Claude succeeds")

        ac._call_claude_for_verdict = fake_claude_succeeds
        ac._call_gemini_for_verdict = fake_gemini_should_not_be_called

        verdict, rationale = ac.generate_verdict(SAMPLE_METRICS)
        assert verdict == "WATCH"
        assert "Gemini fallback" not in rationale
    finally:
        ac._call_claude_for_verdict = original_claude
        ac._call_gemini_for_verdict = original_gemini
    print("PASS: Gemini is never called when Claude succeeds normally.")


def test_raises_when_both_providers_fail():
    original_claude = ac._call_claude_for_verdict
    original_gemini = ac._call_gemini_for_verdict
    try:
        def fake_claude_fails(prompt):
            raise RuntimeError("Claude down")

        def fake_gemini_fails(prompt):
            raise RuntimeError("Gemini also down")

        ac._call_claude_for_verdict = fake_claude_fails
        ac._call_gemini_for_verdict = fake_gemini_fails

        try:
            ac.generate_verdict(SAMPLE_METRICS)
            raise AssertionError("Expected an exception when both providers fail")
        except RuntimeError as exc:
            assert "Gemini also down" in str(exc)
    finally:
        ac._call_claude_for_verdict = original_claude
        ac._call_gemini_for_verdict = original_gemini
    print("PASS: a genuine double failure still raises (same as before -- LeadAnalysisService's existing retry/give-up logic handles this unchanged).")


def test_parse_verdict_response_handles_synonyms_and_bullets():
    verdict, rationale = ac._parse_verdict_response("PASS\nNo real margin here, avoid.")
    assert verdict == "AVOID"  # synonym mapping still applies regardless of which provider produced the text
    assert rationale.startswith("- ")  # falls back to plain bullets when no +/! markers present
    print("PASS: verdict parsing (synonyms, bullet fallback) works identically regardless of provider.")


if __name__ == "__main__":
    test_falls_back_to_gemini_when_claude_fails()
    test_uses_claude_normally_when_it_succeeds()
    test_raises_when_both_providers_fail()
    test_parse_verdict_response_handles_synonyms_and_bullets()
    print("\nALL TESTS PASSED.")
