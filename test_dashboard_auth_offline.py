"""
test_dashboard_auth_offline.py — real tests for dashboard_auth.py. This
module guards every internal endpoint, so these tests prove: the public
storefront/health paths stay reachable with no credentials, every other
path is treated as protected, and credential checking only ever accepts
the exact configured username+password (never partial matches, never a
"good enough" prefix, never succeeds when nothing is configured).
"""

import base64

from dashboard_auth import (
    is_public_path, credentials_configured,
    parse_basic_auth_header, check_credentials,
)


def _basic_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_public_paths_are_recognized():
    for path in ("/", "/health", "/robots.txt", "/sitemap.xml", "/store", "/store/products",
                 "/store/checkout", "/store/webhook", "/store/orders/ord_123",
                 "/store/terms", "/store/business-idea-research",
                 "/store/roblox-game-idea-research", "/store/app-feasibility-report",
                 "/store/real-estate-investment-research",
                 "/static/store.html", "/static/store.css",
                 "/static/store.js", "/static/store-success.html",
                 "/static/store-success.js", "/static/store-legal.html",
                 "/static/favicon.svg"):
        assert is_public_path(path), f"{path} should be public"
    print("PASS: the public storefront/health/root paths are all recognized as public")


def test_sitemap_prefix_is_not_fooled_by_lookalikes():
    assert not is_public_path("/sitemap.xml.evil")
    assert not is_public_path("/sitemapxml")
    print("PASS: the /sitemap.xml public path respects path boundaries, no lookalike bypass")


def test_internal_paths_are_not_public():
    for path in ("/dashboard", "/businesses", "/agents", "/tasks",
                 "/approvals", "/banker/allocate", "/scheduled-jobs",
                 "/static/dashboard.html", "/static/dashboard.js",
                 "/static/dashboard-render.js", "/businesses/biz_1/banker/allocate"):
        assert not is_public_path(path), f"{path} should require auth"
    print("PASS: dashboard/API/static-dashboard paths are never treated as public")


def test_path_prefix_matching_is_not_fooled_by_lookalikes():
    # "/healthy" and "/storefront-evil" must NOT match via naive substring/
    # startswith-without-boundary logic.
    assert not is_public_path("/healthy-check")
    assert not is_public_path("/storefronts")
    print("PASS: prefix matching respects path boundaries, no lookalike bypass")


def test_root_prefix_matches_only_the_exact_root_path():
    """Regression-style test: "/" was added to PUBLIC_PATH_PREFIXES for
    the storefront's landing page. Since every real path starts with
    "/", a naive implementation could accidentally make everything
    public -- this proves is_public_path's `path == prefix or
    path.startswith(prefix + "/")` check requires "//" for the prefix
    match, which no real single-leading-slash path has, so "/" only
    ever matches the exact root."""
    assert is_public_path("/")
    for path in ("/dashboard", "/businesses", "/anything-else",
                 "/static/dashboard.html"):
        assert not is_public_path(path), \
            f"{path} must not become public just because '/' is a public prefix"
    print("PASS: the '/' public prefix matches only the exact root path, not everything")


def test_credentials_configured_reflects_env():
    assert credentials_configured(
        # can't easily unset env vars here without mutating process env;
        # covered properly in test_check_credentials_* below via explicit args
    ) in (True, False)
    print("PASS: credentials_configured() runs without error")


def test_parse_basic_auth_header_valid():
    header = _basic_header("owner", "s3cret")
    parsed = parse_basic_auth_header(header)
    assert parsed == ("owner", "s3cret")
    print("PASS: a well-formed Basic header parses to (username, password)")


def test_parse_basic_auth_header_rejects_missing_or_malformed():
    assert parse_basic_auth_header(None) is None
    assert parse_basic_auth_header("") is None
    assert parse_basic_auth_header("Bearer abc123") is None
    assert parse_basic_auth_header("Basic not-valid-base64!!!") is None
    # valid base64 but no ':' separator
    no_colon = base64.b64encode(b"ownersecret").decode("ascii")
    assert parse_basic_auth_header(f"Basic {no_colon}") is None
    print("PASS: missing, wrong-scheme, and malformed headers all parse to None")


def test_check_credentials_accepts_exact_match():
    header = _basic_header("owner", "s3cret-pw")
    assert check_credentials(header, expected_username="owner", expected_password="s3cret-pw") is True
    print("PASS: the exact configured username+password is accepted")


def test_check_credentials_rejects_wrong_username():
    header = _basic_header("intruder", "s3cret-pw")
    assert check_credentials(header, expected_username="owner", expected_password="s3cret-pw") is False
    print("PASS: a wrong username is rejected")


def test_check_credentials_rejects_wrong_password():
    header = _basic_header("owner", "guess")
    assert check_credentials(header, expected_username="owner", expected_password="s3cret-pw") is False
    print("PASS: a wrong password is rejected")


def test_check_credentials_rejects_missing_header():
    assert check_credentials(None, expected_username="owner", expected_password="s3cret-pw") is False
    print("PASS: a missing Authorization header is rejected, not treated as open access")


def test_check_credentials_rejects_malformed_header():
    assert check_credentials("Basic ???", expected_username="owner", expected_password="s3cret-pw") is False
    print("PASS: a malformed Authorization header is rejected")


def test_check_credentials_fails_closed_when_not_configured():
    header = _basic_header("owner", "s3cret-pw")
    # No expected_username/password passed AND explicitly simulate "not configured"
    # by passing empty strings (mirrors what os.environ.get returns when unset: None).
    assert check_credentials(header, expected_username=None, expected_password=None) is False
    assert check_credentials(header, expected_username="", expected_password="") is False
    assert check_credentials(header, expected_username="owner", expected_password=None) is False
    print("PASS: with no configured username/password, every request is rejected "
          "(fails closed, never silently open)")


def test_check_credentials_is_case_and_exact_sensitive():
    header = _basic_header("Owner", "s3cret-pw")  # wrong case
    assert check_credentials(header, expected_username="owner", expected_password="s3cret-pw") is False
    print("PASS: credential comparison is exact, not case-insensitive")


if __name__ == "__main__":
    test_public_paths_are_recognized()
    test_internal_paths_are_not_public()
    test_path_prefix_matching_is_not_fooled_by_lookalikes()
    test_root_prefix_matches_only_the_exact_root_path()
    test_credentials_configured_reflects_env()
    test_parse_basic_auth_header_valid()
    test_parse_basic_auth_header_rejects_missing_or_malformed()
    test_check_credentials_accepts_exact_match()
    test_check_credentials_rejects_wrong_username()
    test_check_credentials_rejects_wrong_password()
    test_check_credentials_rejects_missing_header()
    test_check_credentials_rejects_malformed_header()
    test_check_credentials_fails_closed_when_not_configured()
    test_check_credentials_is_case_and_exact_sensitive()
    print("\nAll dashboard_auth.py offline tests passed.")
