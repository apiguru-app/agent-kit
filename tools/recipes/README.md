# Recipes

Small, runnable examples of the jobs agents most often do with Apiguru. Each
one is a complete script you can read in a minute and copy into your own agent.

| Recipe | What it does | Needs |
|---|---|---|
| [`compare_products.py`](compare_products.py) | Search a marketplace, enrich the top organic results in one batch call, print a comparison with a link to each product | Python 3.10+, `pip install "mcp>=2.0"` |
| [`enrich_catalogue.mjs`](enrich_catalogue.mjs) | Turn a file of ASINs or Amazon product URLs into JSON records, 20 per call, and report what could not be enriched | Node 18+ |

```bash
python compare_products.py "wireless earbuds" --geo US --top 5
node enrich_catalogue.mjs asins.txt --geo DE > records.jsonl
```

Both start keyless: three calls a day are free. Neither script pays. When the
free calls run out they stop and say how to continue: an Apiguru API key
(`--api-key-file`, a file readable only by you; it is sent only as the
`X-API-KEY` header), signing in through `https://mcp.apiguru.app/account`, or
an x402 client with a wallet and a spend cap. Batch calls bill per item, so
check `https://agent.apiguru.app/.well-known/x402` before a large job.

What the answers mean -- which fields are Amazon's own, which ASINs were not
returned and why, what is billed -- is in
[`https://agent.apiguru.app/llms-full.txt`](https://agent.apiguru.app/llms-full.txt).
