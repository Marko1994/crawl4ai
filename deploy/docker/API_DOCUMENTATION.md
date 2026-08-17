# Crawl4AI Scraping & RAG API — Frontend Guide

Verified against `deploy/docker/server.py`, `schemas.py`, `api.py`, and `config.yml`.

> **This file is duplicated in the `crawlers` repo** as `API_DOCUMENTATION.md`.
> The two copies are intended to be byte-identical. If you change the API, update
> **both** — a doc that describes behavior the code does not have is how the
> previous version came to document a request shape the server silently ignored.
>
> ```bash
> diff crawl4ai/deploy/docker/API_DOCUMENTATION.md crawlers/API_DOCUMENTATION.md
> ```

---

## 1. Base URL

| Caller | URL |
| :--- | :--- |
| Another container on `kb-crawl` (e.g. `kb-worker`) | `http://crawl4ai:11235` |
| Local `curl` on the host | `http://localhost:11235` |
| Browser frontend (dev) | `/api-proxy/...` — the Vite dev server proxies to `http://localhost:11235` (`vite.config.js`) |

> **⚠️ The deployed container and `docker-compose.yml` currently disagree.**
> The compose file deliberately omits `ports:` and documents that crawl4ai should be
> reachable only over the internal `kb-crawl` network. But the **running** container
> publishes `0.0.0.0:11235->11235/tcp`, so the API is currently exposed on every host
> interface, including the LAN. Recreating the container from the compose file as
> written will close that port and break anything pointing at a LAN address.
> Don't hardcode a LAN IP; use the proxy.

**Always call the API through the Vite proxy from browser code.** `config.yml` sets
`cors_allow_origins: []` (deny by default), so a direct cross-origin `fetch` from a
browser is blocked regardless of whether the port is reachable.

Interactive Swagger docs: `http://localhost:11235/docs` — but `/docs` is **not** a
public path, and the auth gate only reads an `Authorization: Bearer` header, which a
plain browser navigation cannot send. Opening it in a browser returns `401`; fetch it
with `curl -H "Authorization: Bearer <token>"` instead.

Note that the flat fields in §3 do **not** appear in the OpenAPI schema. They are
consumed by a pre-validation step rather than declared as model fields, so
`/docs`, generated SDKs, and MCP clients only show `urls`, `browser_config`,
`crawler_config`, `crawler_configs`, and `hooks`. This document is the reference
for the flat form.

---

## 2. Authentication

Every request needs a bearer token:

```http
Authorization: Bearer <CRAWL4AI_API_TOKEN>
```

The token's value lives in `.llm.env` as `CRAWL4AI_API_TOKEN`. **Never hardcode it
in frontend source** — read it from an env var or have the user paste it at runtime,
the way the current UI does. Anything committed to a repo or shipped in a JS bundle
is a leaked credential.

### Two scopes — this determines which parameters you may use

`deploy/docker/auth_gate.py` accepts two kinds of credential and they are **not**
equivalent:

| Credential sent as `Bearer …` | Scope | Deep crawl / LLM extraction |
| :--- | :---: | :--- |
| The raw `CRAWL4AI_API_TOKEN` value | `admin` | **Allowed** |
| A JWT minted by `POST /token` | `data` | **Rejected with 400** |

`POST /token` always issues `scope="data"`. So a JWT obtained through the normal
token flow **cannot** run deep crawls or LLM extraction, even though it is also
sent as a bearer token. To use `max_pages`, `max_depth`, `include_external`,
`provider`, `instruction`, or `schema`, send the static operator token itself.

`ignore_links` and `ignore_images` work on **any** valid token.

> **On this deployment, `POST /token` is currently disabled** — it returns
> `403 "Token issuance is disabled: no api_token is configured on the server."`
> because `config.yml` has `security.api_token: ""` and `jwt_enabled: false`
> (the bearer token is supplied through the `CRAWL4AI_API_TOKEN` env var instead).
> There is therefore no way to obtain a `data`-scope JWT here: **every caller that
> can authenticate at all holds `admin` scope.** Treat the token as a full-privilege
> credential — it grants deep crawl and LLM extraction, and it is the only thing
> standing in front of them.

---

## 3. `POST /crawl`

The main endpoint. Crawl one or more URLs and get HTML, Markdown, and optionally
LLM-extracted JSON.

