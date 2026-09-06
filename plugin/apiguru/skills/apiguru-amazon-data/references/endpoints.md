# Apiguru endpoint reference

Generated from the API spec - do not edit by hand.

- Keyless base URL: `https://agent.apiguru.app/agent/v1`
- Keyed base URL: `https://dash.apiguru.app/api/v1` (send `X-API-KEY`)

All endpoints are `GET` with query parameters.

## Marketplaces

Pass as `geo`, chosen from the user's request or the Amazon domain they mention (amazon.de -> DE). The API assumes `US` only when the parameter is omitted; do not rely on that default.

| Code | Domain |
|---|---|
| `US` | amazon.com |
| `CA` | amazon.ca |
| `DE` | amazon.de |
| `MX` | amazon.com.mx |
| `UK` | amazon.co.uk |
| `FR` | amazon.fr |
| `IT` | amazon.it |
| `ES` | amazon.es |
| `AU` | amazon.com.au |
| `BR` | amazon.com.br |
| `IN` | amazon.in |
| `JP` | amazon.co.jp |
| `NL` | amazon.nl |
| `AE` | amazon.ae |
| `PL` | amazon.pl |
| `SA` | amazon.sa |
| `SG` | amazon.sg |
| `SE` | amazon.se |
| `TR` | amazon.com.tr |
| `BE` | amazon.com.be |

## `GET /v2/product-details`

Fetches the complete product record for one ASIN on one marketplace: title, price, star rating, rating count, images, description, feature bullets, variations and category.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `asin` | string | yes | Single Amazon ASIN, 10 uppercase alphanumeric characters. Exactly one - comma-separated lists are rejected; use product_details_batch for many. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> 404 means the ASIN is absent from that marketplace and IS billed. 503 means our fetch failed and is NOT billed - retry. Bullet points and specs are what Amazon shows for the listing; on multi-variant listings they can describe the product family rather than the exact variant. A null field means Amazon did not show it.

## `GET /v2/product-reviews`

Returns the review block for one ASIN: overall star rating, total rating count, Amazon's 'customers say' AI summary, and the individual review list.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `asin` | string | yes | Single Amazon ASIN, 10 uppercase alphanumeric characters. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> Same 404-billed / 503-not-billed semantics as product_details.

## `GET /search`

Keyword search with pagination, sorting, and filtering by category, price range, condition, brand or seller.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Search keywords. Required and must be non-empty. |
| `page` | integer | no | Result page, 1-based. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `sort_by` | enum | no | Result ordering. Default `RELEVANCE`. |
| `category_id` | string | no | Restrict to an Amazon category id. |
| `min_price` | string | no | Minimum price filter, marketplace currency. |
| `max_price` | string | no | Maximum price filter, marketplace currency. |
| `product_condition` | string | no | Condition filter, e.g. NEW or USED. |
| `brand` | string | no | Brand name filter. |
| `seller_id` | string | no | Restrict results to one seller. |
| `today_deals` | boolean | no | Restrict to items in today's deals. Default `False`. |

`sort_by` accepts: `RELEVANCE`, `BEST_SELLERS`, `LOW_HIGH_PRICE`, `HIGH_LOW_PRICE`, `REVIEWS`, `NEWEST`

> Blank values and the literal string 'null' are treated as unset. `page` must be a positive integer or the call 400s. Ten filters narrow a search besides `query`: page, sort_by, geo, brand, seller_id, category_id, min_price, max_price, product_condition and today_deals. `product_num_ratings` and `offers_count` are integers; `product_star_rating`, `product_price` and `product_original_price` are decimal strings; a null field means Amazon did not show it for that result. `is_prime` is true when the result carries a Prime badge or its delivery line offers Prime delivery. `metadata.total_pages` says how far `page` can go. TWO THINGS TO READ CAREFULLY: `product_num_ratings` is the count for the whole listing family, not for this ASIN - Amazon pools reviews across variants, so every colour of one shoe reports the same number. And a variant's `product_title` can be the parent's title while the ASIN is the variant's; the URL slug usually shows which variant it really is, and /v2/product-details on that ASIN is authoritative. `badges` is the source of truth for Amazon's Choice / Best Seller / Overall Pick; `is_amazon_choice` and `is_best_seller` are derived from it. `delivery` splits the delivery line: `free_delivery_date` is the date a non-member gets for free, `prime_delivery` the slot Prime would give, `fastest_delivery_date` the paid faster option; `raw` is always the whole line. Dates are the strings Amazon printed, and on non-English marketplaces only `raw` may be filled. A full page is up to 48 results and about 54 KB of JSON; over MCP the tool returns the first 10 as light rows by default and tells you how to ask for more (limit, compact, fields).

