#!/usr/bin/env python3
"""Generate openapi.yaml / openapi.json / llms.txt from endpoints.json.

endpoints.json is the single source of truth. Everything else in agent-kit
that describes the API surface -- the OpenAPI document, the llms.txt agents
crawl, the MCP tool schemas, the gateway's price table and the Bazaar
discovery metadata -- is derived from it so the four can never drift apart.

Usage:  python agent-kit/spec/generate.py
"""

import json
import pathlib
import re
import shutil
from pathlib import Path

import yaml

SPEC_DIR = Path(__file__).resolve().parent
SOURCE = SPEC_DIR / "endpoints.json"


def load():
    with SOURCE.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def price_label(ep):
    """Human/agent-readable price for one endpoint."""
    if ep["price_model"] == "per_item":
        return f"${ep['unit_price_usd']} per item (max {ep['max_items']})"
    return f"${ep['price_usd']} per call"


def price_extension(ep):
    """The x- block the gateway and Bazaar both read."""
    if ep["price_model"] == "per_item":
        return {
            "model": "per_item",
            "unitPriceUsd": ep["unit_price_usd"],
            "countParam": ep["count_param"],
            "countSeparator": ep["count_separator"],
            "maxItems": ep["max_items"],
            "countDedup": ep.get("count_dedup", True),
        }
    return {"model": "flat", "priceUsd": ep["price_usd"]}


# --------------------------------------------------------------------------
# OpenAPI
# --------------------------------------------------------------------------

def to_parameters(input_schema):
    """JSON Schema object -> list of OpenAPI query parameters."""
    required = set(input_schema.get("required", []))
    params = []
    for name, prop in input_schema.get("properties", {}).items():
        schema = {k: v for k, v in prop.items() if k != "description"}
        params.append(
            {
                "name": name,
                "in": "query",
                "required": name in required,
                "description": prop.get("description", ""),
                "schema": schema,
            }
        )
    return params


def error_responses(conventions):
    """Shared error responses, worded from the live blueprint behaviour."""
    out = {}
    for code, text in conventions["error_semantics"].items():
        out[code] = {
            "description": text,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"}
                }
            },
        }
    return out


def build_openapi(spec):
    api = spec["api"]
    conventions = spec["conventions"]
    shared_errors = error_responses(conventions)

    paths = {}
    for ep in spec["endpoints"]:
        operation = {
            "operationId": ep["name"],
            "summary": ep["summary"],
            "description": (
                f"{ep['description']}\n\n"
                f"**Price:** {price_label(ep)}\n\n"
                f"**Notes:** {ep.get('notes', '-')}"
            ),
            "tags": ["Amazon Data"],
            "parameters": to_parameters(ep["input_schema"]),
            "responses": {
                "200": {
                    "description": "Success.",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object"},
                            "example": ep["output_example"],
                        }
                    },
                },
                **shared_errors,
            },
            "x-apiguru-price": price_extension(ep),
            "x-apiguru-source": ep["source"],
        }

        for path in [ep["path"]] + ep.get("aliases", []):
            entry = {"get": operation}
            if path in ep.get("aliases", []):
                # Alias routes share the handler; mark them so tooling picks
                # the canonical path and does not emit a duplicate tool.
                entry = json.loads(json.dumps(entry))
                entry["get"]["deprecated"] = True
                entry["get"]["operationId"] = f"{ep['name']}_alias"
                entry["get"]["summary"] = f"{ep['summary']} (legacy alias of {ep['path']})"
            paths[path] = entry

    return {
        "openapi": "3.1.0",
        "info": {
            "title": api["name"],
            "version": api["version"],
            "summary": api["description"],
            "description": (
                f"{api['description']}\n\n"
                "## Two ways to call this API\n\n"
                "**Humans / existing customers** — send `X-API-KEY` against "
                f"`{api['base_url']}`.\n\n"
                "**AI agents** — call "
                f"`{api['agent_base_url']}` with no credentials at all. "
                f"You get {api['free_tier']['probes_per_client']} free calls "
                f"per {api['free_tier']['window_hours']}h, then an HTTP 402 "
                "with a `PAYMENT-REQUIRED` challenge you settle in "
                f"{api['auth']['agent']['asset']} on "
                f"{api['auth']['agent']['network']}. No account, no API key, "
                "no subscription.\n\n"
                f"An MCP server is available at `{api['mcp_url']}`.\n\n"
                "## Retry policy\n\n"
                f"{conventions['retry_policy']}\n"
            ),
            "contact": {"email": api["contact"]},
        },
        "servers": [
            {"url": api["base_url"], "description": "Keyed API (X-API-KEY)"},
            {"url": api["agent_base_url"], "description": "Keyless agent gateway (x402, USDC on Base)"},
        ],
        "security": [{"ApiKeyHeader": []}, {"ApiKeyQuery": []}, {}],
        "components": {
            "securitySchemes": {
                "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-KEY"},
                "ApiKeyQuery": {"type": "apiKey", "in": "query", "name": "api_key"},
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "description": (
                        "Every field an agent needs to decide what to do next "
                        "is in the body, not only in headers -- many agent "
                        "HTTP clients show the model the body and hide the "
                        "status line and headers entirely."
                    ),
                    "properties": {
                        "error": {"type": "string", "description": "Human-readable cause."},
                        "code": {
                            "type": "string",
                            "description": "Stable machine-readable cause; branch on this.",
                            "enum": [
                                "missing_parameter", "invalid_parameter",
                                "too_many_items", "payment_required",
                                "unknown_endpoint", "upstream_unavailable",
                                "rate_limited", "upstream_rejected",
                            ],
                        },
                        "param": {"type": "string", "description": "The offending parameter, when there is one."},
                        "http_status": {"type": "integer"},
                        "billed": {"type": "boolean", "description": "Whether this answer cost you anything."},
                        "retryable": {"type": "boolean", "description": "Whether repeating the identical request can succeed."},
                        "next_step": {"type": "string", "description": "What to do about it, in words."},
                        "free_calls_remaining": {"type": "integer"},
                        "price_next_call": {"type": "string"},
                        "message": {"type": "string"},
                        "request_id": {"type": "string"},
                    },
                    "required": ["error"],
                }
            },
        },
        "paths": paths,
        "x-apiguru-geos": spec["geos"],
        "x-apiguru-conventions": conventions,
    }