> **⚠️ The flat fields below require a rebuilt server image.** They were added in
> `deploy/docker/schemas.py`. Against an older running container the request still
> returns `200`, but every flat field is **silently dropped** — verified live: a
> request with `max_pages: 3` came back `200` with exactly one page. Until crawl4ai
> is redeployed, use the nested form shown further down, which works on both.

### Request body

```json
{
  "urls": ["https://aveosoft.com"],
  "max_pages": 15,
  "max_depth": 2,
  "include_external": false,
  "ignore_links": false,
  "ignore_images": false,
  "provider": "gemini/gemini-flash-latest",
  "instruction": "Extract the brand voice, tone, and key messaging pillars.",
  "schema": {
    "type": "object",
    "properties": {
      "tone": { "type": "string" },
      "pillars": { "type": "array", "items": { "type": "string" } }
    }
  },
  "browser_config": {
    "viewport_width": 390,
    "viewport_height": 844,
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
  }
}
```

### Parameters

| Field | Type | Default | What it does |
| :--- | :---: | :---: | :--- |
| `urls` | `array` | **required** | 1–100 URLs to crawl. |
| `max_pages` | `integer` | *none* | Page budget for a deep crawl. **Admin scope.** |
| `max_depth` | `integer` | *none* | Link depth to follow. `1` = homepage + direct links. **Admin scope.** |
| `include_external` | `boolean` | `false` | Follow links off-domain. Only meaningful alongside `max_pages`/`max_depth`. **Admin scope.** |
| `ignore_links` | `boolean` | `false` | `true` strips `[text](url)` from Markdown. |
| `ignore_images` | `boolean` | `false` | `true` strips `![alt](url)` from Markdown. |
| `provider` | `string` | `gemini/gemini-flash-latest` | LLM for extraction. **Admin scope.** See §5. |
| `instruction` | `string` | *none* | Plain-English extraction goal. Presence of this (or `schema`/`provider`) turns on LLM extraction. **Admin scope.** |
| `schema` | `object` | *none* | JSON Schema the model must fill. **Admin scope.** |
| `browser_config` | `object` | *none* | Viewport emulation: `viewport_width`, `viewport_height`, `user_agent`. |

**Deep crawl only starts if `max_pages` or `max_depth` is present.** There is no
implicit default — omit both and you get a single-page crawl. Sending
`include_external` alone does *not* trigger a deep crawl.

The server clamps deep crawls regardless of what you ask for: `config.yml`
`limits.max_pages: 100` and `limits.max_depth: 5`.

<details>
<summary>Equivalent nested form (also supported)</summary>

The flat fields above are sugar. `deploy/docker/schemas.py` folds them into the
nested `crawler_config` shape below before validation. If you send both, **the
nested form wins**.

```json
{
  "urls": ["https://aveosoft.com"],
  "crawler_config": {
    "deep_crawl_strategy": {
      "name": "BFSDeepCrawlStrategy",
      "max_pages": 15, "max_depth": 2, "include_external": false
    },
    "extraction_strategy": {
      "name": "LLMExtractionStrategy",
      "provider": "gemini/gemini-flash-latest",
      "instruction": "…", "schema": { }
    },
    "markdown_generator": {
      "type": "DefaultMarkdownGenerator",
      "params": { "options": { "ignore_links": false, "ignore_images": false } }
    }
  }
}
```

Deep-crawl strategies: `BFSDeepCrawlStrategy` (default), `DFSDeepCrawlStrategy`,
`BestFirstCrawlingStrategy`.
</details>

### Response

```json
{
  "results": [
    {
      "url": "https://aveosoft.com",
      "success": true,
      "status_code": 200,
      "html": "<!DOCTYPE html>…",
      "cleaned_html": "<html>…",
      "markdown": {
        "raw_markdown": "# Aveo…",
        "markdown_with_citations": "# Aveo…[1]",
        "references_markdown": "[1]: https://…",
        "fit_markdown": "# Aveo…",
        "fit_html": "<h1>Aveo</h1>…"
      },
      "extracted_content": "{\"tone\":\"confident\"}",
      "metadata": { "title": "…", "description": "…", "depth": 0 },
      "links": { "internal": [], "external": [] },
      "media": { "images": [] },
      "error_message": null
    }
  ]
}
```

