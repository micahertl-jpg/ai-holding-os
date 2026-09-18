"""
test_research_roblox_trend_offline.py — real tests for
tasks/research_roblox_trend.py, using unittest.mock to stand in for
network fetches (this sandbox has no internet) and a MockClient
returning controlled JSON strings to exercise both the success path
and every validation failure path. Mirrors
test_research_opportunity_offline.py exactly, since research_roblox_trend
follows the same structure and the same safety requirements.
"""

import json
from unittest.mock import patch

from tasks.research_roblox_trend import (
    research_roblox_trend, RobloxTrendAssessmentError, REQUIRED_FIELDS,
)
from tasks.webfetch import FetchError
from llm_client import MockClient


VALID_ASSESSMENT = {
    "player_demand_signals": "Moderate search/discussion volume based on the reference material.",
    "competition_level": "Several established experiences in this genre; no single dominant leader.",
    "build_complexity": "Moderate — needs custom scripting for the core mechanic.",
    "target_audience": "Players aged roughly 8-14 who enjoy obby/roleplay hybrids.",
    "monetization_fit": "Game passes and a cosmetic gamepass shop are a plausible fit.",
    "estimated_dev_time": "Roughly 6-10 weeks for a playable MVP with one core loop.",
    "similar_successful_games": "A few comparable experiences exist in this genre on the platform.",
    "risk_factors": "Genre is moderately saturated; differentiation will matter a lot.",
    "confidence_level": "medium",
    "summary": "A plausible, moderately competitive concept worth a small prototype spend.",
}


def _mock_client_returning(obj):
    return MockClient(canned_response=json.dumps(obj))


def test_success_with_reference_urls():
    with patch("tasks.research_roblox_trend.fetch_url_text",
               return_value="Some fetched trend-report text about the genre."):
        result = research_roblox_trend(
            "obby with a twist mechanic", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/a", "https://example.com/b"],
        )
    for field in REQUIRED_FIELDS:
        assert field in result, f"missing {field}"
    assert result["concept"] == "obby with a twist mechanic"
    assert result["reference_urls_used"] == ["https://example.com/a", "https://example.com/b"]
    assert result["confidence_level"] == "medium"
    print("PASS: success path with reference URLs returns a complete, correctly-shaped assessment")


def test_success_with_no_reference_urls():
    result = research_roblox_trend(
        "tycoon with a pet-collecting twist", _mock_client_returning(VALID_ASSESSMENT),
    )
    assert result["reference_urls_used"] == []
    print("PASS: success path with no reference URLs works (general-knowledge assessment)")


def test_fetch_failures_shrink_evidence_but_dont_fail_the_task():
    with patch("tasks.research_roblox_trend.fetch_url_text", side_effect=FetchError("dead link")):
        result = research_roblox_trend(
            "Some concept", _mock_client_returning(VALID_ASSESSMENT),
            reference_urls=["https://example.com/dead"],
        )
    assert result["reference_urls_used"] == [], (
        "a dead reference URL should be dropped, not counted as used"
    )
    print("PASS: a failed reference-URL fetch shrinks the evidence base without failing the task")


def test_over_3_urls_rejected():
    try:
        research_roblox_trend("x", MockClient(), reference_urls=["a", "b", "c", "d"])
    except ValueError as e:
        print(f"PASS: correctly rejected >3 reference URLs ({e})")
        return
    raise AssertionError("expected ValueError for >3 reference URLs")


def test_invalid_json_raises_not_fabricates():
    with patch("tasks.research_roblox_trend.fetch_url_text", return_value=""):
        try:
            research_roblox_trend("x", MockClient(canned_response="not json at all"))
        except RobloxTrendAssessmentError as e:
            assert "did not return valid JSON" in str(e)
            print("PASS: invalid JSON from the model raises, never falls back to a fabricated result")
            return
    raise AssertionError("expected RobloxTrendAssessmentError for invalid JSON")


def test_missing_field_raises():
    incomplete = dict(VALID_ASSESSMENT)
    del incomplete["risk_factors"]
    try:
        research_roblox_trend("x", _mock_client_returning(incomplete))
    except RobloxTrendAssessmentError as e:
        assert "risk_factors" in str(e)
        print("PASS: a JSON response missing a required field is rejected, not silently patched")
        return
    raise AssertionError("expected RobloxTrendAssessmentError for a missing field")


def test_bad_confidence_level_raises():
    bad = dict(VALID_ASSESSMENT)
    bad["confidence_level"] = "definitely"
    try:
        research_roblox_trend("x", _mock_client_returning(bad))
    except RobloxTrendAssessmentError as e:
        assert "confidence_level" in str(e)
        print("PASS: an invalid confidence_level value is rejected")
        return
    raise AssertionError("expected RobloxTrendAssessmentError for bad confidence_level")


if __name__ == "__main__":
    test_success_with_reference_urls()
    test_success_with_no_reference_urls()
    test_fetch_failures_shrink_evidence_but_dont_fail_the_task()
    test_over_3_urls_rejected()
    test_invalid_json_raises_not_fabricates()
    test_missing_field_raises()
    test_bad_confidence_level_raises()
    print("\nAll research_roblox_trend.py offline tests passed.")
