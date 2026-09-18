"""
tasks/research_roblox_trend.py — the Roblox Game Development business
vertical's first task type.

Same pattern and same safety posture as tasks/research_opportunity.py
(Sec 9.C and Sec 26 "No Magic" in the project spec): this is read-only
research (permission_level 1-2). It never claims a game concept is
guaranteed to succeed, never fabricates player-count or revenue
numbers, and never recommends manipulative tactics (fake engagement,
exploiting Roblox's algorithm, buying fake players, etc.) — the system
prompt explicitly forbids the model from suggesting any of that. Every
field is framed as an estimate, and confidence_level is mandatory.

The model returns strict JSON so results can be stored/compared across
trend research runs (see the `roblox_trends` table). Invalid or
incomplete JSON is a hard failure — never a fabricated fallback.
"""

import json

from tasks.webfetch import fetch_url_text, FetchError

REQUIRED_FIELDS = [
    "player_demand_signals", "competition_level", "build_complexity",
    "target_audience", "monetization_fit", "estimated_dev_time",
    "similar_successful_games", "risk_factors", "confidence_level", "summary",
]

SYSTEM_PROMPT = """You are a Roblox game trend research analyst. You will be given a \
game genre/mechanic/concept and, optionally, text fetched from reference web pages \
about it (trend reports, competitor pages, community discussion). Produce a \
structured, evidence-based assessment of whether it's worth building.

Rules:
- You are estimating, not predicting. Never state player demand, revenue, or an \
outcome as a guaranteed fact. Use qualitative bands (e.g. "low", "moderate", "high") \
or explicit ranges, and say plainly when you lack enough evidence to estimate something.
- Base your assessment on the provided reference text where given; where you must draw \
on general knowledge instead, say so.
- Never claim a game concept is guaranteed to go viral, succeed, or "can't fail."
- Never suggest or endorse fake engagement, bot players, exploiting Roblox's \
recommendation algorithm, buying fake reviews/favorites, or any tactic that violates \
Roblox's Terms of Service. If asked to evaluate something that would require such \
tactics to succeed, say so plainly in risk_factors instead of working around it.
- confidence_level must be one of: "low", "medium", "high" — reflecting how much real \
evidence (vs. general reasoning) backs this assessment.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after) \
with exactly these string fields: player_demand_signals, competition_level, \
build_complexity, target_audience, monetization_fit, estimated_dev_time, \
similar_successful_games, risk_factors, confidence_level, summary. Each field's value \
should be 1-3 sentences, except confidence_level which must be exactly "low", \
"medium", or "high"."""


class RobloxTrendAssessmentError(Exception):
    pass


def research_roblox_trend(concept: str, client, reference_urls=None) -> dict:
    """Runs one Roblox trend/concept assessment. `client` is anything
    with a .complete(messages, system, max_tokens) method (real
    AnthropicClient or MockClient). `reference_urls` is optional and
    capped at 3, same limit/reasoning as research_opportunity.

    Returns a dict with exactly REQUIRED_FIELDS plus 'concept' and
    'reference_urls_used' (the subset that actually fetched). Raises
    RobloxTrendAssessmentError if the model's response isn't valid,
    complete JSON — a hard failure, not a soft fallback."""
    reference_urls = reference_urls or []
    if len(reference_urls) > 3:
        raise ValueError("research_roblox_trend is capped at 3 reference URLs")

    fetched_texts = []
    urls_used = []
    for url in reference_urls:
        try:
            text = fetch_url_text(url)
            if text:
                fetched_texts.append(f"--- Reference: {url} ---\n{text}")
                urls_used.append(url)
        except FetchError:
            # A reference URL failing to fetch shrinks the evidence base;
            # it does not fail the whole assessment — the model is told
            # explicitly below how much reference material it actually got.
            continue

    if fetched_texts:
        evidence_block = "\n\n".join(fetched_texts)
        user_content = (
            f"Game genre/mechanic/concept: {concept}\n\n"
            f"Reference material ({len(urls_used)} of {len(reference_urls)} "
            f"requested URLs fetched successfully):\n\n{evidence_block}"
        )
    else:
        user_content = (
            f"Game genre/mechanic/concept: {concept}\n\n"
            f"No reference material could be fetched"
            f"{' (none was requested)' if not reference_urls else ' (all requested URLs failed to fetch)'}"
            f". Base this assessment on general knowledge only, and reflect that "
            f"limitation in confidence_level."
        )

    raw_response = client.complete(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=900,
    )

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise RobloxTrendAssessmentError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    if missing:
        raise RobloxTrendAssessmentError(
            f"model's JSON is missing required fields: {missing}. Got keys: {list(parsed.keys())}"
        )
    if parsed["confidence_level"] not in ("low", "medium", "high"):
        raise RobloxTrendAssessmentError(
            f"confidence_level must be low/medium/high, got: {parsed['confidence_level']!r}"
        )

    parsed["concept"] = concept
    parsed["reference_urls_used"] = urls_used
    return parsed
