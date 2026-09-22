"""
test_launch_business_offline.py — calls api.py's real launch_opportunity/
launch_roblox_trend/launch_app_feasibility_assessment/
launch_real_estate_assessment functions directly (FastAPI's @app.post
decorator leaves the underlying function callable, same technique
test_delete_abandoned_order_offline.py and
test_api_request_models_offline.py use) rather than mirroring their
logic by hand, since all four are thin wrappers around the shared
_launch_business_from_research() and a bug in that shared function
would otherwise need to be caught four separate times.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import os

from fastapi import HTTPException

import api
from db import Database
from registry import BusinessRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_launch_business.db")

# (endpoint function, table, id column value used below, title field,
# a real column set to insert, the record's own title, kind_label used
# in refusal messages)
VERTICALS = [
    {
        "fn": api.launch_opportunity,
        "table": "opportunities",
        "insert_cols": "(id, business_id, topic, summary)",
        "insert_vals": ("opp_1", None, "AI-powered pet grooming subscription boxes",
                         "Worth a small validation effort."),
        "title": "AI-powered pet grooming subscription boxes",
        "kind_label": "business opportunity",
    },
    {
        "fn": api.launch_roblox_trend,
        "table": "roblox_trends",
        "insert_cols": "(id, business_id, concept, summary)",
        "insert_vals": ("trend_1", None, "A cozy farming sim", "Worth prototyping."),
        "title": "A cozy farming sim",
        "kind_label": "Roblox concept",
    },
    {
        "fn": api.launch_app_feasibility_assessment,
        "table": "app_feasibility_assessments",
        "insert_cols": "(id, business_id, concept, summary)",
        "insert_vals": ("app_1", None, "A habit tracker app", "Feasible as an MVP."),
        "title": "A habit tracker app",
        "kind_label": "app idea",
    },
    {
        "fn": api.launch_real_estate_assessment,
        "table": "real_estate_assessments",
        "insert_cols": "(id, business_id, property_or_market, summary)",
        "insert_vals": ("re_1", None, "123 Main St", "Solid rental yield."),
        "title": "123 Main St",
        "kind_label": "real estate opportunity",
    },
]


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    api.state["businesses"] = BusinessRegistry(db)
    biz_id = api.state["businesses"].create("Research Co", "research", "test", 0.0)
    return db, biz_id


def test_every_vertical_launches_a_real_business_seeded_from_its_own_research():
    for v in VERTICALS:
        db, biz_id = _setup()
        vals = (v["insert_vals"][0], biz_id) + v["insert_vals"][2:]
        db.execute(f"INSERT INTO {v['table']} {v['insert_cols']} VALUES (?,?,?,?)", vals)

        result = v["fn"](biz_id, v["insert_vals"][0], api.LaunchBusinessRequest())
        new_biz = api.state["businesses"].get(result["business_id"])
        assert new_biz["name"] == v["title"]
        assert new_biz["type"] == "venture"
        assert v["title"] in new_biz["objective"]
        assert new_biz["budget_usd"] == 0.0

        record = db.query_one(f"SELECT launched_business_id FROM {v['table']} WHERE id=?",
                               (v["insert_vals"][0],))
        assert record["launched_business_id"] == result["business_id"]
        print(f"PASS: {v['fn'].__name__} launches a real business seeded from its own research")
        db.close()
        os.remove(TEST_DB_PATH)


def test_every_vertical_accepts_an_owner_supplied_name_override():
    for v in VERTICALS:
        db, biz_id = _setup()
        vals = (v["insert_vals"][0], biz_id) + v["insert_vals"][2:]
        db.execute(f"INSERT INTO {v['table']} {v['insert_cols']} VALUES (?,?,?,?)", vals)

        result = v["fn"](biz_id, v["insert_vals"][0], api.LaunchBusinessRequest(name="A Custom Name"))
        new_biz = api.state["businesses"].get(result["business_id"])
        assert new_biz["name"] == "A Custom Name"
        print(f"PASS: {v['fn'].__name__} respects an owner-supplied name override")
        db.close()
        os.remove(TEST_DB_PATH)


def test_every_vertical_refuses_to_launch_the_same_record_twice():
    for v in VERTICALS:
        db, biz_id = _setup()
        vals = (v["insert_vals"][0], biz_id) + v["insert_vals"][2:]
        db.execute(f"INSERT INTO {v['table']} {v['insert_cols']} VALUES (?,?,?,?)", vals)

        first = v["fn"](biz_id, v["insert_vals"][0], api.LaunchBusinessRequest())
        try:
            v["fn"](biz_id, v["insert_vals"][0], api.LaunchBusinessRequest())
            assert False, "expected HTTPException on the second launch attempt"
        except HTTPException as e:
            assert e.status_code == 400
            assert first["business_id"] in e.detail
        print(f"PASS: {v['fn'].__name__} refuses to launch the same record a second time")
        db.close()
        os.remove(TEST_DB_PATH)


def test_every_vertical_404s_on_an_unknown_record_or_business():
    for v in VERTICALS:
        db, biz_id = _setup()
        try:
            v["fn"](biz_id, "does_not_exist", api.LaunchBusinessRequest())
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 404

        try:
            v["fn"]("biz_does_not_exist", "whatever", api.LaunchBusinessRequest())
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 404
        print(f"PASS: {v['fn'].__name__} 404s on an unknown record or business")
        db.close()
        os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_every_vertical_launches_a_real_business_seeded_from_its_own_research()
    test_every_vertical_accepts_an_owner_supplied_name_override()
    test_every_vertical_refuses_to_launch_the_same_record_twice()
    test_every_vertical_404s_on_an_unknown_record_or_business()
    print("\nAll launch-business offline tests passed.")
