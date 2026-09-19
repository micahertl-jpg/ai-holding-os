"""
tasks/research_app_feasibility.py — the App Development business
vertical's first task type.

Same pattern and same safety posture as tasks/research_opportunity.py
and tasks/research_roblox_trend.py (Sec 9.E and Sec 26 "No Magic" in the
project spec): this is read-only research/planning (permission_level
1-2). It never promises a specific delivery date or dollar figure as
fact, never claims to have scoped a legally/technically complete plan,
and explicitly flags when a proposed app would touch a regulated domain
(payments, health data, minors, etc.) as a RISK to get specialist review
on — never as something this assessment itself resolves. Every field is
framed as a rough estimate, and confidence_level is mandatory.

The model returns strict JSON so results can be stored/compared across
runs (see the `app_feasibility_assessments` table). Invalid or
incomplete JSON is a hard failure — never a fabricated fallback.
"""

import json

from tasks.webfetch import fetch_url_text, FetchError

REQUIRED_FIELDS = [
    "platform_recommendation", "suggested_tech_stack", "complexity_tier",
    "estimated_timeline", "estimated_cost_range", "mvp_feature_scope",
    "key_technical_risks", "similar_existing_apps", "confidence_level", "summary",
]

SYSTEM_PROMPT = """You are a software feasibility and technical planning analyst. You will be \
given an app idea/concept and, optionally, text fetched from reference web pages about it \
(similar products, market context). Produce a structured, evidence-based feasibility \
assessment for someone deciding whether and how to build it.

Rules:
- You are estimating, not promising. Never state a delivery timeline, cost, or outcome as a \
guaranteed fact. Use qualitative bands and explicit ranges, and say plainly when you lack \
enough evidence to estimate something.
- Base your assessment on the provided reference text where given; where you must draw on \
general knowledge instead, say so.
- If the concept would touch a regulated or sensitive domain (payments/financial data, health \
data, data from minors, biometric data, government IDs, etc.), you MUST name that explicitly in \
key_technical_risks and say it needs dedicated legal/compliance and security review before \
building — never attempt to resolve or advise on the legal/regulatory requirement yourself, \
that is out of scope for this assessment.
- complexity_tier must be one of: "simple", "moderate", "complex", "very_complex".
- confidence_level must be one of: "low", "medium", "high" — reflecting how much real evidence \
(vs. general reasoning) backs this assessment.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after) with \
exactly these fields: platform_recommendation (string), suggested_tech_stack (string), \
complexity_tier ("simple"|"moderate"|"complex"|"very_complex"), estimated_timeline (string, a \
qualitative range e.g. "6-10 weeks for an MVP"), estimated_cost_range (string, a rough order-of-\
magnitude range, explicitly labeled as a rough estimate), mvp_feature_scope (string), \
key_technical_risks (string), similar_existing_apps (string), confidence_level \
("low"|"medium"|"high"), summary (string). Each free-text field should be 1-4 sentences."""

VALID_COMPLEXITY_TIERS = {"simple", "moderate", "complex", "very_complex"}


class AppFeasibilityAssessmentError(Exception):
    pass


def research_app_feasibility(concept: str, client, reference_urls=None) -> dict:
    """Runs one app feasibility assessment. `client` is anything with a
    .complete(messages, system, max_tokens) method (real AnthropicClient
    or MockClient). `reference_urls` is optional and capped at 3, same
    limit/reasoning as research_opportunity/research_roblox_trend.

    Returns a dict with exactly REQUIRED_FIELDS plus 'concept' and
    'reference_urls_used' (the subset that actually fetched). Raises
    AppFeasibilityAssessmentError if the model's response isn't valid,
    complete JSON, or if complexity_tier/confidence_level are out of
    range — a hard failure, not a soft fallback."""
    reference_urls = reference_urls or []
    if len(reference_urls) > 3:
        raise ValueError("research_app_feasibility is capped at 3 reference URLs")

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
            f"App idea/concept: {concept}\n\n"
            f"Reference material ({len(urls_used)} of {len(reference_urls)} "
            f"requested URLs fetched successfully):\n\n{evidence_block}"
        )
    else:
        user_content = (
            f"App idea/concept: {concept}\n\n"
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
        raise AppFeasibilityAssessmentError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    if missing:
        raise AppFeasibilityAssessmentError(
            f"model's JSON is missing required fields: {missing}. Got keys: {list(parsed.keys())}"
        )
    if parsed["confidence_level"] not in ("low", "medium", "high"):
        raise AppFeasibilityAssessmentError(
            f"confidence_level must be low/medium/high, got: {parsed['confidence_level']!r}"
        )
    if parsed["complexity_tier"] not in VALID_COMPLEXITY_TIERS:
        raise AppFeasibilityAssessmentError(
            f"complexity_tier must be one of {sorted(VALID_COMPLEXITY_TIERS)}, "
            f"got: {parsed['complexity_tier']!r}"
        )

    parsed["concept"] = concept
    parsed["reference_urls_used"] = urls_used
    return parsed
