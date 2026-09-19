"""
test_research_app_feasibility_offline.py — real tests for
tasks/research_app_feasibility.py, using unittest.mock to stand in for
network fetches (this sandbox has no internet) and a MockClient
returning controlled JSON strings to exercise both the success path
and every validation failure path. Mirrors
test_research_roblox_trend_offline.py exactly, since research_app_feasibility
follows the same structure and the same safety requirements, plus one
extra test for complexity_tier validation (unique to this vertical).
"""

import json
from unittest.mock import patch

from tasks.research_app_feasibility import (
    research_app_feasibility, AppFeasibilityAssessmentError, REQUIRED_FIELDS,
)
from tasks.webfetch import FetchError
from llm_client import MockClient


VALID_ASSESSMENT = {
    "platform_recommendation": "Cross-platform mobile via React Native to cover iOS and Android with one codebase.",
    "suggested_tech_stack": "React Native, a managed Postgres backend, and a thin FastAPI layer for business logic.",
    "complexity_tier": "moderate",
    "estimated_timeline": "8-12 weeks for an MVP",
    "estimated_cost_range": "Roughly $15k-$40k for an MVP build, a rough order-of-magnitude estimate only.",
    "mvp_feature_scope": "Account creation, core content browsing, and a single primary interaction loop.",
    "key_technical_risks": "Push notification delivery reliability and initial App Store review turnaround.",
    "similar_existing_apps": "A few comparable apps exist in this space; none dominate the category.",
    "confidence_level": "medium",
    "summary": "A moderately complex but well-precedented app concept, reasonable for a small team to scope.",
}


def _mock_client_returning(obj):
    return MockClient(canned_response=json.dumps(obj))


def test_success_with_reference_urls():
    with patch("tasks.research_app_feasibility.fetch_url_text",
               return_value="Some fetched market/competitor text about similar apps."):
        result = research_app_feasibility(
            "a habit tracker with social accountability", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/a", "https://example.com/b"],
        )
    for field in REQUIRED_FIELDS:
        assert field in result, f"missing {field}"
    assert result["concept"] == "a habit tracker with social accountability"
    assert result["reference_urls_used"] == ["https://example.com/a", "https://example.com/b"]
    assert result["confidence_level"] == "medium"
    assert result["complexity_tier"] == "moderate"
    print("PASS: success path with reference URLs returns a complete, correctly-shaped assessment")


def test_success_with_no_reference_urls():
    result = research_app_feasibility(
        "a local-first note-taking app", _mock_client_returning(VALID_ASSESSMENT),
    )
    assert result["reference_urls_used"] == []
    print("PASS: success path with no reference URLs works (general-knowledge assessment)")


def test_fetch_failures_shrink_evidence_but_dont_fail_the_task():
    with patch("tasks.research_app_feasibility.fetch_url_text", side_effect=FetchError("dead link")):
        result = research_app_feasibility(
            "Some concept", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/dead"],
        )
    assert result["reference_urls_used"] == [], (
        "a dead reference URL should be dropped, not counted as used"
    )
    print("PASS: a failed reference-URL fetch shrinks the evidence base without failing the task")


def test_over_3_urls_rejected():
    try:
        research_app_feasibility("x", MockClient(), reference_urls=["a", "b", "c", "d"])
    except ValueError as e:
        print(f"PASS: correctly rejected >3 reference URLs ({e})")
        return
    raise AssertionError("expected ValueError for >3 reference URLs")


def test_invalid_json_raises_not_fabricates():
    with patch("tasks.research_app_feasibility.fetch_url_text", return_value=""):
        try:
            research_app_feasibility("x", MockClient(canned_response="not json at all"))
        except AppFeasibilityAssessmentError as e:
            assert "did not return valid JSON" in str(e)
            print("PASS: invalid JSON from the model raises, never falls back to a fabricated result")
            return
    raise AssertionError("expected AppFeasibilityAssessmentError for invalid JSON")


def test_missing_field_raises():
    incomplete = dict(VALID_ASSESSMENT)
    del incomplete["key_technical_risks"]
    try:
        research_app_feasibility("x", _mock_client_returning(incomplete))
    except AppFeasibilityAssessmentError as e:
        assert "key_technical_risks" in str(e)
        print("PASS: a JSON response missing a required field is rejected, not silently patched")
        return
    raise AssertionError("expected AppFeasibilityAssessmentError for a missing field")


def test_bad_confidence_level_raises():
    bad = dict(VALID_ASSESSMENT)
    bad["confidence_level"] = "definitely"
    try:
        research_app_feasibility("x", _mock_client_returning(bad))
    except AppFeasibilityAssessmentError as e:
        assert "confidence_level" in str(e)
        print("PASS: an invalid confidence_level value is rejected")
        return
    raise AssertionError("expected AppFeasibilityAssessmentError for bad confidence_level")


def test_bad_complexity_tier_raises():
    bad = dict(VALID_ASSESSMENT)
    bad["complexity_tier"] = "trivial"
    try:
        research_app_feasibility("x", _mock_client_returning(bad))
    except AppFeasibilityAssessmentError as e:
        assert "complexity_tier" in str(e)
        print("PASS: an invalid complexity_tier value is rejected")
        return
    raise AssertionError("expected AppFeasibilityAssessmentError for bad complexity_tier")


if __name__ == "__main__":
    test_success_with_reference_urls()
    test_success_with_no_reference_urls()
    test_fetch_failures_shrink_evidence_but_dont_fail_the_task()
    test_over_3_urls_rejected()
    test_invalid_json_raises_not_fabricates()
    test_missing_field_raises()
    test_bad_confidence_level_raises()
    test_bad_complexity_tier_raises()
    print("\nAll research_app_feasibility.py offline tests passed.")