# --------------------------------------------------------------------------
# llms.txt
# --------------------------------------------------------------------------

def _example_urls(spec):
    """One complete, fetchable URL per endpoint.

    Built from the schema so they cannot rot: required parameters get a real
    sample value, and geo is pinned so the URL is unambiguous rather than
    relying on the US default.
    """
    conv = spec["conventions"]
    samples = {
        "asin": conv["sample_asin"],
        "asins": f"{conv['sample_asin']},B0014C0LUC",
        "query": "wireless+headphones",
        "seller_id": conv["sample_seller_id"],
        "seller_ids": conv["sample_seller_id"],
    }
    urls = []
    for ep in spec["endpoints"]:
        required = ep["input_schema"].get("required", [])
        has_geo = "geo" in ep["input_schema"].get("properties", {})
        # One required parameter -> the path form, which is what we want an
        # agent to copy. Anything else keeps a query string because a segment
        # could not say which value it binds to.
        if len(required) == 1:
            value = samples.get(required[0], "example")
            urls.append(f"{ep['path']}/{value}/US" if has_geo else f"{ep['path']}/{value}")
            continue
        params = [f"{name}={samples.get(name, 'example')}" for name in required]
        if has_geo:
            params.append("geo=US")
        urls.append(f"{ep['path']}?{'&'.join(params)}" if params else ep["path"])
    return urls


