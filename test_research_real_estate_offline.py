"""
test_research_real_estate_offline.py — real tests for
tasks/research_real_estate.py, using unittest.mock to stand in for
network fetches (this sandbox has no internet) and a MockClient
returning controlled JSON strings to exercise both the success path
and every validation failure path. Mirrors
test_research_app_feasibility_offline.py's structure exactly, since
research_real_estate follows the same pattern and the same safety
requirements (minus complexity_tier, which this vertical doesn't have).
"""

import json
from unittest.mock import patch

from tasks.research_real_estate import (
    research_real_estate, RealEstateAssessmentError, REQUIRED_FIELDS,
)
from tasks.webfetch import FetchError
from llm_client import MockClient


VALID_ASSESSMENT = {
    "market_trend": "Prices in this area have risen modestly over the past two years, tracking regional trends.",
    "comparable_properties": "A few similar properties nearby sold in the last 6 months at comparable price points.",
    "estimated_rental_yield": "Roughly 4-6% gross, before expenses -- a rough estimate only.",
    "price_trend_assessment": "Gradual appreciation, consistent with broader market conditions.",
    "risk_factors": "Local zoning rules and HOA restrictions should be confirmed with a licensed agent in this jurisdiction.",
    "confidence_level": "medium",
    "summary": "A reasonably stable market with modest upside; worth a closer look with a local professional.",
}


def _mock_client_returning(obj):
    return MockClient(canned_response=json.dumps(obj))


def test_success_with_reference_urls():
    with patch("tasks.research_real_estate.fetch_url_text",
               return_value="Some fetched listing/market text."):
        result = research_real_estate(
            "123 Main St, Springfield", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/a", "https://example.com/b"],
        )
    for field in REQUIRED_FIELDS:
        assert field in result, f"missing {field}"
    assert result["property_or_market"] == "123 Main St, Springfield"
    assert result["reference_urls_used"] == ["https://example.com/a", "https://example.com/b"]
    assert result["confidence_level"] == "medium"
    print("PASS: success path with reference URLs returns a complete, correctly-shaped assessment")


def test_success_with_no_reference_urls():
    result = research_real_estate(
        "Downtown Springfield rental market", _mock_client_returning(VALID_ASSESSMENT),
    )
    assert result["reference_urls_used"] == []
    print("PASS: success path with no reference URLs works (general-knowledge assessment)")


def test_fetch_failures_shrink_evidence_but_dont_fail_the_task():
    with patch("tasks.research_real_estate.fetch_url_text", side_effect=FetchError("dead link")):
        result = research_real_estate(
            "Some property", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/dead"],
        )
    assert result["reference_urls_used"] == [], (
        "a dead reference URL should be dropped, not counted as used"
    )
    print("PASS: a failed reference-URL fetch shrinks the evidence base without failing the task")


def test_over_3_urls_rejected():
    try:
        research_real_estate("x", MockClient(), reference_urls=["a", "b", "c", "d"])
    except ValueError as e:
        print(f"PASS: correctly rejected >3 reference URLs ({e})")
        return
    raise AssertionError("expected ValueError for >3 reference URLs")


def test_invalid_json_raises_not_fabricates():
    with patch("tasks.research_real_estate.fetch_url_text", return_value=""):
        try:
            research_real_estate("x", MockClient(canned_response="not json at all"))
        except RealEstateAssessmentError as e:
            assert "did not return valid JSON" in str(e)
            print("PASS: invalid JSON from the model raises, never falls back to a fabricated result")
            return
    raise AssertionError("expected RealEstateAssessmentError for invalid JSON")


def test_missing_field_raises():
    incomplete = dict(VALID_ASSESSMENT)
    del incomplete["risk_factors"]
    try:
        research_real_estate("x", _mock_client_returning(incomplete))
    except RealEstateAssessmentError as e:
        assert "risk_factors" in str(e)
        print("PASS: a JSON response missing a required field is rejected, not silently patched")
        return
    raise AssertionError("expected RealEstateAssessmentError for a missing field")


def test_bad_confidence_level_raises():
    bad = dict(VALID_ASSESSMENT)
    bad["confidence_level"] = "definitely"
    try:
        research_real_estate("x", _mock_client_returning(bad))
    except RealEstateAssessmentError as e:
        assert "confidence_level" in str(e)
        print("PASS: an invalid confidence_level value is rejected")
        return
    raise AssertionError("expected RealEstateAssessmentError for bad confidence_level")


if __name__ == "__main__":
    test_success_with_reference_urls()
    test_success_with_no_reference_urls()
    test_fetch_failures_shrink_evidence_but_dont_fail_the_task()
    test_over_3_urls_rejected()
    test_invalid_json_raises_not_fabricates()
    test_missing_field_raises()
    test_bad_confidence_level_raises()
    print("\nAll research_real_estate.py offline tests passed.")
