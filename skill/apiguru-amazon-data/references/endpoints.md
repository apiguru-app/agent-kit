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

> Same 404-billed / 503-not-billed semantics as product_details. Takes no filters: it returns the rating, rating count, the 'customers say' summary and the reviews Amazon shows on the product page itself. There is no paging, star filter or sort -- Amazon's review pages require a signed-in customer, and the API does not sign in. For per-star counts read the rating histogram on product_details.

## `GET /search`

Search Amazon products by keyword. Filters: page, sort_by, category_id (browse node), min_price / max_price (decimals), product_condition (NEW / USED / RENEWED), brand, seller_id, today_deals and deal_type (coupons, all_discounts, buy_more_save_more). Every answer carries filters_applied, filters_ignored (with the reason) and available_filters for that marketplace.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Search keywords. Required and must be non-empty. |
| `page` | integer | no | Result page, 1-based. metadata.total_pages says how far it goes. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `sort_by` | enum | no | Result ordering. Default `RELEVANCE`. |
| `category_id` | string | no | Amazon browse node id to restrict to, e.g. 172282 (Electronics on US). Take one from a best_sellers answer's available_subcategories, a product's category_path, or node= in an Amazon URL. Ids differ per marketplace. |
| `min_price` | number | no | Lowest price, in the marketplace currency; decimals such as 19.99 are fine. |
| `max_price` | number | no | Highest price, in the marketplace currency. |
| `product_condition` | enum | no | NEW, USED or RENEWED (case-insensitive). Applied with the marketplace's own condition node; where a marketplace does not offer one, the answer's filters_ignored says so and available_filters lists what it does offer. |
| `brand` | string | no | Brand name as Amazon spells it (case-insensitive), e.g. Samsung. |
| `seller_id` | string | no | Restrict results to one seller's offers (Amazon seller id). |
| `today_deals` | boolean | no | Only items in Today's Deals, using that marketplace's own refinement. Where a marketplace has none (amazon.fr on 2026-09-08) it is reported under filters_ignored. Default `False`. |
| `deal_type` | enum | no | A specific promotion refinement: today_deals, all_discounts, coupons or buy_more_save_more. available_filters.deal_type lists the ones this marketplace has. |

`sort_by` accepts: `RELEVANCE`, `BEST_SELLERS`, `LOW_HIGH_PRICE`, `HIGH_LOW_PRICE`, `REVIEWS`, `NEWEST`

`product_condition` accepts: `NEW`, `USED`, `RENEWED`

`deal_type` accepts: `today_deals`, `all_discounts`, `coupons`, `buy_more_save_more`

> Blank values and the literal string 'null' are treated as unset. Invalid page, sort_by, price, product_condition or deal_type is a free 400 naming the parameter and the allowed values. Condition and deal refinements use per-marketplace node ids captured from Amazon's own search pages; a marketplace that lacks one gets the unfiltered feed plus an entry under filters_ignored, never a silent empty page. `product_num_ratings` and `offers_count` are integers; `product_star_rating`, `product_price` and `product_original_price` are decimal strings; a null field means Amazon did not show it. `is_prime` is true when the result carries a Prime badge or its delivery line offers Prime delivery. `metadata.total_pages` says how far `page` can go. A full page is up to 48 results and about 54 KB; the tool returns the first 10 as light rows by default and the answer carries `_truncated`, `_omitted_fields`, `_projection` and `_notes`. filters_applied echoes the effective sort_by (RELEVANCE when none was sent). A BEST_SELLERS ordering is Amazon's query-scoped popularity, not a category rank: a row's `badges` / `is_best_seller` are what the result card showed for this query, and an ASIN that is #1 in its subcategory can carry no badge here while product_details reports best_seller=true with the rank. For a rank claim, use product_details or best_sellers.

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
| `condition` | string | no | Comma-separated condition filter: ALL, NEW, USED_LIKE_NEW, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE (case-insensitive). Omit for every offer. An unknown value is a free 400 listing the allowed ones; it used to be silently treated as ALL. |

> Billed per upstream Amazon request, which is more than one per ASIN when check_inventory is true. /scrape is a legacy alias for the same handler.

## `GET /v2/best-sellers`

