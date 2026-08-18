"""Flat top-level /crawl request fields (the documented frontend shape).

`API_DOCUMENTATION.md` hands frontend callers a flat request body:

    {"urls": [...], "max_pages": 15, "provider": "gemini/...", "ignore_links": false}

but `CrawlerRunConfig` wants those nested under `crawler_config` as strategy
objects. Before this layer existed, pydantic's default `extra="ignore"` dropped
every flat field silently - the request succeeded and none of the options took
effect, which is the worst possible failure mode.

`CrawlRequest` now desugars the flat form into the nested form. These tests pin
the mapping, the precedence rule (explicit nested config always wins), and the
deliberate decision that `include_external` alone must NOT trigger a deep crawl.

Desugaring happens in the model, so it lands *before* the admin-scope check in
server.py - the flat form is sugar over the same allowlisted builders, never a
way around them.
"""

import pytest

from schemas import CrawlRequest


# ─────────────────────── deep crawl (max_pages / max_depth) ───────────────────

def test_max_pages_and_max_depth_desugar_into_deep_crawl_strategy():
    req = CrawlRequest(urls=["https://example.com"], max_pages=15, max_depth=2)

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "BFSDeepCrawlStrategy",
        "max_pages": 15,
        "max_depth": 2,
    }


def test_include_external_rides_along_when_deep_crawl_is_requested():
    req = CrawlRequest(urls=["https://example.com"], max_pages=5, include_external=True)

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "BFSDeepCrawlStrategy",
        "max_pages": 5,
        "include_external": True,
        "max_depth": 1,  # required by the strategy; defaulted when not given
    }


def test_include_external_alone_does_not_trigger_a_deep_crawl():
    """A single-page crawl must stay single-page.

    `include_external` only qualifies a deep crawl's link-following; on its own
    it is not a request for one. Synthesising a strategy here would silently
    turn every plain crawl into a multi-page site crawl.
    """
    req = CrawlRequest(urls=["https://example.com"], include_external=True)

    assert "deep_crawl_strategy" not in req.crawler_config


def test_max_depth_alone_is_enough_to_request_a_deep_crawl():
    req = CrawlRequest(urls=["https://example.com"], max_depth=3)

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "BFSDeepCrawlStrategy",
        "max_depth": 3,
    }


# ──────────────────── LLM extraction (provider / instruction / schema) ────────

def test_provider_instruction_and_schema_desugar_into_extraction_strategy():
    target_schema = {"type": "object", "properties": {"tone": {"type": "string"}}}
    req = CrawlRequest(
        urls=["https://example.com"],
        provider="gemini/gemini-flash-latest",
        instruction="Extract the brand voice.",
        schema=target_schema,
    )

    assert req.crawler_config["extraction_strategy"] == {
        "name": "LLMExtractionStrategy",
        "provider": "gemini/gemini-flash-latest",
        "instruction": "Extract the brand voice.",
        "schema": target_schema,
    }


def test_instruction_alone_is_enough_to_request_extraction():
    req = CrawlRequest(urls=["https://example.com"], instruction="Summarise the page.")

    assert req.crawler_config["extraction_strategy"] == {
        "name": "LLMExtractionStrategy",
        "instruction": "Summarise the page.",
    }


# ──────────────────── markdown options (ignore_links / ignore_images) ────────

def test_ignore_links_and_ignore_images_desugar_into_markdown_generator():
    req = CrawlRequest(
        urls=["https://example.com"], ignore_links=True, ignore_images=True
    )

    assert req.crawler_config["markdown_generator"] == {
        "type": "DefaultMarkdownGenerator",
        "params": {"options": {"ignore_links": True, "ignore_images": True}},
    }


def test_ignore_links_false_is_still_forwarded():
    """`False` is a real choice, not an absent value - it must survive."""
    req = CrawlRequest(urls=["https://example.com"], ignore_links=False)

    assert req.crawler_config["markdown_generator"]["params"]["options"] == {
        "ignore_links": False
    }


# ───────────────────────────── precedence & no-ops ────────────────────────────

def test_explicit_nested_deep_crawl_strategy_wins_over_flat_fields():
    """The existing frontend sends the nested form; it must be untouched."""
    req = CrawlRequest(
        urls=["https://example.com"],
        max_pages=99,
        crawler_config={
            "deep_crawl_strategy": {"name": "DFSDeepCrawlStrategy", "max_pages": 3}
        },
    )

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "DFSDeepCrawlStrategy",
        "max_pages": 3,
    }


