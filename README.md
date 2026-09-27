# Apiguru Agent Kit

Everything needed for an AI agent to discover, call and pay for the Apiguru
Amazon Data API — **with no account, no API key and no subscription**.

The existing backend is not modified by any of this. Not one line.

## Install in your agent

| Client | How |
|---|---|
| Claude Code | `claude mcp add --transport http apiguru https://mcp.apiguru.app/mcp` or `/plugin marketplace add apiguru-app/agent-kit` then `/plugin install apiguru@apiguru` |
| claude.ai, Claude Desktop, ChatGPT | add connector `https://mcp.apiguru.app/account` and sign in |
| Codex CLI | `codex mcp add apiguru --url https://mcp.apiguru.app/mcp` |
| Cursor | [Add to Cursor](cursor://anysphere.cursor-deeplink/mcp/install?name=apiguru&config=eyJjb21tYW5kIjogIm5weCIsICJhcmdzIjogWyIteSIsICJhcGlndXJ1LW1jcCJdfQ==) (runs `npx -y apiguru-mcp`) |
| VS Code | [Install in VS Code](vscode:mcp/install?%7B%22name%22%3A%20%22apiguru%22%2C%20%22command%22%3A%20%22npx%22%2C%20%22args%22%3A%20%5B%22-y%22%2C%20%22apiguru-mcp%22%5D%7D) |
| Gemini CLI | `gemini extensions install https://github.com/apiguru-app/agent-kit` |
| Hermes, OpenClaw, any stdio client | `npx -y apiguru-mcp` (npm, Node only) or `uvx apiguru-mcp` (PyPI) |
| Skill only (any agent) | `npx skills add apiguru-app/agent-kit` or ClawHub `apiguru-amazon-data` |

## Why this exists

The API is gated behind register → verify email → get key → top up. Every step
assumes a human. Agents cannot do any of it, so agent traffic bounces off the
front door.

And agents don't browse — they query indexes: the x402 Bazaar, the MCP
Registry, ClawHub. Apiguru was in none of them, and the repo had no OpenAPI
spec at all, so there was nothing machine-readable to publish.

## Telling us what is broken

Agents are the ones who find the defects in this API, so there are two ways
back to us and neither costs anything:

- **GitHub issues — preferred:**
  <https://github.com/apiguru-app/agent-kit/issues>. A thread can hold a
  reply, and a fix gets linked back to it.
- **The wall, for callers with no GitHub account:** one unauthenticated
  `POST https://dash.apiguru.app/api/v1/feedback` with
  `{"message": "...", "category": "bug|wish|praise|question|other",
  "endpoint": "/search", "agent": "your-name/1.0", "contact": "optional"}`.
  No key, no signup, never billed. Read it at
  <https://dash.apiguru.app/feedback>, or `GET` the same URL for JSON.
- Over MCP: the free `send_feedback` tool. From the skill:
  `python scripts/probe.py feedback --message "..." --category bug`.

This is not decoration. 1.1.3 exists because an agent reported that search
returned the brand as `product_title`, `null` for every
`product_num_ratings`, and delivery text with the words welded together.


## What is in this repository

| Path | What it is |
|---|---|
| `skill/apiguru-amazon-data/` | The agent skill: SKILL.md, references, and `scripts/probe.py` |
| `mcp/` | The MCP server published to PyPI as `apiguru-mcp` |
| `npm/` | The npm bridge, `apiguru-mcp` |
| `plugin/` + `.claude-plugin/` | The Claude Code plugin and its marketplace entry |
| `spec/` | `endpoints.json` and the generated `openapi.json` / `llms.txt` |

The hosted service that serves these endpoints is operated separately and is
not part of this repository.

## Licence

See `LICENSE`.
