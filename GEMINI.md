# Apiguru Amazon Data (MCP)

Live Amazon marketplace data across 20 countries, as MCP tools: product_details,
product_details_batch, product_reviews, search, offers_stock, best_sellers,
deals, seller_profile_batch, seller_products, seller_reviews, and the free
list_capabilities.

Rules: ASINs are 10 uppercase alphanumerics; prefer the batch tools; product
records come back compact by default (ask for `fields` or `compact=false` for
more); every error is JSON with `billed`, `retryable` and `next_step`; a 404 is
billed, a 503 is not. Keyless calls get 3 free probes per day, then a 402 --
set `APIGURU_API_KEY` from https://dash.apiguru.app to bill an account.