def test_explicit_nested_extraction_strategy_wins_over_flat_fields():
    req = CrawlRequest(
        urls=["https://example.com"],
        provider="gemini/gemini-flash-latest",
        crawler_config={
            "extraction_strategy": {
                "name": "LLMExtractionStrategy",
                "instruction": "nested wins",
            }
        },
    )

    assert req.crawler_config["extraction_strategy"] == {
        "name": "LLMExtractionStrategy",
        "instruction": "nested wins",
    }


def test_explicit_nested_markdown_generator_wins_over_flat_fields():
    req = CrawlRequest(
        urls=["https://example.com"],
        ignore_links=True,
        crawler_config={"markdown_generator": {"type": "DefaultMarkdownGenerator"}},
    )

    assert req.crawler_config["markdown_generator"] == {
        "type": "DefaultMarkdownGenerator"
    }


def test_request_without_flat_fields_leaves_crawler_config_empty():
    req = CrawlRequest(urls=["https://example.com"])

    assert req.crawler_config == {}


def test_flat_fields_do_not_survive_as_top_level_attributes():
    """They are sugar consumed by the validator, not part of the model."""
    req = CrawlRequest(urls=["https://example.com"], max_pages=5, provider="gemini/x")

    assert not hasattr(req, "max_pages")
    assert not hasattr(req, "provider")


def test_unrelated_crawler_config_keys_are_preserved():
    req = CrawlRequest(
        urls=["https://example.com"],
        max_pages=5,
        crawler_config={"cache_mode": "bypass", "stream": False},
    )

    assert req.crawler_config["cache_mode"] == "bypass"
    assert req.crawler_config["stream"] is False
    assert req.crawler_config["deep_crawl_strategy"]["max_pages"] == 5


# ───────────────────────────────── type errors ────────────────────────────────

@pytest.mark.parametrize(
    "field,value",
    [
        ("max_pages", "fifteen"),
        ("max_depth", 1.5),
        ("include_external", "yes"),
        ("ignore_links", "true"),
        ("instruction", 42),
        ("provider", ["gemini/x"]),
        ("schema", "not-an-object"),
    ],
)
def test_wrong_type_on_a_flat_field_is_rejected(field, value):
    """Reject at the edge with a 422 rather than desugaring garbage downstream."""
    with pytest.raises(ValueError):
        CrawlRequest(urls=["https://example.com"], **{field: value})


def test_bool_is_not_accepted_as_max_pages():
    """`True` is an int in Python; it is not a page budget."""
    with pytest.raises(ValueError):
        CrawlRequest(urls=["https://example.com"], max_pages=True)


# ───────────── the desugared shape must satisfy the real consumers ────────────
# Desugaring is only useful if what it emits is what api.py's allowlisted
# builders and the untrusted-config loader actually accept. These tests fail if
# the synthesised key names ever drift from the builders' allowlists.

def test_desugared_deep_crawl_shape_is_accepted_by_the_safe_builder():
    from api import _build_safe_deep_crawl_strategy
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    req = CrawlRequest(
        urls=["https://example.com"], max_pages=7, max_depth=3, include_external=False
    )
    strategy = _build_safe_deep_crawl_strategy(req.crawler_config["deep_crawl_strategy"])

    assert isinstance(strategy, BFSDeepCrawlStrategy)
    assert strategy.max_pages == 7
    assert strategy.max_depth == 3
    assert strategy.include_external is False


def test_desugared_extraction_keys_are_within_the_builders_allowlist():
    from api import _EXTRACTION_ALLOWED_FIELDS

    req = CrawlRequest(
        urls=["https://example.com"],
        provider="gemini/gemini-flash-latest",
        instruction="Extract the brand voice.",
        schema={"type": "object"},
    )
    keys = set(req.crawler_config["extraction_strategy"]) - {"name"}

    assert keys <= _EXTRACTION_ALLOWED_FIELDS


def test_desugared_deep_crawl_keys_are_within_the_builders_allowlist():
    from api import _DEEP_CRAWL_ALLOWED_FIELDS

    req = CrawlRequest(
        urls=["https://example.com"], max_pages=7, max_depth=3, include_external=True
    )
    keys = set(req.crawler_config["deep_crawl_strategy"]) - {"name"}

    assert keys <= _DEEP_CRAWL_ALLOWED_FIELDS