def build_llms_txt(spec):
    api = spec["api"]
    conventions = spec["conventions"]
    free = api["free_tier"]
    agent_auth = api["auth"]["agent"]
    geos = ", ".join(spec["geos"].keys())

    lines = [
        f"# {api['name']}",
        "",
        f"> {api['description']}",
        "",
        "Apiguru is callable by AI agents with **no account, no API key and no "
        "subscription**. This is live now, not a plan.",
        "",
        "## Quickstart",
        "",
        "```bash",
        f"curl '{api['agent_base_url']}/v2/product-details/{conventions['sample_asin']}'",
        "```",
        "",
        "No headers. No signup. That returns live Amazon data.",
        "",
        # The path form is shown FIRST and unconditionally, on purpose. It
        # used to appear further down under "if your client loses query
        # strings", and a model read that section and still sent
        # ?query=... -- because no client knows in advance that it is one of
        # the ones that drops them. A conditional instruction whose condition
        # the reader cannot evaluate never fires.
        "**Put values in the path, not the query string.** Both work:",
        "",
        f"    {api['agent_base_url']}/v2/product-details/{conventions['sample_asin']}",
        f"    {api['agent_base_url']}/v2/product-details?asin={conventions['sample_asin']}",
        "",
        "and they mean the same thing. Prefer the first. Several agent HTTP "
        "clients -- Claude's web fetch, and others that wrap a fetch and hand "
        "the model back a string -- silently drop the query string from a URL "
        "the model composed, and you cannot tell from inside whether yours is "
        "one of them: the request simply fails, or returns a stale cached "
        "answer for a URL you did not ask for. A path has nothing to drop.",
        "",
        "The rule: the one **required** parameter goes in the path, and the "
        "marketplace may follow it. Optional filters (`sort_by`, `brand`, "
        "price bounds, `page`) still need a query string, so a call carrying "
        "them may lose them and fall back to defaults -- check the response "
        "rather than assuming a filter applied.",
        "",
        # Deliberately outside a code fence and one per line. An agent fetcher
        # told us it "only registers URLs that appear in previously fetched
        # content", and picked up only the first few from the x402 catalogue --
        # so endpoints it never saw as plain text were unreachable to it.
        "## Working URLs you can fetch right now",
        "",
        "Copy one, change the value at the end, fetch it. Every one of these "
        "is a complete, valid request with nothing that can be dropped in "
        "transit:",
        "",
    ] + [
        f"- {api['agent_base_url']}{ex}"
        for ex in _example_urls(spec)
    ] + [
        "",
        "## When free calls run out",
        "",
        "Free calls are counted per client. If you get a 402 you have used "
        "the free allowance for whichever client you are; the request itself "
        "was fine. Settle the attached x402 challenge, send an API key as "
        "`X-API-KEY`, or come back after the window rolls.",
        "",
        "## Clients that cannot send headers, or lose the query string",
        "",
        "You do not need to set any request header to use this API. There is "
        "no content negotiation and nothing to opt into: a bare `GET` with no "
        "headers at all is a first-class client, and no request is refused "
        "for lacking one. Headers are optional extras: `X-API-KEY` to bill an "
        "account, `PAYMENT-SIGNATURE` to pay.",
        "",
        "What does bite agent fetchers is the **query string**, and there are "
        "two different failures. Which one you have decides what to do, and "
        "they need opposite advice, so establish which before retrying.",
        "",
        "**1. The query string is dropped.** You built a URL with `?asin=...` "
        "and it arrived here without it. You will get a 400 naming the "
        "parameter in `param`; if you believe you did send it, that is the "
        "diagnosis. Retry in the path form, which has nothing to lose: "
        f"`{api['agent_base_url']}/v2/product-details/{conventions['sample_asin']}/DE` "
        "carries the ASIN and the marketplace. Changing values in a path URL "
        "works fine for this kind of client.",
        "",
        "**2. Only URLs seen verbatim are allowed.** Some fetchers refuse any "
        "URL that has not appeared in content they already fetched. Copying a "
        "URL from the list above works; changing the ASIN in it is refused, "
        "because the changed URL was never seen. **The path form does not "
        "help here** -- do not keep trying URL variants, you are spending "
        "free calls on a client limitation. Use a shell or code tool "
        "(`curl`), or the MCP server, where arguments are passed structurally "
        "and there is no URL to allowlist.",
        "",
        "The tell between them: if a URL you copied verbatim works and the "
        "same URL with one value changed is refused before any request is "
        "made, you have (2). If your changed URL is sent but arrives here "
        "stripped, you have (1).",
        "",
        "**Responses link to themselves, so you can traverse.** Every answer "
        "carries a `_links` object as its FIRST key, and every search result "
        "row carries a `details_url`. They are this API's own URLs, in path "
        "form, inside a body you have already fetched -- which is what makes "
        "them usable by a client in case (2). One seeded call is therefore "
        "enough: from a search you can reach all 48 products, and from a "
        "product you can reach its reviews, its stock and its sibling "
        "variants without composing a single URL yourself.",
        "",
        "```json",
        '{"_links": {',
        f'  "self":     "{api["agent_base_url"]}/v2/product-details/{conventions["sample_asin"]}/US",',
        f'  "reviews":  "{api["agent_base_url"]}/v2/product-reviews/{conventions["sample_asin"]}/US",',
        f'  "stock":    "{api["agent_base_url"]}/stock/{conventions["sample_asin"]}/US",',
        '  "variations": ["...20 sibling ASINs..."],',
        '  "variations_total": 725',
        "}}",
        "```",
        "",
        "`_links` also carries `parent` when the listing has a parent ASIN -- "
        "often the next thing you want, and until an agent pointed it out, "
        "the one ASIN in neither the variation map nor the links.",
        "",
        "`variations` is capped at 100, which covers most listing families "
        "outright. When one is larger the object carries "
        "`variations_truncated: true` and a note giving both counts and how "
        "to build the rest: an array quietly shorter than its own "
        "`variations_total` is exactly what `_truncated` exists to prevent "
        "on the list endpoints. `_links` is omitted when you pass `fields=`, "
        "since you asked for named keys.",
        "",
        "**A markdown link is not a URL.** If someone hands you "
        "`[https://.../B09NLCNGC7/US](https://.../B0014C0LUC/US)`, an "
        "allowlist fetcher sees only the target -- the second one -- and the "
        "text you were reading is not fetchable. Anyone passing a working "
        "call to an agent should paste it as plain text, not as a link.",
        "",
        "Rejected requests cost nothing: a 400 does not consume a free call.",
        "",
        "Every error body carries `code`, `http_status`, `billed`, `retryable` "
        "and `next_step`, plus `free_calls_remaining` and `price_next_call`. "
        "If your client hides status codes and headers -- many do -- read "
        "those from the body. A `code` of `missing_parameter` is your bug; "
        "`payment_required` means the free allowance is spent and the request "
        "itself was fine.",
        "",
        "## If the user gave you an Apiguru API key",
        "",
        "Send it as `X-API-KEY` on these same URLs. There is no separate base "
        "URL, no wallet and no x402 handshake on this path -- the call is "
        "billed to that account, newest free trial calls first, and the user "
        "sees it in their dashboard.",
        "",
        "```bash",
        "curl -H 'X-API-KEY: THE_KEY_THE_USER_GAVE_YOU' \\",
        f"  '{api['agent_base_url']}/v2/product-details?asin={conventions['sample_asin']}&geo=US'",
        "```",
        "",
        "A key is 401 if it is wrong and 402 if the account is out of credit; "
        "neither is retryable. Never print the key back to the user, never "
        "write it into a file the user did not ask for, and never put it in a "
        "URL query string where it lands in shell history and server logs -- "
        f"the header is the only supported place. Keys come from {api['base_url'].rsplit('/api', 1)[0]} "
        "and also work on the keyed REST base below.",
        "",
        "## What it costs and how you pay",
        "",
        f"- **{free['probes_per_client']} free calls** per client per "
        f"{free['window_hours']}h, so you can check this API fits your task "
        "before spending anything.",
        f"- After that: HTTP **402** with a `PAYMENT-REQUIRED` header stating "
        "exactly what to pay. Any x402-capable HTTP client settles it and "
        "retries automatically.",
        f"- Paid in **{agent_auth['asset']}** on **{agent_auth['network']}**.",
        "- Watch `X-Free-Probes-Remaining` and `X-Price-Next-Call` on every "
        "response to know where you stand before you get a 402.",
        "",
        f"Free probes are counted by client IP. {free['counted_by']}",
        "",
        "## Paying",
        "",
        "You need two things: an x402 client library, and an EVM wallet key whose "
        f"address holds a little **{agent_auth['asset']}** on "
        f"**{agent_auth['network']}**. Nothing else: no account here, no ETH for "
        "gas (the facilitator submits the transfer), no minimum deposit. One call "
        "costs about a cent; `X-Price-Next-Call` on every response is the exact "
        "figure.",
        "",
        "How a paid call works: your first request gets `402` with a "
        "`PAYMENT-REQUIRED` header (base64 JSON: the `accepts` list names the "
        "scheme `exact`, the network, the asset, the receiving address and the "
        "amount). The client signs an EIP-3009 authorization for that amount and "
        "retries the same request with a `PAYMENT-SIGNATURE` header "
        "(`X-PAYMENT`, the v1 name, is accepted too). The gateway verifies the "
        "signature, serves the request, and settles on-chain only after a "
        "billable answer (2xx, or the 404 that means the item does not exist). "
        "If the failure was ours you see `X-Payment-Status: not-settled` and pay "
        "nothing. The libraries below do all of this on a plain 402; you just "
        "wrap your HTTP client.",
        "",
        "Python (`pip install \"x402[evm,httpx]\" eth-account`):",
        "",
        "```python",
        "from eth_account import Account",
        "from x402 import x402Client",
        "from x402.http.clients import x402HttpxClient",
        "from x402.mechanisms.evm import EthAccountSigner",
        "from x402.mechanisms.evm.exact.register import register_exact_evm_client",
        "",
        "client = x402Client().set_spend_controls({\"max_amount_per_payment\": \"$1\"})",
        "register_exact_evm_client(client, EthAccountSigner(Account.from_key(PRIVATE_KEY)))",
        "async with x402HttpxClient(client) as http:",
        f"    r = await http.get(\"{api['agent_base_url']}/v2/product-details\", params={{\"asin\": \"{conventions['sample_asin']}\"}})",
        "    print(r.status_code, r.headers.get(\"X-Payment-Status\"), r.json()[\"data\"][\"product_title\"])",
        "```",
        "",
        "TypeScript / Node (`npm i @x402/fetch @x402/evm viem`):",
        "",
        "```ts",
        "import { x402Client, wrapFetchWithPayment } from \"@x402/fetch\";",
        "import { ExactEvmScheme } from \"@x402/evm/exact/client\";",
        "import { privateKeyToAccount } from \"viem/accounts\";",
        "",
        "const client = new x402Client();",
        "client.setSpendControls({ maxAmountPerPayment: \"$1\" });",
        "client.register(\"eip155:*\", new ExactEvmScheme(privateKeyToAccount(PRIVATE_KEY)));",
        "const fetchWithPayment = wrapFetchWithPayment(fetch, client);",
        f"const r = await fetchWithPayment(\"{api['agent_base_url']}/v2/product-details?asin={conventions['sample_asin']}\");",
        "```",
        "",
        "Keep the spend control: it caps what one 402 can take from the wallet. "
        "Batch endpoints bill per item (up to 20 items, so up to $0.16 in one "
        "call); check `X-Price-Next-Call` or `/.well-known/x402` before raising "
        "the cap.",
        "",
        "No wallet and no way to get one? An API key from "
        "https://dash.apiguru.app/register comes with free trial calls and bills "
        "a normal account instead; MCP clients such as claude.ai and ChatGPT sign "
        "in through https://mcp.apiguru.app/account and never touch x402.",
        "",
        "## Free forever, never metered",
        "",
        "Use these to plan a job at zero cost before committing to a paid call:",
        "",
    ] + [
        f"- `{api['agent_base_url'].rsplit('/agent/v1', 1)[0]}{path}`"
        for path in api["free_endpoints"]
    ] + [
        "",
        "`/.well-known/x402` lists every endpoint with its price and JSON "
        "Schema, so you can decide what to call and what it will cost without "
        "spending a probe. The MCP server exposes the same thing as a free "
        "`list_capabilities` tool that answers locally with no network call.",
        "",
        "## Interfaces",
        "",
        f"- [MCP server]({api['mcp_url']}): streamable HTTP, keyless; also installable locally (`uvx apiguru-mcp`).",
        f"- [MCP server, signed in]({api['mcp_account_url']}): OAuth 2.1 for claude.ai, Claude Desktop and ChatGPT connectors; bills your Apiguru account.",
        f"- [OpenAPI spec]({api['openapi_url']}): full machine-readable schema for all {len(spec['endpoints'])} endpoints.",
        f"- [Keyed REST API]({api['base_url']}): for existing customers, `X-API-KEY` header.",
        f"- [Docs]({api['docs_url']}): human documentation.",
        "",
        "## Endpoints",
        "",
    ]

    for ep in spec["endpoints"]:
        required = ep["input_schema"].get("required", [])
        req = ", ".join(f"`{r}`" for r in required) if required else "none"
        lines.append(
            f"- **`GET {ep['path']}`** — {ep['summary']}. "
            f"Required: {req}. Price: {price_label(ep)}."
        )

    lines += [
        "",
        "## Reading list results without drowning in them",
        "",
        "A full page from `/search` is up to 48 results and about 54 KB of "
        "JSON. Over MCP the list tools (`search`, `best_sellers`, `deals`, "
        "`seller_products`, `seller_reviews`) answer with the first **10 rows "
        "as light records** by default, which is around 7 KB and fits inline. "
        "Three ways to change that:",
        "",
        "- `limit=N` - how many rows (`limit=0` for the whole page).",
        "- `compact=false` - every field the REST API sends, including the "
        "full delivery text.",
        "- `fields=\"asin,product_title,product_price\"` - only the keys you "
        "name.",
        "",
        "Answers carry `_truncated` (how many rows the page really had) and "
        "`_omitted_fields` (what the light projection dropped), so nothing is "
        "hidden. Over plain REST filter with `brand`, `min_price`/`max_price` "
        "and `sort_by` to keep a page small, which also makes it more "
        "relevant than paging does.",
        "",
        "### One product is the big one, not the list",
        "",
        "`/v2/product-details` returns about 135 KB for a listing with many "
        "variations, and `fields=` works there too -- over REST as well as "
        "MCP, which it did not until an agent pointed out that the only "
        "documented way to slim a response was for list tools:",
        "",
        f"    {api['agent_base_url']}/v2/product-details"
        f"?asin={conventions['sample_asin']}&fields=product_title,product_price,customers_say",
        "",
        "Measured on the ASIN above: 2,223 bytes against 135,923 for the "
        "whole record, a 61x reduction. It works on `/v2/product-reviews` "
        "too. Names you ask for that do not exist come back in "
        "`_unknown_fields` rather than being silently dropped, and "
        "`_omitted_fields` lists what was left out. `asin` is always "
        "included so a response can be matched back to its request.",
        "",
        "**If you cannot send a query string**, you do not need `fields=` as "
        "much as you might think: the record is ordered with the useful "
        "fields first. `asin`, title, price, star rating, rating count, "
        "`customers_say` and `badges` are inside the first 400 bytes; the "
        "bulk arrays (`all_product_variations`, `product_reviews`, image "
        "lists, `tech_specs`) come last. Truncating the response is "
        "therefore safe -- you lose the bulk, not the summary. It used to be "
        "alphabetical, which put 53 KB of variations ahead of the 1 KB "
        "review summary.",
        "",
        "**Fields that repeat each other.** `product_information`, "
        "`tech_specs` and `product_details` are three views of the same "
        "specification table, and `product_photos`, `product_images` and "
        "`images_meta` overlap the same way. They are all passed through as "
        "Amazon presents them rather than merged, because different callers "
        "depend on different ones. Pick one and name it in `fields=`.",
        "",
        "## Three things about Amazon's data that mislead",
        "",
        "- **Reviews pool across the listing family, counts AND text.** Every "
        "colour of one shoe reports the same `product_num_ratings`, so it is "
        "not a per-variant figure. The same is true of the review bodies "
        "returned by `/v2/product-reviews`: a query for a black Classic Clog "
        "can come back with a review of a different colour, or of the Bistro "
        "model altogether, because Amazon shows them on that listing. Do not "
        "attribute a complaint to the exact variant you asked about without "
        "checking the review text for a colour or model mentioned in it.",
        "- **A variant's title can be the parent's.** In search results the "
        "ASIN and the title can disagree; the URL slug usually shows the real "
        "variant. When the exact variant matters, call `/v2/product-details` "
        "on that ASIN - it is authoritative for the ASIN you passed.",
        "- **Amazon's Choice is awarded per search term, not per product.** "
        "This is the one that looks most like a bug in this API and is not. "
        "`/search` for `crocs black` returns B0014C0LUC with "
        "`badges: [\"Overall Pick\"]` and `is_amazon_choice: true`, while "
        "`/v2/product-details` for the same ASIN, seconds later, returns "
        "`amazon_choice: false`. Both are correct. We captured the live "
        "product page to check: it contains no badge markup at all. The badge "
        "belongs to the pair (product, query), so it exists in a result list "
        "and not on the product itself. Read it from the `/search` row that "
        "carried it, and record the query alongside it -- \"Amazon's Choice\" "
        "with no query attached does not mean anything.",
        "",
        "- **`/product` and `/v2/product-details` read different Amazon "
        "surfaces, and some fields differ because of it.** The batch endpoint "
        "reads Amazon's mobile API, the single one reads the product page. "
        "For B0014C0LUC the batch reports `Date First Available: September 1, "
        "2023` (mobile field `site_launch_date`) and the detail page reports "
        "`April 3, 2025`. We checked the page: it says April 3, 2025 and "
        "contains no mention of 2023, so both are faithful readings of their "
        "own source rather than a parsing fault. Use `/v2/product-details` "
        "when you want what a person sees on the listing. If a date matters "
        "to your decision, say which endpoint it came from. "
        "**Do not read `Date First Available` as the product's age at all**: "
        "it tracks the listing record, which Amazon re-dates. Seen twice -- "
        "B0014C0LUC shows April 3, 2025 against 488,443 ratings, B008YA0Z44 "
        "shows October 1, 2025 against 50,879. Neither count can accumulate "
        "in that window. We checked the page for the first and it carries "
        "the date we return, so this is Amazon's value, not a parse fault. "
        "For how established a product is, use `product_num_ratings` and the "
        "best-seller rank.",
        "",
        "- **Refurbished listings report their real condition.** This was "
        "briefly wrong and is fixed: B0G4RV4F71, titled \"... (Renewed "
        "Premium)\", returned `condition: \"new\"`. Its buy box reads "
        "\"Refurbished - Premium\", so that was our bug, not a value from "
        "Amazon. It now returns `condition: \"refurbished - premium\"` with "
        "`buybox_winner.condition.is_new: false`. The correction only ever "
        "moves a condition away from new and only on evidence from the page, "
        "never the reverse -- calling a refurbished item new is the "
        "expensive direction of this mistake. Cross-check "
        "`product_title` for \"Renewed\" or \"Refurbished\" anyway if the "
        "distinction matters to you.",
        "",
        "`badges` is the source of truth for Amazon's Choice / Best Seller / "
        "Overall Pick (Amazon renamed that slot). `is_amazon_choice` and "
        "`is_best_seller` are conveniences derived from it. Note the shape "
        "differs by endpoint for historical reasons: `/search` rows carry a "
        "list of label strings, `/v2/product-details` carries an object of "
        "booleans. Check the type before indexing it.",
        "",
        "`customers_say` is Amazon's AI summary of the reviews, with per-aspect "
        "sentiment and mention counts. It is null when Amazon shows no summary "
        "for that listing, which is common on low-review products -- a null "
        "there is an answer, not a failure.",
        "",
        "## Conventions",
        "",
        f"- **Marketplaces** ({len(spec['geos'])}): {geos}. Pass as `geo`, chosen from the "
        f"user's request or the Amazon domain they mention (amazon.de -> DE). The API assumes "
        f"`{conventions['geo_default']}` only when the parameter is omitted; do not rely on that.",
        f"- **ASIN format**: `{conventions['asin_pattern']}`. {conventions['asin_note']}",
        f"- **Seller ID format**: `{conventions['seller_id_pattern']}`.",
        f"- **Sample ASIN for testing**: `{conventions['sample_asin']}`.",
        "",
        "## Error semantics",
        "",
        "These matter for cost control — some errors bill and some do not:",
        "",
    ]

    for code, text in conventions["error_semantics"].items():
        lines.append(f"- **{code}** — {text}")

    fb = api.get("feedback")
    if fb:
        lines += [
            "",
            f"**Retry policy:** {conventions['retry_policy']}",
            "",
            "## Telling us what is broken",
            "",
            "This API has been fixed more than once because an agent said what "
            "was wrong with it. If a field is empty, mistyped, welded together "
            "or simply missing, say so:",
            "",
            f"- **Preferred - GitHub issues:** {fb['github_issues']}. An issue "
            "can hold a conversation; you get a reply on the thread.",
            f"- **No GitHub account? The wall:** `POST {fb['wall_post']}` with "
            "`{\"message\": \"...\", \"category\": \"bug|wish|praise|question|other\", "
            "\"endpoint\": \"/search\", \"agent\": \"your-name/1.0\"}`. No key, no "
            "signup, never billed.",
            f"- **Over MCP:** the free `{fb['mcp_tool']}` tool does the same thing.",
            f"- **Read what others wrote:** {fb['wall_read']}",
            "",
        ]
    else:
        lines += [
            "",
            f"**Retry policy:** {conventions['retry_policy']}",
            "",
        ]

    return "\n".join(lines)