| Field | Type | Notes |
| :--- | :---: | :--- |
| `results` | `array` | One entry per crawled page. A deep crawl returns many. |
| `results[].url` | `string` | The crawled URL. |
| `results[].success` | `boolean` | Per-page success. Check this per entry. |
| `results[].status_code` | `integer` | HTTP status. |
| `results[].html` | `string` | Full rendered DOM. |
| `results[].cleaned_html` | `string` | DOM minus `<script>`/`<style>`. |
| `results[].markdown` | **`object`** | **Not a string.** Use `markdown.raw_markdown`. See the warning below about `fit_markdown`. |
| `results[].extracted_content` | `string` | LLM mode only. A **JSON string** — you must `JSON.parse()` it. |
| `results[].metadata` | `object` | `title`, `description`, `depth`. |
| `results[].error_message` | `string` | The failure reason when `success` is `false`; an **empty string** on success, not `null`. |

> `markdown` is an object, and there is no top-level `raw_markdown` — it is
> `markdown.raw_markdown`.

**`fit_markdown` is an empty string unless you attach a content filter.** It is
populated only by a filter such as `PruningContentFilter`; neither the flat
`ignore_links`/`ignore_images` options nor the nested example above attaches one.
Because `''` is not nullish, the tempting guard silently yields empty output:

```javascript
// WRONG - fit_markdown is '' , which ?? does NOT treat as absent
const md = r.markdown?.fit_markdown ?? r.markdown?.raw_markdown;   // -> ''

// RIGHT - || falls back on the empty string
const md = r.markdown?.fit_markdown || r.markdown?.raw_markdown || '';
```

Prefer `raw_markdown` unless you have deliberately configured a filter.

If **every** result fails, the endpoint returns `500` instead of a `200` with a
results array.

### Deep crawl is not available on the streaming path

`POST /crawl/stream`, and `POST /crawl` with `crawler_config.stream: true`, reject a
deep crawl with `400 Rejected request: field 'deep_crawl_strategy' is not permitted`
— **even for an admin token.** The admin escalation is applied only on the
non-streaming path, so `max_pages`/`max_depth` (flat or nested) cannot be combined
with streaming. Use the non-streaming `POST /crawl` for multi-page crawls.

---

## 4. `POST /multi-device-html`

Desktop, mobile, and tablet HTML in one call. Single page only — no deep crawl,
no LLM extraction.

**Request**

```json
{ "url": "https://aveosoft.com", "devices": ["desktop", "mobile", "tablet"] }
```

Viewports: desktop `1920×1080`, mobile `390×844` (iPhone UA), tablet `768×1024` (iPad UA).

**Response** — note each device is an **object**, not an HTML string:

```json
{
  "url": "https://aveosoft.com",
  "success": true,
  "html": {
    "desktop": { "success": true,  "html": "<!DOCTYPE html>…" },
    "mobile":  { "success": true,  "html": "<!DOCTYPE html>…" },
    "tablet":  { "success": false, "error": "Navigation timeout" }
  }
}
```

Read `data.html.desktop.html`, **not** `data.html.desktop`. A device that fails has
an `error` key and **no** `html` key, while the top-level `success` is
`any(device succeeded)` — so it can be `true` with a device still broken. Check
each device individually.

Device names are case-insensitive but **unrecognised names are silently dropped**,
not rejected: `["desktop", "phone"]` returns only `desktop`. You get a `400` only
when *no* requested name is valid. Always confirm the keys you expected are present
in the response.

---

## 5. LLM providers

`config.yml` `llm.allowed_providers` gates this. Current state:

All statuses below were **verified with live extraction calls** against the running
server, not read off the allowlist.

| Provider string | Status |
| :--- | :--- |
| `gemini/gemini-flash-latest` | ✅ **Works. Default — use this.** |
| `gemini/gemma-4-26b-a4b-it` | ✅ Works (free-tier quota). |
| `gemini/gemma-4-31b-it` | ✅ Works (free-tier quota). Not in `allowed_providers`, but passes on family match. |
| `gemini/gemini-2.0-flash` | ❌ Retired by Google. |
| `gemini/gemini-2.0-flash-lite` | ❌ Retired by Google. |
| `gemini/gemini-1.5-flash` | ❌ Retired by Google. |
| `gemini/gemini-1.5-pro` | ❌ Retired by Google. |
| `openai/gpt-4o` | ❌ Disabled — commented out in `config.yml`, no `OPENAI_API_KEY`. Returns `400`. |

### A retired model does NOT return an error status

This is the trap. The allowlist matches only the provider **family** (the part
before `/`), so any `gemini/*` string passes server validation. A retired model
then fails at the upstream API, but the crawl itself succeeded — so you get:

- HTTP **`200`**
- `results[].success` = **`true`**
- and the failure buried inside `extracted_content` as `"error": true`

```javascript
const parsed = JSON.parse(result.extracted_content);
const blocks = Array.isArray(parsed) ? parsed : [parsed];
if (blocks.some(b => b.error)) {
  throw new Error('LLM extraction failed — check the provider model name');
}
```

Neither the HTTP status nor `success` tells you extraction worked. **You must
inspect `extracted_content` for `error: true`.**

`api_token` and `base_url` are always resolved server-side and cannot be set by a
request.

---

## 6. Frontend integration

```javascript
const API = '/api-proxy';                 // Vite proxies to the container
const token = getCrawl4aiToken();         // env var or user-supplied; never hardcoded

async function crawl({ url, deepCrawl = false, extract = false }) {
  const body = { urls: [url], ignore_links: false, ignore_images: false };

  if (deepCrawl) {                        // admin-scope token required
    body.max_pages = 15;
    body.max_depth = 2;
    body.include_external = false;
  }

  if (extract) {                          // admin-scope token required
    body.provider = 'gemini/gemini-flash-latest';
    body.instruction = 'Extract the brand voice, tone, and messaging pillars.';
    body.schema = {
      type: 'object',
      properties: { tone: { type: 'string' } },
    };
  }

  const res = await fetch(`${API}/crawl`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Crawl failed (${res.status}): ${detail}`);
  }

  const { results } = await res.json();

  return results.filter(r => r.success).map(r => ({
    url: r.url,
    // markdown is an object, not a string. Use || not ?? - fit_markdown is ''
    // (not null) unless a content filter is attached.
    markdown: r.markdown?.fit_markdown || r.markdown?.raw_markdown || '',
    // extracted_content is a JSON *string*, and a retired model reports
    // failure inside it while the HTTP status stays 200.
    data: r.extracted_content ? JSON.parse(r.extracted_content) : null,
  }));
}

async function fetchAllDeviceHtml(url) {
  const res = await fetch(`${API}/multi-device-html`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ url, devices: ['desktop', 'mobile', 'tablet'] }),
  });

  const data = await res.json();

  // Each device is an object: { success, html } or { success, error }
  return Object.fromEntries(
    Object.entries(data.html).map(([device, r]) => [device, r.success ? r.html : null])
  );
}
```

---

## 7. Error responses

| Status | Meaning | Fix |
| :--- | :---: | :--- |
| `401` | Missing/invalid token, or not sent as `Authorization: Bearer …` | Check the token matches `CRAWL4AI_API_TOKEN`. There is no `X-API-Key` support. |
| `422` | Malformed body — wrong type on a field, empty `urls` | Check types: `max_pages`/`max_depth` integers, `schema` an object. |
| `400` `Rejected config` | Deep crawl or LLM extraction attempted **without admin scope** | Send the static `CRAWL4AI_API_TOKEN`, not a `/token` JWT. |
| `400` `LLM provider not allowed` | `provider` outside `allowed_providers` | Use `gemini/gemini-flash-latest`. |
| `413` | Body over 10 MiB | Reduce `urls`. |
| `429` | Rate limit — 1000/min | Back off. (The per-caller job quota applies to `/crawl/job`, not `/crawl`, and is disabled: `queue.per_principal: 0`.) |
| `500` | Every URL failed | Inspect `error_message` on the results. |

---

## 8. Rate limits & caps

| Cap | Value | Source |
| :--- | :---: | :--- |
| Requests | 1000/minute | `config.yml` `rate_limiting.default_limit` |
| URLs per request | 100 | `schemas.py` |
| Deep-crawl pages | 100 (hard clamp) | `governor.py` `DEFAULT_MAX_PAGES` |
| Deep-crawl depth | 5 (hard clamp) | `governor.py` `DEFAULT_MAX_DEPTH` |
| Body size | 10 MiB | `config.yml` `limits.max_body_bytes` |

> The deep-crawl clamps come from **module constants in `governor.py`**, not from
> config. `api.py` calls `clamp_deep_crawl(crawler_config)` with no arguments, so
> the `limits.max_pages` / `limits.max_depth` keys in `config.yml` are read
> nowhere — editing them has no effect. Change the constants to raise the ceiling.

Deep crawls with LLM extraction run one LLM call **per page**, which hits Gemini
rate limits fast. Prefer crawling first, then extracting from the aggregated
Markdown in a second single-page call — this is what the existing UI does.
