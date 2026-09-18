"""
tasks/research_opportunity.py — the Opportunity Discovery business
vertical's core task type.

Per the project spec (Sec 9.B and Sec 26 "No Magic"): this produces a
structured assessment with explicit uncertainty, NOT a guarantee. It
never claims a market size or revenue figure as fact — every field is
framed as the model's estimate, and a confidence_level field is
mandatory. This is read-only research (permission_level 1-2): it fetches
public web pages and asks the model to reason about them, and touches
nothing else — no money, no contracts, no external writes.

The model is asked to return strict JSON so the fields can be stored
and compared across opportunities (see the `opportunities` table). If
the model's response isn't valid JSON, that's treated as the task
failing loudly — never as a reason to fabricate a fallback structure.
"""

import json

from tasks.webfetch import fetch_url_text, FetchError

REQUIRED_FIELDS = [
    "market_size", "competition", "startup_cost", "revenue_potential",
    "time_to_market", "operational_complexity", "legal_regulatory_risk",
    "capital_requirements", "downside_risk", "confidence_level", "summary",
]

SYSTEM_PROMPT = """You are a business opportunity research analyst. You will be given \
a topic/niche and, optionally, text fetched from reference web pages about it. Produce \
a structured, evidence-based assessment.

Rules:
- You are estimating, not predicting. Never state a market size, revenue figure, or \
outcome as a guaranteed fact. Use qualitative bands (e.g. "small", "moderate", "large") \
or explicit ranges, and say plainly when you lack enough evidence to estimate something.
- Base your assessment on the provided reference text where given; where you must draw \
on general knowledge instead, say so.
- Never claim an opportunity is guaranteed to succeed, risk-free, or "can't lose."
- confidence_level must be one of: "low", "medium", "high" — reflecting how much real \
evidence (vs. general reasoning) backs this assessment.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after) \
with exactly these string fields: market_size, competition, startup_cost, \
revenue_potential, time_to_market, operational_complexity, legal_regulatory_risk, \
capital_requirements, downside_risk, confidence_level, summary. Each field's value \
should be 1-3 sentences, except confidence_level which must be exactly "low", \
"medium", or "high"."""


class OpportunityAssessmentError(Exception):
    pass


def research_opportunity(topic: str, client, reference_urls=None) -> dict:
    """Runs one opportunity assessment. `client` is anything with a
    .complete(messages, system, max_tokens) method (real AnthropicClient
    or MockClient). `reference_urls` is optional and capped at 3, same
    limit as summarize_urls, for the same reason (keep one task's cost
    and blast radius small and predictable).

    Returns a dict with exactly REQUIRED_FIELDS plus 'topic' and
    'reference_urls_used' (the subset that actually fetched). Raises
    OpportunityAssessmentError if the model's response isn't valid,
    complete JSON — this is a hard failure, not a soft fallback,
    because a partially-invented assessment is worse than none."""
    reference_urls = reference_urls or []
    if len(reference_urls) > 3:
        raise ValueError("research_opportunity is capped at 3 reference URLs")

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
            f"Topic/niche: {topic}\n\n"
            f"Reference material ({len(urls_used)} of {len(reference_urls)} "
            f"requested URLs fetched successfully):\n\n{evidence_block}"
        )
    else:
        user_content = (
            f"Topic/niche: {topic}\n\n"
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
        raise OpportunityAssessmentError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    if missing:
        raise OpportunityAssessmentError(
            f"model's JSON is missing required fields: {missing}. Got keys: {list(parsed.keys())}"
        )
    if parsed["confidence_level"] not in ("low", "medium", "high"):
        raise OpportunityAssessmentError(
            f"confidence_level must be low/medium/high, got: {parsed['confidence_level']!r}"
        )

    parsed["topic"] = topic
    parsed["reference_urls_used"] = urls_used
    return parsed