Best-seller rankings for a department of one marketplace, 50 per page. Every answer carries the department it resolved to and how (category_resolution: by slug, name or a fragment of a name, with a hint when a fragment such as 'shoes' landed on the whole 'Clothing, Shoes & Jewelry' department), available_categories (that marketplace's departments with slugs) and available_subcategories (the children of the node shown, with the ids subcategory_code takes). On amazon.com subcategory_code also takes any browse node id at any depth, or a name resolved under the department ("women's shoes", "mules & clogs"); category.subcategory_path gives the node's full path and category.heading the page's own title line.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `category` | string | no | Best-seller department, by slug or by name as Amazon shows it for that marketplace (case-insensitive; a unique fragment works, and the answer's category_resolution says when a fragment was used -- 'shoes' is the whole 'Clothing, Shoes & Jewelry' department, and the hint then lists the Shoes subcategories with their ids). Departments and their slugs differ per marketplace: amazon.com has electronics, amazon.de has ce-de (Electronics & Photo). Every answer lists that marketplace's departments under available_categories; an unknown or ambiguous name is a free 400 listing them. Default `appliances`. |
| `subcategory_code` | string | no | Browse node id under `category`: one from available_subcategories of a previous answer, or on amazon.com any node id at any depth (679410011 is Women > Shoes > Mules & Clogs) or a name resolved under the department ("women's shoes", "mens boots", "mules & clogs"). A name that fits two nodes equally (Men > Shoes > Boots and Women > Shoes > Boots) is a free 400 listing both ids with their paths. |
| `page` | integer | no | Result page, 1-based, 50 rows each; Amazon's lists stop at page 5. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> No required parameters - calling it bare returns US appliances page 1. `category` accepts a slug, a display name, one of the older US department names, or a unique fragment; category_resolution.via says which, and a fragment match adds a hint naming the subcategories that carry the word, with ids. On amazon.com the whole browse tree is known: subcategory_code takes any node id or a name at any depth, subcategory_name and subcategory_path are filled without a fetch, and available_subcategories lists the node's real children (empty on a leaf). Other marketplaces name only what their navigation showed. page is capped at 5 (a 400 beyond, not a 500). `rank` is the position within the requested list on this page. Rows are the same for every marketplace; only the department vocabulary differs, and the answer carries it.

## `GET /v2/deals`

Returns the current Amazon deals feed: ASIN, title, deal price, list price, discount, deal badge, start/end time and product links. Filter by department (categories), brand id (brands), rating cut-off, price bounds, minimum discount and Prime program. Every answer carries available_filters (the category and brand ids this marketplace accepts, with names), filters_applied / filters_ignored (what took effect) and next_offset (the next page, null when the feed ends).

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `offset` | integer | no | Row to start at. A page is 30 rows; pass the previous answer's next_offset for the next page. Default `0`. |
| `categories` | string | no | Department to restrict to: its id from available_filters.categories, or its name as Amazon shows it for that marketplace (case-insensitive; a unique fragment such as "electronics" works). US departments: Amazon Devices & Accessories, Appliances, Arts Crafts & Sewing, Audible Books & Originals, Automotive, Baby Products, Beauty & Personal Care, Books, CDs & Vinyl, Cell Phones & Accessories, Clothing Shoes & Jewelry, Collectibles & Fine Art, Electronics, Everything Else, Grocery & Gourmet Food, Handmade Products, Health & Household, Home & Kitchen, Industrial & Scientific, Kindle Store, Movies & TV, Musical Instruments, Office Products, Patio Lawn & Garden, Pet Supplies, Software, Sports & Outdoors, Tools & Home Improvement, Toys & Games, Video Games. Other marketplaces use their own localised names -- read them from available_filters.categories of any deals answer for that geo. An unknown name is a free 400 listing the valid names. |
| `brands` | string | no | Comma-separated brand ids, e.g. 46655 for Samsung on US. Take them from brand_id on any deals row or from available_filters.brands (the brands present in the current result). Names resolve only when this marketplace has already shown that brand; for a brand by name use /search with brand=<name> and today_deals=true instead. |
| `min_product_star_rating` | enum | no | Amazon's deals feed offers one rating cut-off: 4 = four stars and up. ALL or omitted = no cut-off. Other values are rejected with a free 400. |
| `min_price` | number | no | Lowest deal price to return, in the marketplace currency. Applied to the fetched rows; see notes. |
| `max_price` | number | no | Highest deal price to return, in the marketplace currency. |
| `min_discount` | integer | no | Smallest discount percentage to return, e.g. 50 for half price or better. |
| `max_discount` | integer | no | Largest discount percentage to return. |
| `prime_exclusive` | boolean | no | Only deals in Amazon's Prime Exclusive program. Default `False`. |
| `prime_early_access` | boolean | no | Only Prime Early Access deals. A marketplace lists the programs it is running under available_filters.prime_programs; when Early Access is not running the answer is empty with a hint saying so. Default `False`. |

`min_product_star_rating` accepts: `4`, `ALL`

> Filters are by id: categories takes a department id or name, brands takes brand ids only; available_filters in every answer lists both with names, and filters_applied / filters_ignored report what Amazon honoured. A page is 30 rows; page with offset=next_offset (null when exhausted); total_count caps at 500. min_price, max_price, min_discount and max_discount are applied to the rows after the fetch, scanning up to 3 upstream pages per call, so a page can hold fewer than 30 rows and total_count does not reflect them. An empty answer carries a hint saying why. Deal prices expire: check deal_ends_at. The older price_range and discount_range parameters are still accepted, as buckets (1-5 = under 25 / 25-50 / 50-100 / 100-200 / 200 and up; 1-4 = 10 / 25 / 50 / 70 percent off or more) or as bands such as 25-50 and 70+.

## `GET /seller-profile`

Returns the storefront profile for each seller id: business name, rating, feedback counts, address and marketplace presence.

**Price:** $0.012 per item (max 10)

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_ids` | string | yes | Comma-separated seller IDs, maximum 10. Each must be 13-15 alphanumeric characters or the whole call 400s. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> Seller ID validation is all-or-nothing: one malformed id rejects the entire request with 400.

## `GET /v2/seller-products`

Products listed by a seller: a storefront search. Takes the same filters as search -- query, page, sort_by, category_id, min_price / max_price, product_condition, brand, today_deals, deal_type -- and answers with filters_applied, filters_ignored and available_filters like search does.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_id` | string | yes | Restrict results to one seller's offers (Amazon seller id). |
| `query` | string | no | Optional keywords to search within this seller's storefront. |
| `page` | integer | no | Result page, 1-based. metadata.total_pages says how far it goes. Default `1`. |
| `geo` | enum | no | Marketplace country code. Default `US`. |
| `sort_by` | enum | no | Result ordering. Default `RELEVANCE`. |
| `category_id` | string | no | Amazon browse node id to restrict to, e.g. 172282 (Electronics on US). Take one from a best_sellers answer's available_subcategories, a product's category_path, or node= in an Amazon URL. Ids differ per marketplace. |
| `min_price` | number | no | Lowest price, in the marketplace currency; decimals such as 19.99 are fine. |
| `max_price` | number | no | Highest price, in the marketplace currency. |
| `product_condition` | enum | no | NEW, USED or RENEWED (case-insensitive). Applied with the marketplace's own condition node; where a marketplace does not offer one, the answer's filters_ignored says so and available_filters lists what it does offer. |
| `brand` | string | no | Brand name as Amazon spells it (case-insensitive), e.g. Samsung. |
| `today_deals` | boolean | no | Only items in Today's Deals, using that marketplace's own refinement. Where a marketplace has none (amazon.fr on 2026-09-08) it is reported under filters_ignored. Default `False`. |
| `deal_type` | enum | no | A specific promotion refinement: today_deals, all_discounts, coupons or buy_more_save_more. available_filters.deal_type lists the ones this marketplace has. |

`sort_by` accepts: `RELEVANCE`, `BEST_SELLERS`, `LOW_HIGH_PRICE`, `HIGH_LOW_PRICE`, `REVIEWS`, `NEWEST`

`product_condition` accepts: `NEW`, `USED`, `RENEWED`

`deal_type` accepts: `today_deals`, `all_discounts`, `coupons`, `buy_more_save_more`

> Unlike seller_profile_batch, seller_id format is not pattern-validated here. metadata.total_pages says how far page goes (48 rows a page). Invalid sort_by, price, product_condition or deal_type is a free 400 that lists the allowed values.

## `GET /v2/seller-reviews`

Returns paginated seller feedback, optionally filtered to a star-rating window.

**Price:** $0.01 per call

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `seller_id` | string | yes | Amazon seller ID. Required. |
| `page` | integer | no | Result page, 1-based, 5 reviews a page; has_next_page in the answer says whether another exists. Default `1`. |
| `from_rating` | integer | no | Lowest star rating to include, 1-5. |
| `to_rating` | integer | no | Highest star rating to include, 1-5. |
| `geo` | enum | no | Marketplace country code. Default `US`. |

> from_rating and to_rating are optional; omit both for unfiltered feedback. A page holds 5 reviews and the answer carries current_page and has_next_page; Amazon exposes no total, so page until has_next_page is false (up to page 100).

## Formats

- ASIN: `^[A-Z0-9]{10}$` - Uppercase only. Lowercase ASINs are rejected with 400 - normalise before calling.
- Seller ID: `^[A-Za-z0-9]{13,15}$`
- Sample ASIN for testing: `B09DJLW458`
