#!/usr/bin/env node
// Recipe: turn a list of ASINs (or Amazon product URLs) into product records.
//
//   node enrich_catalogue.mjs asins.txt --geo DE > records.jsonl
//   node enrich_catalogue.mjs asins.txt --api-key-file ~/.apiguru-key > records.jsonl
//
// One ASIN or product URL per line. Calls /product in batches of 20 (the
// batch price is lower per item than single calls), writes one JSON record per
// line to stdout, and reports on stderr what could not be enriched:
//   - not_returned: Amazon has no record on that marketplace (billed; do not retry)
//   - unavailable:  a batch that failed on our side (not billed; safe to retry)
//
// Without a key the first 3 calls a day are free; after that the gateway
// answers 402 and this script stops -- it never pays. With --api-key-file the
// calls bill that Apiguru account (keep the file readable only by you); the
// key is sent only as the X-API-KEY header to agent.apiguru.app. Node 18+.

import { readFileSync } from "node:fs";

const BASE = "https://agent.apiguru.app/agent/v1";
const BATCH = 20;

const args = process.argv.slice(2);
const file = args.find((a) => !a.startsWith("--"));
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
if (!file) {
  console.error("usage: node enrich_catalogue.mjs <file with ASINs or URLs> [--geo US] [--api-key-file PATH]");
  process.exit(64);
}
const geo = opt("--geo"); // unset: US, or the marketplace of the URLs given
const keyFile = opt("--api-key-file");
const headers = { Accept: "application/json", "User-Agent": "apiguru-recipe-enrich/1.0" };
if (keyFile) headers["X-API-KEY"] = readFileSync(keyFile, "utf8").trim();

const items = [...new Set(readFileSync(file, "utf8").split(/\r?\n/).map((s) => s.trim()).filter(Boolean))];
const notReturned = [];
const unavailable = [];

for (let i = 0; i < items.length; i += BATCH) {
  const batch = items.slice(i, i + BATCH);
  // Items go in the query as-is: the gateway reads an ASIN in any case, or an
  // Amazon product URL, and says what it rewrote under parameters_interpreted.
  const url = `${BASE}/product?${new URLSearchParams(geo ? { asins: batch.join(","), geo } : { asins: batch.join(",") })}`;
  const res = await fetch(url, { headers });
  const body = await res.json().catch(() => ({}));
  if (res.status === 402) {
    console.error("Free calls spent (402). This script does not pay: pass --api-key-file, " +
                  "or pay with an x402 client. Stopping; nothing more was requested.");
    process.exit(2);
  }
  if (!res.ok) {
    console.error(`HTTP ${res.status}: ${body.error || ""} ${body.next_step || ""}`.trim());
    if (body.retryable) { unavailable.push(...batch); continue; }
    process.exit(1);
  }
  for (const record of body.results || []) {
    if (record && record.asins && record.error) { unavailable.push(...record.asins); continue; }
    process.stdout.write(JSON.stringify(record) + "\n");
  }
  notReturned.push(...(body.not_returned || []));
}

if (notReturned.length) console.error(`not_returned on ${geo || "that marketplace"} (billed, do not retry): ${notReturned.join(",")}`);
if (unavailable.length) console.error(`unavailable (not billed, safe to retry): ${unavailable.join(",")}`);
