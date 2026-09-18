"""
test_research_opportunity_offline.py — real tests for
tasks/research_opportunity.py, using unittest.mock to stand in for
network fetches (this sandbox has no internet) and a MockClient
returning controlled JSON strings to exercise both the success path
and every validation failure path.
"""

import json
from unittest.mock import patch

from tasks.research_opportunity import (
    research_opportunity, OpportunityAssessmentError, REQUIRED_FIELDS,
)
from tasks.webfetch import FetchError
from llm_client import MockClient


VALID_ASSESSMENT = {
    "market_size": "Moderate — niche but growing based on the reference material.",
    "competition": "Several established players; no dominant single leader.",
    "startup_cost": "Low to moderate; mostly software development time.",
    "revenue_potential": "Uncertain without pricing data; plausible subscription model.",
    "time_to_market": "Roughly 2-4 months for an MVP.",
    "operational_complexity": "Low initially, likely to grow with customer support needs.",
    "legal_regulatory_risk": "Low; standard SaaS terms-of-service considerations apply.",
    "capital_requirements": "Under $10k to validate an MVP.",
    "downside_risk": "Moderate — competitive market could limit differentiation.",
    "confidence_level": "medium",
    "summary": "A plausible, moderately competitive niche worth a small validation spend.",
}


def _mock_client_returning(obj):
    return MockClient(canned_response=json.dumps(obj))


def test_success_with_reference_urls():
    with patch("tasks.research_opportunity.fetch_url_text",
               return_value="Some fetched competitor page text about the niche."):
        result = research_opportunity(
            "AI-powered recipe apps", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/a", "https://example.com/b"],
        )
    for field in REQUIRED_FIELDS:
        assert field in result, f"missing {field}"
    assert result["topic"] == "AI-powered recipe apps"
    assert result["reference_urls_used"] == ["https://example.com/a", "https://example.com/b"]
    assert result["confidence_level"] == "medium"
    print("PASS: success path with reference URLs returns a complete, correctly-shaped assessment")


def test_success_with_no_reference_urls():
    result = research_opportunity(
        "Niche subscription boxes", _mock_client_returning(VALID_ASSESSMENT),
    )
    assert result["reference_urls_used"] == []
    print("PASS: success path with no reference URLs works (general-knowledge assessment)")


def test_fetch_failures_shrink_evidence_but_dont_fail_the_task():
    with patch("tasks.research_opportunity.fetch_url_text", side_effect=FetchError("dead link")):
        result = research_opportunity(
            "Some topic", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/dead"],
        )
    assert result["reference_urls_used"] == [], (
        "a dead reference URL should be dropped, not counted as used"
    )
    print("PASS: a failed reference-URL fetch shrinks the evidence base without failing the task")


def test_over_3_urls_rejected():
    try:
        research_opportunity("x", MockClient(), reference_urls=["a", "b", "c", "d"])
    except ValueError as e:
        print(f"PASS: correctly rejected >3 reference URLs ({e})")
        return
    raise AssertionError("expected ValueError for >3 reference URLs")


def test_invalid_json_raises_not_fabricates():
    with patch("tasks.research_opportunity.fetch_url_text", return_value=""):
        try:
            research_opportunity("x", MockClient(canned_response="not json at all"))
        except OpportunityAssessmentError as e:
            assert "did not return valid JSON" in str(e)
            print("PASS: invalid JSON from the model raises, never falls back to a fabricated result")
            return
    raise AssertionError("expected OpportunityAssessmentError for invalid JSON")


def test_missing_field_raises():
    incomplete = dict(VALID_ASSESSMENT)
    del incomplete["downside_risk"]
    try:
        research_opportunity("x", _mock_client_returning(incomplete))
    except OpportunityAssessmentError as e:
        assert "downside_risk" in str(e)
        print("PASS: a JSON response missing a required field is rejected, not silently patched")
        return
    raise AssertionError("expected OpportunityAssessmentError for a missing field")


def test_bad_confidence_level_raises():
    bad = dict(VALID_ASSESSMENT)
    bad["confidence_level"] = "definitely"
    try:
        research_opportunity("x", _mock_client_returning(bad))
    except OpportunityAssessmentError as e:
        assert "confidence_level" in str(e)
        print("PASS: an invalid confidence_level value is rejected")
        return
    raise AssertionError("expected OpportunityAssessmentError for bad confidence_level")


if __name__ == "__main__":
    test_success_with_reference_urls()
    test_success_with_no_reference_urls()
    test_fetch_failures_shrink_evidence_but_dont_fail_the_task()
    test_over_3_urls_rejected()
    test_invalid_json_raises_not_fabricates()
    test_missing_field_raises()
    test_bad_confidence_level_raises()
    print("\nAll research_opportunity.py offline tests passed.")
