"""
tasks/research_real_estate.py — the Real Estate business vertical's
first task type, and the fifth (of six) verticals named in the project
spec.

Scoped deliberately narrow after a dedicated conversation with the
owner: this is investment RESEARCH only, same pattern and same safety
posture as tasks/research_opportunity.py, tasks/research_roblox_trend.py,
and tasks/research_app_feasibility.py (permission_level 1-2). It never
performs, brokers, or facilitates an actual transaction — no listing,
no contract, no offer, nothing resembling acting as a real estate agent
or broker. It never claims to be a licensed appraisal. Every field is
framed as a rough estimate, and confidence_level is mandatory.

Real estate is uniquely jurisdiction-sensitive (property law, zoning,
disclosure requirements, rent control, and broker/appraiser licensing
all vary by state/country) and a report here could plausibly influence
a much larger financial decision than any other vertical's $19 report —
so the model is explicitly instructed to flag (never resolve) anything
jurisdiction-specific as needing a licensed real estate agent,
appraiser, or attorney, mirroring how research_app_feasibility.py
flags a regulated domain for legal/compliance review rather than
attempting to resolve it itself.

The model returns strict JSON so results can be stored/compared across
runs (see the `real_estate_assessments` table). Invalid or incomplete
JSON is a hard failure — never a fabricated fallback.
"""

import json

from tasks.webfetch import fetch_url_text, FetchError

REQUIRED_FIELDS = [
    "market_trend", "comparable_properties", "estimated_rental_yield",
    "price_trend_assessment", "risk_factors", "confidence_level", "summary",
]

SYSTEM_PROMPT = """You are a real estate investment research analyst. You will be given a \
property address, neighborhood, or market and, optionally, text fetched from reference web \
pages about it (listings, market reports, comparable sales). Produce a structured, \
evidence-based investment research assessment for someone deciding whether to look into it \
further.

Rules:
- You are NOT a licensed appraiser and this is NOT an appraisal, and you must say so plainly if \
asked to state a specific dollar valuation — give qualitative bands and ranges instead, framed \
explicitly as rough estimates, never a precise or guaranteed figure.
- You are NOT a real estate agent or broker, and nothing you produce facilitates, lists, offers, \
or negotiates an actual transaction — this is research only.
- Base your assessment on the provided reference text where given; where you must draw on \
general knowledge instead, say so and reflect that limitation in confidence_level.
- Anything jurisdiction-specific -- zoning restrictions, disclosure requirements, rent control, \
HOA rules, title issues, permit/licensing requirements -- you MUST name explicitly in \
risk_factors and say it needs a licensed real estate agent, appraiser, or attorney in that \
specific jurisdiction before any decision is made -- never attempt to resolve or give a \
jurisdiction-specific legal opinion yourself, that is out of scope for this assessment.
- estimated_rental_yield must be a qualitative range (e.g. "roughly 4-6% gross, before expenses"), \
explicitly labeled as a rough estimate, never a guaranteed return.
- confidence_level must be one of: "low", "medium", "high" — reflecting how much real evidence \
(vs. general reasoning) backs this assessment.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after) with \
exactly these fields: market_trend (string), comparable_properties (string), \
estimated_rental_yield (string, an explicitly-labeled rough range), price_trend_assessment \
(string), risk_factors (string, must include any jurisdiction-specific flags per the rule above), \
confidence_level ("low"|"medium"|"high"), summary (string). Each free-text field should be 1-4 \
sentences."""


class RealEstateAssessmentError(Exception):
    pass


def research_real_estate(property_or_market: str, client, reference_urls=None) -> dict:
    """Runs one real estate investment research assessment. `client` is
    anything with a .complete(messages, system, max_tokens) method (real
    AnthropicClient or MockClient). `reference_urls` is optional and
    capped at 3, same limit/reasoning as the other research verticals.

    Returns a dict with exactly REQUIRED_FIELDS plus 'property_or_market'
    and 'reference_urls_used' (the subset that actually fetched). Raises
    RealEstateAssessmentError if the model's response isn't valid,
    complete JSON, or if confidence_level is out of range — a hard
    failure, not a soft fallback."""
    reference_urls = reference_urls or []
    if len(reference_urls) > 3:
        raise ValueError("research_real_estate is capped at 3 reference URLs")

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
            f"Property/market to research: {property_or_market}\n\n"
            f"Reference material ({len(urls_used)} of {len(reference_urls)} "
            f"requested URLs fetched successfully):\n\n{evidence_block}"
        )
    else:
        user_content = (
            f"Property/market to research: {property_or_market}\n\n"
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
        raise RealEstateAssessmentError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    if missing:
        raise RealEstateAssessmentError(
            f"model's JSON is missing required fields: {missing}. Got keys: {list(parsed.keys())}"
        )
    if parsed["confidence_level"] not in ("low", "medium", "high"):
        raise RealEstateAssessmentError(
            f"confidence_level must be low/medium/high, got: {parsed['confidence_level']!r}"
        )

    parsed["property_or_market"] = property_or_market
    parsed["reference_urls_used"] = urls_used
    return parsed