# --------------------------------------------------------------------------
# skill reference docs
# --------------------------------------------------------------------------

def build_endpoints_md(spec):
    api = spec["api"]
    conv = spec["conventions"]

    lines = [
        "# Apiguru endpoint reference",
        "",
        "Generated from the API spec - do not edit by hand.",
        "",
        f"- Keyless base URL: `{api['agent_base_url']}`",
        f"- Keyed base URL: `{api['base_url']}` (send `X-API-KEY`)",
        "",
        "All endpoints are `GET` with query parameters.",
        "",
        "## Marketplaces",
        "",
        "Pass as `geo`, chosen from the user's request or the Amazon domain they mention "
        "(amazon.de -> DE). The API assumes `US` only when the parameter is omitted; do not "
        "rely on that default.",
        "",
        "| Code | Domain |",
        "|---|---|",
    ]
    lines += [f"| `{code}` | amazon.{domain} |" for code, domain in spec["geos"].items()]

    for ep in spec["endpoints"]:
        schema = ep["input_schema"]
        required = schema.get("required", [])
        lines += [
            "",
            f"## `GET {ep['path']}`",
            "",
            ep["description"],
            "",
            f"**Price:** {price_label(ep)}",
            "",
            "| Parameter | Type | Required | Notes |",
            "|---|---|---|---|",
        ]
        for name, prop in schema.get("properties", {}).items():
            kind = prop.get("type", "string")
            if "enum" in prop:
                kind = "enum"
            note = prop.get("description", "")
            if "default" in prop:
                note = f"{note} Default `{prop['default']}`."
            lines.append(
                f"| `{name}` | {kind} | {'yes' if name in required else 'no'} | {note} |"
            )
        if "enum" in str(schema):
            for name, prop in schema.get("properties", {}).items():
                if "enum" in prop and name != "geo":
                    values = ", ".join(f"`{v}`" for v in prop["enum"])
                    lines += ["", f"`{name}` accepts: {values}"]
        if ep.get("notes"):
            lines += ["", f"> {ep['notes']}"]

    lines += [
        "",
        "## Formats",
        "",
        f"- ASIN: `{conv['asin_pattern']}` - {conv['asin_note']}",
        f"- Seller ID: `{conv['seller_id_pattern']}`",
        f"- Sample ASIN for testing: `{conv['sample_asin']}`",
        "",
    ]
    return "\n".join(lines)