## `GET /product`

Batch variant of product_details. Accepts a comma-separated ASIN list, deduplicates it, and fetches all of them concurrently. Far cheaper and faster than N single calls.

**Price:** $0.008 per item (max 20)

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `asins` | string | yes | Comma-separated ASIN list, maximum 20 after de-duplication. Each must be 10 uppercase alphanumeric characters. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> Billed per ASIN processed, including ones that come back not-found. More than 20 ASINs returns 413. Bullet points and specs are what Amazon shows for the listing; on multi-variant listings they can describe the product family rather than the exact variant. A null field means Amazon did not show it.

## `GET /stock`

Returns the current offer list per ASIN (seller, price, condition, buy-box winner) and, optionally, the actual purchasable stock quantity.

**Price:** $0.015 per item (max 10)

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `asins` | string | yes | Comma-separated ASIN list, maximum 10. Each must be 10 uppercase alphanumeric characters; malformed entries are rejected with 400. |
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `check_inventory` | boolean | no | Resolve the true purchasable stock quantity. Slower and bills more upstream requests, so leave off unless you need the number. Default `False`. |
| `offers_count` | string | no | 'all' for every offer, 'winner' for the buy-box offer only, or a specific alphanumeric Offer ID. Default `all`. |
| `condition` | string | no | Comma-separated condition filter. Any of ALL, NEW, USED_LIKE_NEW, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE. Unrecognised values silently fall back to ALL. |

> Billed per upstream Amazon request, which is more than one per ASIN when check_inventory is true. /scrape is a legacy alias for the same handler.

## `GET /v2/best-sellers`

Returns the current Amazon best-seller list for a category, with optional subcategory drill-down and pagination.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `category` | string | no | Category slug, lowercased by the server. Defaults to 'appliances'. Default `appliances`. |
| `subcategory_code` | string | no | Optional subcategory node id to drill into. |
| `page` | integer | no | Result page, 1-based. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> No required parameters - calling it bare returns US appliances page 1.

## `GET /v2/deals`

Returns active Amazon deals, filterable by category, brand, minimum star rating, price band, discount band, and Prime early access.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `offset` | integer | no | Pagination offset, non-negative. Default `0`. |
| `categories` | string | no | Category filter. |
| `min_product_star_rating` | enum | no | Minimum star rating. Only 1, 2, 3, 4 or ALL are accepted - 5 is rejected with 400. |
| `price_range` | enum | no | Price band bucket 1-5, or ALL. |
| `discount_range` | enum | no | Discount band bucket 1-4, or ALL. |
| `brands` | string | no | Brand filter. |
| `prime_early_access` | boolean | no | Restrict to Prime early access deals. Default `False`. |

`min_product_star_rating` accepts: `1`, `2`, `3`, `4`, `ALL`

`price_range` accepts: `1`, `2`, `3`, `4`, `5`, `ALL`

`discount_range` accepts: `1`, `2`, `3`, `4`, `ALL`

> The bucket parameters are ordinal buckets, not literal prices or percentages. min_product_star_rating rejects 5.

## `GET /seller-profile`

Returns the storefront profile for each seller id: business name, rating, feedback counts, address and marketplace presence.

**Price:** $0.012 per item (max 10)

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_ids` | string | yes | Comma-separated seller IDs, maximum 10. Each must be 13-15 alphanumeric characters or the whole call 400s. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> Seller ID validation is all-or-nothing: one malformed id rejects the entire request with 400.

## `GET /v2/seller-products`

Returns the paginated catalogue of products offered by a given seller storefront.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_id` | string | yes | Amazon seller ID. Required. |
| `page` | integer | no | Result page, 1-based. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> Unlike seller_profile_batch, seller_id format is not pattern-validated here.

## `GET /v2/seller-reviews`

Returns paginated seller feedback, optionally filtered to a star-rating window.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_id` | string | yes | Amazon seller ID. Required. |
| `page` | integer | no | Result page, 1-based. Default `1`. |
| `from_rating` | integer | no | Lower bound of the star-rating filter. |
| `to_rating` | integer | no | Upper bound of the star-rating filter. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> from_rating and to_rating are optional; omit both for unfiltered feedback.

## Formats

- ASIN: `^[A-Z0-9]{10}$` - Uppercase only. Lowercase ASINs are rejected with 400 - normalise before calling.
- Seller ID: `^[A-Za-z0-9]{13,15}$`
- Sample ASIN for testing: `B09DJLW458`
