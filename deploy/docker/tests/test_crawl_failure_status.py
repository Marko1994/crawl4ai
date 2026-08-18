"""A crawl whose targets all fail must say why.

When every URL fails, /crawl raised HTTPException(500). 500 is special-cased by
`_http_exception_handler` (server.py) - it is the raw-str(e) leak vector, so it
is genericised to {"error": "Internal server error", "correlation_id": ...} and
the reason is written only to the server log. The caller is therefore told
nothing, and "the target site is dead" is indistinguishable from "crawl4ai
broke". Diagnosing a failed batch meant grepping container logs by correlation
id.

The handler already documents the intended shape: "Deliberate operational
statuses (502/503/504 ...) pass through, as do 4xx". A crawl whose upstream
targets failed is exactly such an operational status, so use 502 - the caller
gets the reason, and clients that branch on `status >= 500` retry exactly as
they did with the 500.

The reason still needs sanitising: error_message can carry a Playwright
traceback complete with interpreter paths and source context, which is the leak
500 was being genericised to avoid.
"""

import pytest


@pytest.fixture
def crawl_returning(server_module, monkeypatch):
    """Force handle_crawl_request to return a chosen set of results."""
    def _install(results):
        async def fake(**kwargs):
            return {"success": True, "results": results}
        monkeypatch.setattr(server_module, "handle_crawl_request", fake)
    return _install


def _bearer():
    from auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': 'u@x.com'})}"}


def _post(client, urls=("https://example.com",)):
    return client.post("/crawl", json={"urls": list(urls)}, headers=_bearer())


ANTI_BOT = "Blocked by anti-bot protection: Structural: minimal_text (53 bytes, 14 chars visible)"

TRACEBACK = (
    "Unexpected error in _crawl_web at line 778 in _crawl_web "
    "(../usr/local/lib/python3.12/site-packages/crawl4ai/async_crawler_strategy.py):"
    "Error: Failed on navigating ACS-GOTO:Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED "
    "Code context: 773 tag=\"GOTO\", 774 params={\"url\": url}, 775 )"
)


def test_all_urls_failing_is_not_a_generic_500(stock_client, crawl_returning):
    crawl_returning([{"url": "https://dead.example", "success": False,
                      "error_message": ANTI_BOT}])

    r = _post(stock_client)

    assert r.status_code != 500, "500 is genericised; the reason never reaches the caller"
    assert "Internal server error" not in r.text


def test_all_urls_failing_returns_502_with_the_reason(stock_client, crawl_returning):
    crawl_returning([{"url": "https://dead.example", "success": False,
                      "error_message": ANTI_BOT}])

    r = _post(stock_client)

    assert r.status_code == 502, r.status_code
    assert "anti-bot" in r.text.lower()


def test_the_reason_does_not_leak_internal_paths_or_source(stock_client, crawl_returning):
    """error_message can carry a full traceback; only the cause may go out."""
    crawl_returning([{"url": "https://dead.example", "success": False,
                      "error_message": TRACEBACK}])

    r = _post(stock_client)

    assert r.status_code == 502
    body = r.text
    assert "ERR_TUNNEL_CONNECTION_FAILED" in body, "the useful cause must survive"
    assert "site-packages" not in body
    assert "Code context" not in body
    assert "async_crawler_strategy.py" not in body


def test_the_reason_is_length_bounded(stock_client, crawl_returning):
    crawl_returning([{"url": "https://dead.example", "success": False,
                      "error_message": "E" * 5000}])

    r = _post(stock_client)

    assert r.status_code == 502
    assert len(r.text) < 1000, "an unbounded error_message must not be echoed whole"


def test_a_partial_failure_is_still_a_200(stock_client, crawl_returning):
    """Only *total* failure is an upstream error; mixed results are a normal 200."""
    crawl_returning([
        {"url": "https://ok.example", "success": True, "error_message": ""},
        {"url": "https://dead.example", "success": False, "error_message": ANTI_BOT},
    ])

    r = _post(stock_client, urls=("https://ok.example", "https://dead.example"))

    assert r.status_code == 200, r.status_code
    assert len(r.json()["results"]) == 2


def test_a_missing_error_message_still_produces_a_502(stock_client, crawl_returning):
    """Absent/None error_message must not turn the refusal into a crash."""
    crawl_returning([{"url": "https://dead.example", "success": False}])

    r = _post(stock_client)

    assert r.status_code == 502, r.status_code
