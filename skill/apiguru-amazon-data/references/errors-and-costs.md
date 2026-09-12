# Costs, billing and retries

Generated from the API spec - do not edit by hand.

## Prices

| Endpoint | Price |
|---|---|
| `/v2/product-details` | $0.01 per call |
| `/v2/product-reviews` | $0.01 per call |
| `/search` | $0.01 per call |
| `/product` | $0.008 per item (max 20) |
| `/stock` | $0.015 per item (max 10) |
| `/v2/best-sellers` | $0.01 per call |
| `/v2/deals` | $0.01 per call |
| `/seller-profile` | $0.012 per item (max 10) |
| `/v2/seller-products` | $0.01 per call |
| `/v2/seller-reviews` | $0.01 per call |

Batch endpoints are billed per item and are cheaper per item than the
single-item equivalents. Always prefer them for more than one item.

## What each status means, and whether it costs money

| Status | Billed? | Meaning and what to do |
|---|---|---|
| `400` | no | Bad input (bad ASIN format, unknown geo, missing required param). NOT billed. |
| `401` | - | Missing or invalid API key on the keyed path. |
| `402` | - | Payment required. On the agent path this carries a PAYMENT-REQUIRED challenge. On the keyed path it means the account balance is exhausted. |
| `403` | - | Account disabled, or no active subscription plan. |
| `404` | **yes** | The ASIN genuinely does not exist on that marketplace. BILLED - the upstream fetch was performed and the bad input was the caller's. Retrying will not help; try a different geo. |
| `413` | - | Too many items in a batch request. |
| `429` | - | Per-second rate limit exceeded for the plan. Back off and retry. |
| `500` | no | Internal error. NOT billed. |
| `502` | no | Bad gateway -- our reverse proxy could not get an answer from the gateway. NOT billed. Same class as 503: retry with backoff. |
| `503` | no | Upstream fetch failed on our side (block, parse fault). NOT billed. Safe and correct to retry. |
| `504` | no | Gateway timeout. The upstream fetch ran past its deadline. NOT billed. Retry with backoff; a narrower query often succeeds. |
| `timeout` | - | No response before your own client's deadline. Nothing is billed for a request we never answered. Cold-geo sessions are the slow case and are bounded at 25s server-side; allow 60s. |

## Retry policy

Retry 429, 500, 502, 503, 504 and client-side timeouts with backoff -- none of them are billed. Never retry 400, 401, 403, 404 or 413: the request itself is the problem and repeating it will not change the answer.

Concretely:

```python
for attempt in range(4):
    status, body = call(...)
    if status in (503, 429):
        time.sleep(2 ** attempt)   # transient, not billed
        continue
    break                          # 200/400/404 are final
```

## Free probes

The keyless gateway serves a few free requests per IP per rolling
window before it starts charging. Response header
`X-Free-Probes-Remaining` tells you how many are left, and
`X-Price-Next-Call` what the next one will cost.