def test_desugared_markdown_generator_survives_an_untrusted_config_load():
    """ignore_links/ignore_images need no admin scope, unlike the other two."""
    from crawl4ai.async_configs import CrawlerRunConfig, Provenance

    req = CrawlRequest(
        urls=["https://example.com"], ignore_links=True, ignore_images=True
    )
    cfg = CrawlerRunConfig.load(req.crawler_config, provenance=Provenance.UNTRUSTED)

    assert cfg.markdown_generator is not None
    assert cfg.markdown_generator.options["ignore_links"] is True
    assert cfg.markdown_generator.options["ignore_images"] is True


# ───────────────────── the admin gate still applies (behavioral) ──────────────
# These drive the real /crawl route through the app. The crawl *execution* is
# stubbed - the fixture deliberately skips the lifespan, so there is no browser
# pool - but everything under test (auth gate, desugaring, admin-scope check,
# untrusted config load, strategy building) is the real code path. Assertions are
# on the config the route actually computed, not on the stub.

@pytest.fixture
def captured_crawl(server_module, monkeypatch):
    """Intercept crawl execution and record the arguments the route computed."""
    captured = {}

    async def fake_handle_crawl_request(**kwargs):
        captured.update(kwargs)
        return {"results": [{"success": True, "url": kwargs["urls"][0]}]}

    monkeypatch.setattr(
        server_module, "handle_crawl_request", fake_handle_crawl_request
    )
    return captured


def _bearer(scope=None):
    from auth import create_access_token

    claims = {"sub": "ops@x.com" if scope == "admin" else "user@x.com"}
    token = (
        create_access_token(claims, scope=scope)
        if scope
        else create_access_token(claims)  # scope defaults to "data"
    )
    return {"Authorization": f"Bearer {token}"}


def test_flat_deep_crawl_from_a_non_admin_is_rejected_not_ignored(
    stock_client, captured_crawl
):
    """The whole point of the gate: a data-scope caller must be refused loudly.

    Before desugaring existed, this returned success with max_pages quietly
    dropped. It must now reach the same UntrustedConfigError -> 400 path the
    nested form hits - the flat form is sugar, never a bypass.
    """
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "max_pages": 5},
        headers=_bearer(),
    )

    assert r.status_code == 400, r.status_code
    assert "Rejected config" in r.text


def test_flat_deep_crawl_from_an_admin_builds_a_real_strategy(
    stock_client, captured_crawl
):
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "max_pages": 5, "max_depth": 2},
        headers=_bearer("admin"),
    )

    assert r.status_code == 200, r.text
    strategy = captured_crawl["deep_crawl_strategy_override"]
    assert isinstance(strategy, BFSDeepCrawlStrategy)
    assert strategy.max_pages == 5
    assert strategy.max_depth == 2


def test_flat_markdown_options_need_no_admin_scope(stock_client, captured_crawl):
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "ignore_links": True},
        headers=_bearer(),
    )

    assert r.status_code == 200, r.text
    options = captured_crawl["crawler_config"]["markdown_generator"]["params"]["options"]
    assert options == {"ignore_links": True}


def test_flat_max_pages_alone_is_a_complete_deep_crawl_request(
    stock_client, captured_crawl
):
    """`max_pages` on its own must work - it is the documented headline case.

    `BFSDeepCrawlStrategy.__init__` takes `max_depth` as a *required* positional
    argument, so desugaring `{"max_pages": 5}` into a strategy dict without a
    depth raised TypeError -> HTTP 500 inside the builder.
    """
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "max_pages": 5},
        headers=_bearer("admin"),
    )

    assert r.status_code == 200, r.text
    strategy = captured_crawl["deep_crawl_strategy_override"]
    assert isinstance(strategy, BFSDeepCrawlStrategy)
    assert strategy.max_pages == 5


def test_max_pages_alone_gets_an_explicit_default_depth():
    req = CrawlRequest(urls=["https://example.com"], max_pages=5)

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "BFSDeepCrawlStrategy",
        "max_pages": 5,
        "max_depth": 1,
    }


def test_an_explicit_max_depth_is_never_overridden_by_the_default():
    req = CrawlRequest(urls=["https://example.com"], max_pages=5, max_depth=4)

    assert req.crawler_config["deep_crawl_strategy"]["max_depth"] == 4


@pytest.mark.parametrize("field", ["provider", "instruction", "schema"])
def test_flat_llm_extraction_from_a_non_admin_is_a_400_not_a_500(
    stock_client, captured_crawl, field
):
    """A refusal must be a 400, not a server error.

    `extraction_strategy` is on the untrusted *allowlist*, so unlike
    `deep_crawl_strategy` it is not key-rejected. The raw dict reached
    `CrawlerRunConfig.__init__`, which raised a bare ValueError that
    server.py did not catch -> 500. Clients retry 500s; a refusal must not
    look like an outage.
    """
    value = {"type": "object"} if field == "schema" else "x"
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], field: value},
        headers=_bearer(),
    )

    assert r.status_code == 400, r.status_code
    assert not captured_crawl, "a refused request must not reach the crawler"


