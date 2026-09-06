# Apiguru MCP Server

Live Amazon marketplace data for AI agents — product details, reviews, keyword
search, best-sellers, deals, live offers and stock, and seller profiles across
20 country marketplaces.

<!-- mcp-name: app.apiguru/amazon-data -->

**No API key required.** Unkeyed callers get a small free probe budget, then an
HTTP 402 payment challenge payable autonomously via x402 (USDC on Base
mainnet). Bring an API key if you already have one and calls bill to your
account instead.

## Install

No install step is required: `uvx` fetches `apiguru-mcp` from PyPI and caches
it on first launch. (`pip install apiguru-mcp` works too, and so does running
straight from this repository: `uvx --from "git+https://github.com/apiguru-app/agent-kit#subdirectory=mcp" apiguru-mcp`.)

```bash
uvx apiguru-mcp
```

No Python or uv on the machine? `npx -y apiguru-mcp` (Node 18+) runs a stdio
bridge to the hosted server with the same 11 tools; see `../npm/README.md`.

### Claude Code

```bash
# remote (nothing to run locally):
claude mcp add --transport http apiguru https://mcp.apiguru.app/mcp
# or the plugin, which also carries the skill:
/plugin marketplace add apiguru-app/agent-kit
/plugin install apiguru@apiguru
```

### Codex CLI

```bash
codex mcp add apiguru --url https://mcp.apiguru.app/mcp
# or locally:
codex mcp add apiguru -- uvx apiguru-mcp
```

### Hermes Agent / OpenClaw / any stdio client

```json
{
  "mcpServers": {
    "apiguru": {
      "command": "uvx",
      "args": ["apiguru-mcp"],
      "env": { "APIGURU_API_KEY": "optional-key-here" }
    }
  }
}
```

Drop the `env` block entirely to run keyless.

### claude.ai, Claude Desktop connectors, ChatGPT connectors

```
https://mcp.apiguru.app/account
```

Add it as a custom connector. The client discovers the OAuth 2.1 authorization
server, registers itself, and sends you to a sign-in page where you use your
Apiguru dashboard e-mail and password or paste an API key (Google sign-in
accounts use the key). Every call then bills your account at your plan's
rates. Disconnect from the client to revoke.

These clients call from their vendor's shared addresses and cannot send
headers, which is why the keyless endpoint below is the wrong choice for them:
all of their users would share one free-probe budget.

### Remote, keyless (Claude Code, Codex, Hermes, any agent that can send headers)

```
https://mcp.apiguru.app/mcp
```

Streamable HTTP. Send `X-API-KEY` if you have one; omit it to go keyless.

## Tools

| Tool | Does | Price |
|---|---|---|
| `list_capabilities` | Endpoints, prices, marketplaces | **free**, no network call |
| `product_details` | Full record for one ASIN | $0.01 |
| `product_details_batch` | Up to 20 ASINs at once | $0.008/item |
| `product_reviews` | Reviews, rating, "customers say" | $0.01 |
| `search` | Keyword search with filters and sorting | $0.01 |
| `offers_stock` | Offers, buy box, live stock (≤10 ASINs) | $0.015/item |
| `best_sellers` | Category rankings | $0.01 |
| `deals` | Current discounts with filters | $0.01 |
| `seller_profile_batch` | Seller profiles (≤10 IDs) | $0.012/item |
| `seller_products` | A seller's catalogue | $0.01 |
| `seller_reviews` | Seller feedback | $0.01 |

Prefer the batch tools over looping the single-item ones — they are cheaper per
item and one round trip instead of N.

## Error semantics worth knowing

Some errors cost money and some do not:

- **404** — the item genuinely isn't on that marketplace. **Billed.** Retrying
  won't help; try a different `geo`.
- **503** — an Apiguru-side fetch failure. **Not billed.** Retry. On the
  keyless path a free probe spent on a 503 is handed back, and a signed x402
  payment is left unsettled (`X-Payment-Status: not-settled`).
- **429** — rate limit. Back off and retry.
- **413** — too many items in a batch (20 ASINs for `product_details_batch`,
  10 for `offers_stock` and `seller_profile_batch`). Not billed; split the list.
- **400** — bad input (ASINs must be 10 **uppercase** alphanumeric chars).
  Not billed.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APIGURU_API_KEY` | unset | Use the keyed API instead of the keyless gateway |
| `APIGURU_BASE_URL` | from spec | Override the API base (self-hosted/staging) |
| `APIGURU_MCP_ALLOWED_HOSTS` | `mcp.apiguru.app,localhost,127.0.0.1` | Hosts the HTTP transport accepts. Must include the public hostname when running behind a reverse proxy, or every request is rejected with 421. |
| `APIGURU_MCP_ALLOWED_ORIGINS` | derived | CORS/DNS-rebinding origin allowlist |
| `APIGURU_MCP_STATELESS` | `true` | Stateless streamable HTTP. Keep it on: in stateful mode the SDK snapshots each session's request context at `initialize`, so a key sent on a later request would be ignored. |
| `APIGURU_AGENT_INTERNAL_URL` | unset | Hosted deployments only: the gateway's address on the private network. With `AGENT_INTERNAL_TOKEN` set, keyless calls go there and carry the real caller's address so free probes are keyed per caller, not per MCP container. |
| `APIGURU_OAUTH_ENABLED` | `false` | Hosted deployments only: mount the OAuth 2.1 endpoint at `/account` (needs `DATABASE_URL`; tokens are stored hashed in `mcp_oauth_*` tables). |
| `APIGURU_MCP_PUBLIC_URL` | from spec | The OAuth issuer, e.g. `https://mcp.apiguru.app`. |
| `APIGURU_API_INTERNAL_URL` | unset | Hosted deployments only: the backend on the private network, used for keyed calls and for proxying password logins to its `/login`. |
| `APIGURU_OAUTH_ACCESS_TTL` / `APIGURU_OAUTH_REFRESH_TTL` | 1 day / 30 days | Token lifetimes in seconds. Refresh tokens rotate. |

## Running the HTTP server yourself

```bash
pip install "apiguru-mcp[http]"
apiguru-mcp --http --host 0.0.0.0 --port 8790
```

Also serves `/health`, `/llms.txt` and `/spec.json` unauthenticated.

## Development

Tools are generated from `apiguru_mcp/data/endpoints.json`, which is written by
`agent-kit/spec/generate.py` from the canonical spec. To add or change an
endpoint, edit `agent-kit/spec/endpoints.json` and re-run the generator — the
MCP tools, the OpenAPI document and `llms.txt` all update together.

```bash
python agent-kit/spec/generate.py
```

## Links

- API docs: https://dash.apiguru.app/docs
- OpenAPI spec: https://dash.apiguru.app/api/v1/openapi.json
- Get an API key: https://dash.apiguru.app/register
