# apiguru-mcp (npm)

Live Amazon marketplace data for AI agents as MCP tools: product details,
reviews, keyword search, best-sellers, deals, live offers and stock, and seller
profiles across 20 country marketplaces. Eleven read-only tools with prices and
retry rules in their descriptions.

```bash
npx -y apiguru-mcp
```

That is the whole install. Node 18+ is the only requirement: no Python, no
`uv`, no account, no API key.

<!-- mcp-name: app.apiguru/amazon-data -->

## What it runs

`npx apiguru-mcp` is a stdio bridge to the hosted server at
`https://mcp.apiguru.app/mcp`: your MCP client talks stdio to the bridge, the
bridge talks streamable HTTP to the server, and every JSON-RPC message passes
through untouched. You get exactly the tools `uvx apiguru-mcp` (the Python
package) serves locally, because that package is itself a thin HTTP client over
the same gateway.

Free probes are counted per caller, since the connection to the gateway comes
from your machine rather than from a shared vendor address.

Prefer to run the server itself? `npx apiguru-mcp --local` runs the Python
package through `uvx` (needs [uv](https://docs.astral.sh/uv/)).

## Client setup

**Claude Code**

```bash
claude mcp add apiguru -- npx -y apiguru-mcp
# or remote, nothing to run locally:
claude mcp add --transport http apiguru https://mcp.apiguru.app/mcp
```

**Codex CLI**

```bash
codex mcp add apiguru -- npx -y apiguru-mcp
```

**Cursor, Windsurf, Claude Desktop, Hermes, OpenClaw, any stdio client**

```json
{
  "mcpServers": {
    "apiguru": {
      "command": "npx",
      "args": ["-y", "apiguru-mcp"],
      "env": { "APIGURU_API_KEY": "optional-key-here" }
    }
  }
}
```

Drop the `env` block to run keyless.

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "apiguru": { "type": "stdio", "command": "npx", "args": ["-y", "apiguru-mcp"] }
  }
}
```

**claude.ai, Claude Desktop connectors, ChatGPT**: no bridge needed. Add
`https://mcp.apiguru.app/account` as a connector and sign in; calls bill your
Apiguru account.

## Paying for calls

Without a key you get 3 free calls per 24 hours per machine. After that a tool
call returns a structured error carrying an HTTP 402 payment challenge (x402,
USDC on Base mainnet). Either:

- set `APIGURU_API_KEY` from https://dash.apiguru.app so calls bill your
  account at your plan's rates, or
- have an x402-capable HTTP client pay the challenge against the REST gateway
  at `https://agent.apiguru.app/agent/v1/...` directly. The full guide is in
  `https://agent.apiguru.app/llms.txt`.

## Options

| Flag / variable | Meaning |
|---|---|
| `--url <url>` / `APIGURU_MCP_URL` | Bridge to another streamable-HTTP MCP server (default `https://mcp.apiguru.app/mcp`) |
| `--local [args...]` | Run the Python server via `uvx apiguru-mcp` instead; remaining arguments are passed through |
| `APIGURU_API_KEY` | Sent as `X-API-KEY`; bills that Apiguru account instead of using the keyless path |
| `APIGURU_MCP_DEBUG` | Log transport noise to stderr |
| `--version`, `--help` | |

## Tools

| Tool | Does | Price |
|---|---|---|
| `list_capabilities` | Endpoints, prices, marketplaces, your access mode | **free**, offline |
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

Every tool is annotated `readOnlyHint: true`; nothing here writes anywhere.

## Links

- Source, skill and plugin: https://github.com/apiguru-app/agent-kit
- Python package: https://pypi.org/project/apiguru-mcp/
- API docs: https://dash.apiguru.app/docs
- Support: support@apiguru.app
