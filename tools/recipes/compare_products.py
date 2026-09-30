#!/usr/bin/env python3
"""Recipe: compare products for a shopping question, over MCP.

    pip install "mcp>=2.0"
    python compare_products.py "wireless earbuds" --geo US --top 5

What it does, as an agent would:
  1. `search` the marketplace for the query (one call);
  2. `product_details_batch` on the top organic results (one call, cheaper
     per item than N single calls);
  3. prints a comparison: price, rating, rating count, Prime, and the link
     to each product, so every number can be checked at the source.

It connects to the hosted MCP server keyless. Three calls a day are free;
after that a tool returns an x402 PaymentRequired result. This script never
pays: it stops and prints how to continue (sign in, an API key, or an
x402-capable MCP client). To bill an Apiguru account instead, pass
--api-key-file with a file that holds the key (keep it readable only by you).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

MCP_URL = "https://mcp.apiguru.app/mcp"


def result_json(result):
    """The tool's answer as a dict: structuredContent, else the text block."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text[text.find("{"):])
            except ValueError:
                return {"error": text}
    return {}


def stop_if_unpaid(result, answer) -> None:
    if getattr(result, "is_error", False):
        if answer.get("x402Version"):
            print("The free calls are spent. " + answer.get("next_step", ""), file=sys.stderr)
        else:
            print(f"The call failed: {answer.get('error')} -- {answer.get('next_step', '')}", file=sys.stderr)
        raise SystemExit(2)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("query")
    parser.add_argument("--geo", default="US")
    parser.add_argument("--top", type=int, default=5, help="how many products to compare (max 20)")
    parser.add_argument("--api-key-file", help="bill an Apiguru account instead of the free calls")
    args = parser.parse_args()

    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    transport = MCP_URL
    if args.api_key_file:
        with open(args.api_key_file, encoding="utf-8") as fh:
            key = fh.read().strip()
        # The key travels only as the X-API-KEY header to mcp.apiguru.app.
        transport = streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers={"X-API-KEY": key}))
    async with Client(transport) as session:
        found = await session.call_tool("search", {"query": args.query, "geo": args.geo, "limit": 20})
        page = result_json(found)
        stop_if_unpaid(found, page)
        rows = [r for r in page.get("products", []) if r.get("asin") and not r.get("is_sponsored")]
        asins = list(dict.fromkeys(r["asin"] for r in rows))[: max(1, min(args.top, 20))]
        if not asins:
            print("No organic results for that query.", file=sys.stderr)
            return 1

        detail = await session.call_tool("product_details_batch",
                                         {"asins": ",".join(asins), "geo": args.geo})
        records = result_json(detail)
        stop_if_unpaid(detail, records)

    # Rating, rating count and Prime come from the search row; the current
    # price, availability and delivery from the batch record, fetched just now.
    rows_by_asin = {r["asin"]: r for r in rows}
    by_asin = {r.get("asin"): r for r in records.get("results", [])}
    print(f"{'ASIN':<11} {'price':>10} {'rating':>6} {'ratings':>9} {'prime':>5}  title / availability")
    for asin in asins:
        row, rec = rows_by_asin.get(asin, {}), by_asin.get(asin)
        if rec is None:
            print(f"{asin:<11} {'-':>10} {'':>6} {'':>9} {'':>5}  (not returned on {args.geo})")
            continue
        price = rec.get("price") or row.get("product_price") or "-"
        print(f"{asin:<11} {str(price):>10} {str(row.get('product_star_rating') or '-'):>6} "
              f"{str(row.get('product_num_ratings') or '-'):>9} {'yes' if row.get('is_prime') else 'no':>5}  "
              f"{(rec.get('title') or row.get('product_title') or '')[:60]}")
        print(f"{'':<11} {(rec.get('availability') or '')[:40]:<40} {rec.get('url') or row.get('product_url') or ''}")
    if records.get("not_returned"):
        print(f"\nNot returned on {args.geo}: {', '.join(records['not_returned'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
