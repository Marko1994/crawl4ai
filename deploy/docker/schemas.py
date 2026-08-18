from typing import Any, List, Optional, Dict
from enum import Enum
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from utils import FilterType


# ───────────────────── flat /crawl request fields (sugar) ─────────────────────
# API_DOCUMENTATION.md documents a flat request body for frontend callers:
#
#     {"urls": [...], "max_pages": 15, "provider": "gemini/...", "ignore_links": false}
#
# None of these are CrawlerRunConfig fields - they belong to nested strategy
# objects (deep_crawl_strategy / extraction_strategy / markdown_generator).
# Because this model does not set extra="forbid", pydantic's default
# extra="ignore" used to drop them silently: the request succeeded, and not one
# of the options took effect. CrawlRequest therefore desugars the flat form into
# the nested form up front.
#
# This is sugar only. Desugaring runs at model-validation time, i.e. *before*
# the admin-scope check in server.py, so a flat deep_crawl/extraction request
# still goes through the same allowlisted builders in api.py and is still
# refused for non-admin callers. It is not a way around that gate.
_FLAT_DEEP_CRAWL_FIELDS = ("max_pages", "max_depth", "include_external")
_FLAT_EXTRACTION_FIELDS = ("provider", "instruction", "schema")
_FLAT_MARKDOWN_FIELDS = ("ignore_links", "ignore_images")

# Validated here rather than downstream so a typo is a 422 at the edge instead
# of a confusing failure inside a strategy constructor.
_FLAT_FIELD_TYPES = {
    "max_pages": int,
    "max_depth": int,
    "include_external": bool,
    "ignore_links": bool,
    "ignore_images": bool,
    "provider": str,
    "instruction": str,
    "schema": dict,
}

# Lower bounds, checked here because governor.clamp_deep_crawl only clamps
# *upward* - an out-of-range low value would otherwise reach the strategy.
# The floors differ: a crawl must fetch at least one page, but depth 0 is a
# real request meaning "this page only, follow no links" (BFSDeepCrawlStrategy
# gates link-following on `next_depth > max_depth`), so 0 must be accepted.
_FLAT_FIELD_MINIMUMS = {
    "max_pages": 1,
    "max_depth": 0,
}


class CrawlRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=100)
    browser_config: Optional[Dict] = Field(default_factory=dict)
    crawler_config: Optional[Dict] = Field(default_factory=dict)
    crawler_configs: Optional[List[Dict]] = Field(
        default=None,
        description=(
            "List of per-URL CrawlerRunConfig dicts for arun_many(). "
            "When provided, each config can include a 'url_matcher' pattern "
            "to match against specific URLs. Takes precedence over crawler_config."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _desugar_flat_fields(cls, data):
        """Fold the documented flat fields into nested crawler_config strategies.

        An explicitly supplied nested strategy always wins - the flat form never
        overwrites what the caller spelled out, so clients already sending the
        nested shape are unaffected.
        """
        if not isinstance(data, dict):
            return data

        # An explicit null means "not set", same as omitting the key.
        present = {
            key: data[key]
            for key in _FLAT_FIELD_TYPES
            if key in data and data[key] is not None
        }
        if not present:
            return data

        for key, value in present.items():
            expected = _FLAT_FIELD_TYPES[key]
            # bool is a subclass of int in Python; True is not a page budget.
            if expected is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{key} must be an integer")
                minimum = _FLAT_FIELD_MINIMUMS.get(key)
                if minimum is not None and value < minimum:
                    raise ValueError(f"{key} must be at least {minimum}")
            elif not isinstance(value, expected):
                article = "an object" if expected is dict else f"a {expected.__name__}"
                raise ValueError(f"{key} must be {article}")

        # crawler_configs (plural) takes precedence over crawler_config
        # downstream, which would silently discard everything desugared here -
        # the exact failure mode this sugar exists to remove. Refuse instead.
        if data.get("crawler_configs"):
            raise ValueError(
                "flat fields ("
                + ", ".join(sorted(present))
                + ") cannot be combined with crawler_configs; "
                "put the equivalent nested config in each crawler_configs entry"
            )

        raw_crawler_config = data.get("crawler_config")
        if raw_crawler_config is not None and not isinstance(raw_crawler_config, dict):
            # dict([1, 2]) raises TypeError, which pydantic does not turn into a
            # validation error - it would surface as a 500 rather than a 422.
            raise ValueError("crawler_config must be an object")

        data = dict(data)
        crawler_config = dict(raw_crawler_config or {})

        def _explicit(strategy_key: str) -> bool:
            """True when the caller actually supplied a nested strategy.

            A null value counts as absent, matching the treatment of flat nulls.
            Leaving the null in place would both suppress the sugar and trip the
            untrusted forbidden-field check.
            """
            if crawler_config.get(strategy_key) is not None:
                return True
            crawler_config.pop(strategy_key, None)
            return False

        # include_external alone is not a request for a deep crawl - it only
        # qualifies one. Synthesising a strategy for it would silently turn a
        # single-page crawl into a whole-site crawl.
        wants_deep_crawl = "max_pages" in present or "max_depth" in present
        if wants_deep_crawl and not _explicit("deep_crawl_strategy"):
            strategy = {
                "name": "BFSDeepCrawlStrategy",
                **{k: present[k] for k in _FLAT_DEEP_CRAWL_FIELDS if k in present},
            }
            # max_depth is a *required* positional arg of every deep-crawl
            # strategy, so `max_pages` on its own would raise TypeError inside
            # the builder. Depth 1 = the given page plus its direct links.
            strategy.setdefault("max_depth", 1)
            crawler_config["deep_crawl_strategy"] = strategy

        if any(k in present for k in _FLAT_EXTRACTION_FIELDS) and not _explicit(
            "extraction_strategy"
        ):
            crawler_config["extraction_strategy"] = {
                "name": "LLMExtractionStrategy",
                **{k: present[k] for k in _FLAT_EXTRACTION_FIELDS if k in present},
            }

        if any(k in present for k in _FLAT_MARKDOWN_FIELDS) and not _explicit(
            "markdown_generator"
        ):
            crawler_config["markdown_generator"] = {
                "type": "DefaultMarkdownGenerator",
                "params": {
                    "options": {
                        k: present[k] for k in _FLAT_MARKDOWN_FIELDS if k in present
                    }
                },
            }

        for key in _FLAT_FIELD_TYPES:
            data.pop(key, None)
        data["crawler_config"] = crawler_config
        return data


class HookSpec(BaseModel):
    """A single declarative hook: a fixed action plus schema-validated params.

    Arbitrary Python (the old `code` map) is no longer accepted - it was an
    exec()-based RCE surface. Available actions are enumerated by GET /hooks/info
    and validated server-side by hook_registry.py.
    """
    action: str = Field(..., description="One of the registered hook actions")
    params: Dict[str, Any] = Field(default_factory=dict, description="Action parameters")


class HookConfig(BaseModel):
    """Configuration for declarative hooks."""
    hooks: List[HookSpec] = Field(
        default_factory=list,
        max_length=10,
        description="Declarative hook specs (action + params), max 10",
    )
    timeout: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Timeout in seconds for each hook execution",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "hooks": [
                    {"action": "block_resources", "params": {"resource_types": ["image", "font"]}},
                    {"action": "scroll_to_bottom", "params": {"max_steps": 10, "delay_ms": 500}},
                ],
                "timeout": 30,
            }
        }


