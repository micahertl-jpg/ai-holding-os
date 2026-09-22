"""
test_static_cache_headers_offline.py — real tests for api.py's
CachedStaticFiles, calling its real get_response() directly against
the real static/ directory (same technique test_chat_endpoint_offline.py
uses for calling api.py functions directly) rather than mirroring its
logic by hand.

Starlette's plain StaticFiles sets no Cache-Control at all -- found for
real when a storefront redesign didn't visually show up for a
returning visitor until they hard-refreshed, since the browser's own
(much longer, inconsistent) heuristic caching was the only thing in
control. CachedStaticFiles adds a short, explicit max-age so a future
static-asset change reaches an already-visited browser sooner.

Requires fastapi/starlette installed (`.venv/bin/python3`).
"""

import asyncio

from api import CachedStaticFiles, STATIC_DIR, STATIC_CACHE_MAX_AGE_SECONDS
# Starlette's own StaticFiles raises starlette.exceptions.HTTPException
# directly (not fastapi.HTTPException, a subclass a plain except clause
# here wouldn't catch an instance of the parent class with).
from starlette.exceptions import HTTPException


def _scope(method="GET"):
    return {"type": "http", "method": method, "headers": []}


def test_a_real_static_file_gets_the_short_cache_control_header():
    static_files = CachedStaticFiles(directory=str(STATIC_DIR))
    response = asyncio.run(static_files.get_response("store.css", _scope()))
    assert response.status_code == 200
    assert response.headers["cache-control"] == f"public, max-age={STATIC_CACHE_MAX_AGE_SECONDS}"
    print(f"PASS: a real static file (store.css) gets Cache-Control: public, "
          f"max-age={STATIC_CACHE_MAX_AGE_SECONDS}")


def test_a_missing_static_file_still_404s_normally():
    static_files = CachedStaticFiles(directory=str(STATIC_DIR))
    try:
        asyncio.run(static_files.get_response("this-file-does-not-exist.css", _scope()))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
    print("PASS: a missing static file still 404s normally -- the cache-header override "
          "doesn't interfere with StaticFiles' own not-found handling")


if __name__ == "__main__":
    test_a_real_static_file_gets_the_short_cache_control_header()
    test_a_missing_static_file_still_404s_normally()
    print("\nAll static-cache-header offline tests passed.")