def test_non_dict_crawler_config_with_a_flat_field_is_a_422_not_a_500(
    stock_client, captured_crawl
):
    """`dict([1, 2])` raises TypeError, which pydantic does not map to a 422."""
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "max_pages": 3, "crawler_config": [1, 2]},
        headers=_bearer("admin"),
    )

    assert r.status_code == 422, r.status_code


@pytest.mark.parametrize("field,value", [("max_pages", 0), ("max_depth", -1)])
def test_out_of_range_deep_crawl_bounds_are_rejected(field, value):
    """governor.clamp_deep_crawl only clamps *upward*, so these would survive.

    The floors differ: a crawl must fetch at least one page, but depth 0 is a
    real request (see below), so only depth < 0 is out of range.
    """
    with pytest.raises(ValueError):
        CrawlRequest(urls=["https://example.com"], **{field: value})


def test_max_depth_zero_means_this_page_only():
    """Depth 0 is a legitimate single-page crawl, not an invalid value.

    BFSDeepCrawlStrategy gates link-following on `next_depth > max_depth`
    (bfs_strategy.py), so depth 0 fetches the start URL and follows nothing.
    Rejecting it broke real callers asking for exactly one page.
    """
    req = CrawlRequest(urls=["https://example.com"], max_pages=1, max_depth=0)

    assert req.crawler_config["deep_crawl_strategy"] == {
        "name": "BFSDeepCrawlStrategy",
        "max_pages": 1,
        "max_depth": 0,
    }


def test_the_single_page_request_shape_survives_the_safe_builder():
    """The exact body a caller sends for 'just this one page'."""
    from api import _build_safe_deep_crawl_strategy

    req = CrawlRequest(
        urls=["https://example.com"],
        max_pages=1,
        max_depth=0,
        include_external=False,
        ignore_images=True,
    )
    strategy = _build_safe_deep_crawl_strategy(req.crawler_config["deep_crawl_strategy"])

    assert strategy.max_depth == 0
    assert strategy.max_pages == 1


def test_an_oversized_flat_budget_is_clamped_by_the_governor():
    """The flat form must not be a way around the server's page/depth ceiling.

    The route tests stub handle_crawl_request, which is where clamp_deep_crawl
    normally runs - so drive the real clamp directly over a flat-derived config.
    """
    from api import _build_safe_deep_crawl_strategy
    from governor import DEFAULT_MAX_DEPTH, DEFAULT_MAX_PAGES, clamp_deep_crawl
    from crawl4ai.async_configs import CrawlerRunConfig

    req = CrawlRequest(
        urls=["https://example.com"], max_pages=10**6, max_depth=1000
    )
    cfg = CrawlerRunConfig()
    cfg.deep_crawl_strategy = _build_safe_deep_crawl_strategy(
        req.crawler_config["deep_crawl_strategy"]
    )
    clamp_deep_crawl(cfg)

    assert cfg.deep_crawl_strategy.max_pages == DEFAULT_MAX_PAGES
    assert cfg.deep_crawl_strategy.max_depth == DEFAULT_MAX_DEPTH


def test_an_explicitly_null_nested_strategy_does_not_block_the_sugar():
    """`null` means "not set" - it must not leave a forbidden key in the config.

    Left in place, `{"deep_crawl_strategy": None}` both suppressed the sugar and
    tripped the untrusted forbidden-field check, yielding a confusing 400.
    """
    req = CrawlRequest(
        urls=["https://example.com"],
        max_pages=7,
        crawler_config={"deep_crawl_strategy": None},
    )

    assert req.crawler_config["deep_crawl_strategy"]["max_pages"] == 7


def test_flat_fields_alongside_crawler_configs_are_rejected_not_dropped():
    """`crawler_configs` (plural) wins downstream, silently discarding the sugar."""
    with pytest.raises(ValueError, match="crawler_configs"):
        CrawlRequest(
            urls=["https://a.com", "https://b.com"],
            max_pages=5,
            crawler_configs=[{"url_matcher": "*"}],
        )


def test_a_wrong_typed_flat_field_is_a_422_at_the_edge(stock_client, captured_crawl):
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "max_pages": "fifteen"},
        headers=_bearer(),
    )

    assert r.status_code == 422, r.status_code
    assert not captured_crawl, "a malformed request must not reach the crawler"