class CrawlRequestWithHooks(CrawlRequest):
    """Extended crawl request with hooks support"""
    hooks: Optional[HookConfig] = Field(
        default=None,
        description="Optional user-provided hook functions"
    )

class MarkdownRequest(BaseModel):
    """Request body for the /md endpoint."""
    url: str                    = Field(...,  description="Absolute http/https URL to fetch")
    f:   FilterType             = Field(FilterType.FIT, description="Content‑filter strategy: fit, raw, bm25, or llm")
    q:   Optional[str] = Field(None,  description="Query string used by BM25/LLM filters")
    c:   Optional[str] = Field("0",   description="Cache‑bust / revision counter")
    provider: Optional[str] = Field(None, description="LLM provider override (e.g., 'anthropic/claude-3-opus')")
    temperature: Optional[float] = Field(None, description="LLM temperature override (0.0-2.0)")
    # base_url removed: a request-supplied LLM endpoint was a credential-exfil
    # vector. The endpoint is derived server-side from the provider name.


class RawCode(BaseModel):
    code: str

class HTMLRequest(BaseModel):
    url: str
    
class ScreenshotRequest(BaseModel):
    url: str
    screenshot_wait_for: Optional[float] = 2
    wait_for_images: Optional[bool] = False
    # output_path removed: callers never name a filesystem path (it was an
    # arbitrary-write -> RCE vector). The server writes to the sandboxed
    # artifact store and returns an opaque artifact_id.


class PDFRequest(BaseModel):
    url: str
    # output_path removed (see ScreenshotRequest).


class JSEndpointRequest(BaseModel):
    url: str
    scripts: List[str] = Field(
        ...,
        description="List of separated JavaScript snippets to execute"
    )


class WebhookConfig(BaseModel):
    """Configuration for webhook notifications."""
    webhook_url: HttpUrl
    webhook_data_in_payload: bool = False
    webhook_headers: Optional[Dict[str, str]] = None

    @field_validator("webhook_headers")
    @classmethod
    def _validate_headers(cls, v):
        # Reject unsafe outbound headers early (422). Mirrors
        # webhook.sanitize_webhook_headers; kept inline to avoid an import cycle.
        if not v:
            return v
        from webhook import sanitize_webhook_headers
        return sanitize_webhook_headers(v)


class WebhookPayload(BaseModel):
    """Payload sent to webhook endpoints."""
    task_id: str
    task_type: str  # "crawl", "llm_extraction", etc.
    status: str  # "completed" or "failed"
    timestamp: str  # ISO 8601 format
    urls: List[str]
    error: Optional[str] = None
    data: Optional[Dict] = None  # Included only if webhook_data_in_payload=True