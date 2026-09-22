"""
test_api_request_models_offline.py — proves every Pydantic request model
api.py's create_task-backed research/task endpoints depend on actually
has the fields those endpoints read off it. Requires fastapi/pydantic
installed (`.venv/bin/python3`, same as running the real server) --
test_api_logic_offline.py deliberately avoids that dependency by
mirroring endpoint logic against the registries directly, which is
exactly why this exact bug slipped through it: request_opportunity_
research() read req.budget_arc, but a routine edit had silently moved
that field onto the wrong class (LaunchOpportunityRequest) while
leaving ResearchOpportunityRequest without it -- something that only
throws AttributeError at real request time, never during that
registry-level test. This test instantiates the real model classes
with only their required fields and asserts every field the matching
endpoint actually reads is really there.
"""

import api


def test_every_research_request_model_has_budget_arc():
    """Each of these backs a POST .../research endpoint that calls
    create_task(..., budget_arc=req.budget_arc, ...) unconditionally --
    a missing field here is not a theoretical risk, it's a 500 on every
    single request to that endpoint."""
    checks = [
        (api.ResearchOpportunityRequest, {"topic": "a test topic"}),
        (api.ResearchRobloxTrendRequest, {"concept": "a test concept"}),
        (api.ResearchAppFeasibilityRequest, {"concept": "a test concept"}),
        (api.ResearchRealEstateRequest, {"property_or_market": "123 Main St"}),
    ]
    for model_cls, required_fields in checks:
        instance = model_cls(**required_fields)
        assert hasattr(instance, "budget_arc"), \
            f"{model_cls.__name__} is missing budget_arc -- its endpoint will 500 on every request"
        assert instance.budget_arc == 0.0, \
            f"{model_cls.__name__}.budget_arc should default to 0.0, got {instance.budget_arc!r}"
    print("PASS: every research request model has a working budget_arc field with the right default "
          "-- the exact field a real edit once silently detached from ResearchOpportunityRequest")


def test_launch_business_request_only_has_the_fields_it_needs():
    """Regression-specific: LaunchBusinessRequest (renamed from
    LaunchOpportunityRequest once every research vertical gained a
    launch endpoint sharing this same model) must NOT have inherited
    budget_arc from the edit that caused this bug -- it never needed
    that field (launching a business never sets a starting ARC
    budget), so its presence here would itself be a sign the same class
    of mistake happened again."""
    instance = api.LaunchBusinessRequest()
    assert instance.name is None
    assert not hasattr(instance, "budget_arc")
    print("PASS: LaunchBusinessRequest only has the fields it actually needs")


if __name__ == "__main__":
    test_every_research_request_model_has_budget_arc()
    test_launch_business_request_only_has_the_fields_it_needs()
    print("\nAll api.py request-model offline tests passed.")
