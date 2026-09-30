# Costs, billing and retries

Generated from the API spec - do not edit by hand.

## Prices

| Endpoint | Price |
|---|---|
| `/v2/product-details` | $0.003 per call |
| `/v2/product-reviews` | $0.003 per call |
| `/search` | $0.003 per call |
| `/product` | $0.0024 per item (max 20) |
| `/stock` | $0.0045 per item (max 10) |
| `/v2/best-sellers` | $0.003 per call |
| `/v2/deals` | $0.003 per call |
| `/seller-profile` | $0.0036 per item (max 10) |
| `/v2/seller-products` | $0.003 per call |
| `/v2/seller-reviews` | $0.003 per call |

Batch endpoints are billed per item and are cheaper per item than the
single-item equivalents. Always prefer them for more than one item.

## What each status means, and whether it costs money

| Status | Billed? | Meaning and what to do |
|---|---|---|
| `400` | no | Bad input (bad ASIN format, unknown geo, missing required param). NOT billed. |
| `401` | - | Missing or invalid API key on the keyed path. |
| `402` | - | Payment required. On the agent path this carries a PAYMENT-REQUIRED challenge (over MCP: an x402 PaymentRequired tool result, payable in-band via _meta["x402/payment"]). On the keyed path it means the account balance is exhausted. |
| `403` | - | Account disabled, or no active subscription plan. |
| `404` | **yes** | The ASIN genuinely does not exist on that marketplace. BILLED - the lookup was performed and the bad input was the caller's. Retrying will not help; try a different geo. |
| `413` | - | Too many items in a batch request. |
| `429` | - | Per-second rate limit exceeded for the plan. Back off and retry. |
| `500` | no | Internal error. NOT billed. |
| `502` | no | Bad gateway. NOT billed. Same class as 503: retry with backoff. |
| `503` | no | Temporary failure on our side. NOT billed. Safe and correct to retry. |
| `504` | no | Gateway timeout: the request ran past its deadline. NOT billed. Retry with backoff; a narrower query often succeeds. |
| `timeout` | - | No response before your own client's deadline. A keyless call without a payment is never billed. On an API key, or with an x402 payment attached, the request may still complete after your client gave up, and is then billed like any answered call. Some marketplaces answer more slowly than others; allow 60s rather than retrying early. |

## Retry policy

Retry 429, 500, 502, 503 and 504 with backoff -- none of them are billed -- unless the error body says `retryable: false`. A client-side timeout on a key or a payment may have completed and been billed; allow 60s before retrying it. Never retry 400, 401, 402, 403, 404 or 413: the request itself is the problem and repeating it will not change the answer.

Concretely (the same set `scripts/probe.py` retries):

```python
RETRYABLE = {429, 500, 502, 503, 504}   # plus your own client timeout
for attempt in range(4):
    try:
        status, body = call(..., timeout=60)
    except TimeoutError:
        status, body = 0, None      # never answered, never billed
    if (status in RETRYABLE or status == 0) and (body or {}).get('retryable') is not False:
        time.sleep(2 ** attempt)   # transient, not billed
        continue
    break                          # 200/400/402/404/413 are final
```

A body's `retryable` flag wins over the status table: a 5xx whose
cause is permanent says `retryable: false` and is not worth repeating.

## Free probes

The keyless gateway serves a few free requests per client per rolling
window before it starts charging. Response header
`X-Free-Probes-Remaining` tells you how many are left, and
`X-Price-Next-Call` what the next one will cost.