def build_errors_md(spec):
    conv = spec["conventions"]
    lines = [
        "# Costs, billing and retries",
        "",
        "Generated from the API spec - do not edit by hand.",
        "",
        "## Prices",
        "",
        "| Endpoint | Price |",
        "|---|---|",
    ]
    lines += [f"| `{ep['path']}` | {price_label(ep)} |" for ep in spec["endpoints"]]

    lines += [
        "",
        "Batch endpoints are billed per item and are cheaper per item than the",
        "single-item equivalents. Always prefer them for more than one item.",
        "",
        "## What each status means, and whether it costs money",
        "",
        "| Status | Billed? | Meaning and what to do |",
        "|---|---|---|",
    ]
    for code, text in conv["error_semantics"].items():
        billed = "**yes**" if "BILLED" in text else ("no" if "NOT billed" in text else "-")
        lines.append(f"| `{code}` | {billed} | {text} |")

    lines += [
        "",
        "## Retry policy",
        "",
        conv["retry_policy"],
        "",
        "Concretely:",
        "",
        "```python",
        "for attempt in range(4):",
        "    status, body = call(...)",
        "    if status in (503, 429):",
        "        time.sleep(2 ** attempt)   # transient, not billed",
        "        continue",
        "    break                          # 200/400/404 are final",
        "```",
        "",
        "## Free probes",
        "",
        "The keyless gateway serves a few free requests per IP per rolling",
        "window before it starts charging. Response header",
        "`X-Free-Probes-Remaining` tells you how many are left, and",
        "`X-Price-Next-Call` what the next one will cost.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------

def purge_bytecode(*roots):
    """Delete __pycache__ / .pyc under the skill trees.

    ClawHub's scanner (and any careful user) treats shipped Python bytecode as
    a supply-chain red flag: bytecode is skipped by content analysis, so a
    malicious .pyc could hide behind clean .py sources. Running the tests or
    the probe script locally creates __pycache__ next to probe.py; this
    removes it every time the kit is regenerated, before anything is
    published.
    """
    import shutil
    for root in roots:
        for path in pathlib.Path(root).rglob("__pycache__"):
            shutil.rmtree(path, ignore_errors=True)
        for path in pathlib.Path(root).rglob("*.py[co]"):
            path.unlink(missing_ok=True)


def sync_versions(spec):
    """One version number, `api.version` in endpoints.json, written into every
    manifest that carries one. Editing any other copy is a mistake."""
    version = spec["api"]["version"]
    root = SPEC_DIR.parent

    def rewrite(path, pattern, replacement, count=0):
        p = root / path
        if not p.exists():
            return
        text = p.read_text(encoding="utf-8")
        new, n = re.subn(pattern, replacement, text, count=count, flags=re.M)
        if n and new != text:
            p.write_text(new, encoding="utf-8")
            print(f"version {version} -> {path}")

    rewrite("mcp/pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"', 1)
    rewrite("mcp/apiguru_mcp/__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{version}"', 1)
    rewrite("plugin/apiguru/.claude-plugin/plugin.json", r'"version": "[^"]+"', f'"version": "{version}"')
    rewrite(".claude-plugin/marketplace.json", r'"version": "[^"]+"', f'"version": "{version}"')
    rewrite("gemini-extension.json", r'"version": "[^"]+"', f'"version": "{version}"')
    rewrite("npm/package.json", r'^  "version": "[^"]+"', f'  "version": "{version}"', 1)

    # The skill pins the MCP package version in its launcher snippets (an
    # unpinned `uvx apiguru-mcp` runs code that was not in the reviewed
    # artifact). Keep the pin on this version so it cannot drift.
    rewrite(
        "skill/apiguru-amazon-data/SKILL.md",
        r'"apiguru-mcp==[0-9]+\.[0-9]+\.[0-9]+"',
        f'"apiguru-mcp=={version}"',
    )
    rewrite(
        "skill/apiguru-amazon-data/SKILL.md",
        r'"apiguru-mcp@[0-9]+\.[0-9]+\.[0-9]+"',
        f'"apiguru-mcp@{version}"',
    )

    server_json = root / "mcp" / "server.json"
    if server_json.exists():
        doc = json.loads(server_json.read_text(encoding="utf-8"))
        changed = doc.get("version") != version
        doc["version"] = version
        for pkg in doc.get("packages", []):
            changed = changed or pkg.get("version") != version
            pkg["version"] = version
        if changed:
            server_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"version {version} -> mcp/server.json")


def main():
    spec = load()
    sync_versions(spec)
    openapi = build_openapi(spec)

    (SPEC_DIR / "openapi.json").write_text(
        json.dumps(openapi, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (SPEC_DIR / "openapi.yaml").write_text(
        yaml.safe_dump(openapi, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    (SPEC_DIR / "llms.txt").write_text(build_llms_txt(spec), encoding="utf-8")

    # Ship a copy inside the MCP package so `pip install apiguru-mcp` is
    # self-contained. Written from here rather than copied by hand so the
    # packaged spec can never fall behind endpoints.json.
    payload = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"

    consumers = [
        SPEC_DIR.parent / "mcp" / "apiguru_mcp" / "data" / "endpoints.json",
        SPEC_DIR.parent / "gateway" / "endpoints.json",
    ]
    for target in consumers:
        if target.parent.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            print(f"synced spec into {target}")

    # The gateway serves this verbatim at /llms.txt, so it must ship with the
    # code rather than being rebuilt (and drifting) inside the app.
    gateway_llms = SPEC_DIR.parent / "gateway" / "llms.txt"
    if gateway_llms.parent.exists():
        gateway_llms.write_text(build_llms_txt(spec), encoding="utf-8")
        print(f"synced llms.txt into {gateway_llms}")

    # The apex site (apiguru.app) is a separate Cloudflare Pages deploy, so
    # its copies live in the landing source and ship on the next Pages build.
    landing = SPEC_DIR.parent.parent / "docker-setup" / "frontend" / "landing-apiguru"
    if landing.exists():
        (landing / "llms.txt").write_text(build_llms_txt(spec), encoding="utf-8")
        (landing / "openapi.json").write_text(
            json.dumps(openapi, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"synced llms.txt + openapi.json into {landing} (Cloudflare Pages)")

    refs = SPEC_DIR.parent / "skill" / "apiguru-amazon-data" / "references"
    if refs.parent.exists():
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "endpoints.md").write_text(build_endpoints_md(spec), encoding="utf-8")
        (refs / "errors-and-costs.md").write_text(build_errors_md(spec), encoding="utf-8")
        print(f"wrote skill references into {refs}")

    # The Claude Code plugin must carry the skill INSIDE its own directory:
    # plugin manifests may not reference paths outside the plugin root, and
    _kit = Path(__file__).resolve().parent.parent
    purge_bytecode(_kit / "skill", _kit / "plugin")
    # only ./plugin/apiguru is fetched from the marketplace. Mirror the
    # canonical skill there rather than maintaining two copies by hand.
    skill_src = SPEC_DIR.parent / "skill" / "apiguru-amazon-data"
    plugin_skills = SPEC_DIR.parent / "plugin" / "apiguru" / "skills"
    if skill_src.exists() and plugin_skills.parent.exists():
        dest = plugin_skills / skill_src.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            skill_src, dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        print(f"mirrored skill into {dest}")

    n_paths = len(openapi["paths"])
    print(f"endpoints.json -> {len(spec['endpoints'])} endpoints, {n_paths} paths")
    print("wrote openapi.json, openapi.yaml, llms.txt")


if __name__ == "__main__":
    main()
