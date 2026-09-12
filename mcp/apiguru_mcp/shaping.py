"""Shape upstream payloads for an agent's context window.

A full `product_details` record is ~75 KB: `from_manufacturer` alone is
~39 KB of marketing HTML-turned-text and `product_reviews` another ~12 KB,
while the facts an agent usually wants -- price, rating, availability, the
bullets -- fit in ~4 KB. So the MCP tools default to a compact projection
and let the model ask for more, either whole (`compact=false`) or by name
(`fields="tech_specs,product_information"`). What was left out is always
listed under `_omitted_fields`, so nothing is silently hidden.

Also here: the loose output models that give every tool a formal
`outputSchema`. They list the top-level keys seen in the spec's
`output_example`, typed as `Any`, and allow extras -- honest about what is
known without promising a shape the upstream does not guarantee.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

# Fields kept by the compact projection of a product record, in this order.
COMPACT_PRODUCT_FIELDS: tuple[str, ...] = (
    "asin", "parent_asin", "product_title", "brand_name",
    "product_price", "product_original_price", "product_price_max", "currency",
    "price_snapshot", "product_star_rating", "product_num_ratings", "product_num_offers",
    "product_availability", "in_stock", "condition", "pre_order", "sales_volume",
    "delivery_info", "buybox_winner", "offer", "badges",
    "category", "category_path", "ranks",
    "about_product", "product_description", "product_overview",
    "product_photo", "product_url", "dimension", "weight",
    "customers_say", "variation_summary",
)

# Always present in a projected record, whatever `fields` says.
_ALWAYS = ("asin",)

# Fields that are large and rarely needed; named so the model knows what
# `fields=` can request.
#
# `variation` (details) and `product_variations` (batch) used to be in the
# compact set. They are the sibling maps, keyed by ASIN, and they scale with
# the listing family: 52 KB and 130 KB on a 733-variant product, which made
# the "compact" record 57 KB with the summary fields under 6 KB of it. An
# agent reported it as half the payload with nothing useful in it for a
# single-product question. `variation_summary` -- the attributes, their
# values, the count and this ASIN's own values -- replaces them; name either
# map in `fields=` to get the whole thing.
KNOWN_LARGE_FIELDS: tuple[str, ...] = (
    "from_manufacturer", "product_reviews", "brand_section", "tech_specs",
    "manufacture_section", "product_information", "product_videos_users",
    "product_videos_main", "description_enhanced", "all_product_variations",
    "variation", "product_variations",
    "product_photos", "qna", "related_to", "size_chart",
)


def parse_fields(fields: str | None) -> list[str]:
    if not fields:
        return []
    return [f.strip() for f in fields.split(",") if f.strip()]


def project_record(record: dict[str, Any], *, compact: bool, fields: list[str]) -> dict[str, Any]:
    """One product record -> the keys the caller asked for."""
    if not isinstance(record, dict):
        return record
    if fields:
        keep = list(dict.fromkeys([*_ALWAYS, *fields]))
    elif compact:
        keep = list(COMPACT_PRODUCT_FIELDS)
    else:
        return record

    out = {k: record[k] for k in keep if k in record}
    omitted = sorted(k for k in record if k not in out)
    unknown = [k for k in fields if k not in record]
    if omitted:
        out["_omitted_fields"] = omitted
    if unknown:
        out["_unknown_fields"] = unknown
    return out


def shape_product_payload(payload: Any, *, compact: bool, fields: str | None) -> Any:
    """Apply the projection to whichever envelope the backend used.

    product_details answers `{"data": {...}}`; product_details_batch answers
    `{"results": [...]}`. Anything else is returned untouched.
    """
    wanted = parse_fields(fields)
    if not compact and not wanted:
        return payload
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("data"), dict):
        return {**payload, "data": project_record(payload["data"], compact=compact, fields=wanted)}
    if isinstance(payload.get("results"), list):
        return {
            **payload,
            "results": [project_record(item, compact=compact, fields=wanted) for item in payload["results"]],
        }
    return payload


def shape_reviews_payload(payload: Any, *, max_reviews: int | None) -> Any:
    """Cap the review list; report how many there were."""
    if max_reviews is None or max_reviews < 0 or not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    reviews = data.get("product_reviews")
    if not isinstance(reviews, list) or len(reviews) <= max_reviews:
        return payload
    trimmed = {**data, "product_reviews": reviews[:max_reviews], "_reviews_total": len(reviews),
               "_reviews_returned": max_reviews}
    return {**payload, "data": trimmed}


def output_model_for(tool_name: str, example: Any) -> type[BaseModel] | None:
    """A permissive pydantic model naming the top-level keys of `example`."""
    if not isinstance(example, dict) or not example:
        return None
    fields: dict[str, Any] = {key: (Any, None) for key in example}
    fields.setdefault("_omitted_fields", (Any, None))
    fields.setdefault("_cache", (Any, None))
    return create_model(
        f"{tool_name}_output",
        __config__=ConfigDict(extra="allow"),
        **fields,
    )


# --- list endpoints (search, best-sellers, deals, seller products) ----------
#
# A page of search results is ~54 KB for 48 items, which no longer fits inline
# in a tool result and spills to a file. Two thirds of that is prose an agent
# does not read: `delivery` repeats its own `raw` line, and
# `product_delivery_info` repeats it again -- 15 KB of the 54 between them.
# So these tools default to a light record and the first `limit` rows, and say
# in the response how to get the rest.

# Kept by the light projection of one list row, in this order.
#
# This is an allowlist: a field absent from it is dropped. So it has to speak
# every vocabulary the list endpoints use, not just search's. It did not, and
# the cost was silent: best-sellers rows call their fields `title`, `price`,
# `star_rating`, `num_ratings`, `url` and `photo_url`, none of which appeared
# here, so a light best-sellers row projected down to `{"asin", "rank"}` --
# ten ASINs and no titles or prices. An agent asked for the best-selling
# electronics could not answer without ten more paid `product_details` calls.
# `_omitted_fields` reported the loss honestly, but nothing read it.
#
# Deals rows were guessed at too: `savings_percentage` never existed upstream,
# where the field is `discount_percentage`.
COMPACT_LIST_FIELDS: tuple[str, ...] = (
    "asin", "product_title", "product_brand",
    "product_price", "product_original_price", "product_price_per_unit",
    "product_star_rating", "product_num_ratings",
    "sales_volume", "is_prime", "is_sponsored", "badges", "promotion",
    "product_stock_message", "delivery_date",
    # `details_url` is added per row by the gateway's link augmentation and is
    # how an agent walks from a search hit to the full record. The light
    # projection was dropping it again one layer later, so the traversal the
    # gateway had just built was only ever visible on `compact=false`.
    "product_url", "product_photo", "details_url",
    # best-sellers / deals rows carry these instead of some of the above
    "rank", "product_minimum_offer_price", "deal_id", "deal_title",
    "deal_price", "list_price", "savings_percentage", "deal_url", "deal_photo",
    "seller_id", "seller_name",
    # best-sellers' own vocabulary: bare names, not `product_`-prefixed.
    "title", "price", "star_rating", "num_ratings", "url", "photo_url",
    # deals, as the upstream actually names them. brand_id and department_ids
    # are the values the `brands` and `categories` filters take, so a light
    # row is enough to narrow the next call.
    "discount_percentage", "discount_absolute", "deal_badge", "deal_ends_at",
    "deal_availability", "currency", "brand_id", "department_ids",
    # seller reviews.
    "review_id", "review_title", "review_comment", "review_star_rating",
    "review_date", "review_author",
)

# The keys a list row is expected to have; used to name what was dropped.
_LIST_ALWAYS = ("asin",)

DEFAULT_LIST_LIMIT = 10

# Which key holds the rows, per tool. Only `search` keeps them at the top
# level; the rest nest them one dict deeper -- see _find_rows.
LIST_KEYS: dict[str, tuple[str, ...]] = {
    "search": ("products",),
    "best_sellers": ("products", "data", "best_sellers"),
    "deals": ("deals", "products", "data"),
    "seller_products": ("products", "data"),
    "seller_reviews": ("reviews", "data"),
}


# Things that will mislead an agent READING these rows, as opposed to things
# it needs in order to CALL the tool. They used to live in the tool
# description, where every client paid for them at connect time whether or not
# it ever called the tool -- `search` alone was 5.2 KB of a 27 KB tools/list.
# Attached to the answer instead, they arrive with the rows they are about.
LIST_NOTES: dict[str, tuple[str, ...]] = {
    "search": (
        "`product_num_ratings` is the rating count for the whole listing "
        "family, not for this ASIN: Amazon pools reviews across variants, so "
        "every colour of one shoe reports the same number. Do not present it "
        "as this variant's review count.",
        "A variant's `product_title` can be the parent listing's title while "
        "the ASIN is the variant's. The URL slug usually shows which variant "
        "it really is; product_details on that ASIN is authoritative.",
        "`badges` is the source of truth for Amazon's Choice / Best Seller / "
        "Overall Pick; `is_amazon_choice` and `is_best_seller` derive from it.",
        "`filters_applied` / `filters_ignored` say which filters took effect on "
        "this marketplace and why one could not; `available_filters` lists the "
        "condition and deal refinements it offers. Page with `page` up to "
        "`metadata.total_pages`.",
        "`filters_applied.sort_by` is the ordering that was used. BEST_SELLERS "
        "is Amazon's popularity for this query, not a category rank: a row's "
        "badges are what the result card showed, so an ASIN that is #1 in its "
        "subcategory can carry none here while product_details reports "
        "best_seller=true with the rank. For a rank claim use product_details "
        "or best_sellers.",
    ),
    "seller_products": (
        "`filters_applied` / `filters_ignored` say which filters took effect on "
        "this marketplace; `available_filters` lists what it offers. The same "
        "filters as search apply: query, sort_by, min_price, max_price, "
        "product_condition, brand, category_id, deal_type.",
    ),
    "deals": (
        "Deal prices are what Amazon showed at fetch time and expire: check "
        "`deal_ends_at` before presenting one as current.",
        "`available_filters` lists the category ids (with names) and brand ids "
        "this marketplace accepts; `filters_applied` / `filters_ignored` say "
        "what actually took effect. Page with offset=`next_offset` (null when "
        "the feed is exhausted); a page is 30 rows and `total_count` caps at 500.",
    ),
    "best_sellers": (
        "`rank` is the position within the requested category on this page, "
        "not an absolute best-seller rank across Amazon.",
        "`category` says which department the answer is for; "
        "`available_categories` lists this marketplace's departments (slugs "
        "differ per marketplace) and `available_subcategories` the children "
        "of the one shown, whose ids `subcategory_code` takes. 50 rows a "
        "page, pages 1-5.",
        "`category_resolution.via` says how `category` was read; `fragment` "
        "means a word matched a department name ('shoes' -> the whole "
        "'Clothing, Shoes & Jewelry' department) and `hint` then lists the "
        "subcategories carrying that word with their ids. On amazon.com "
        "`subcategory_code` takes any browse node id or a name at any depth "
        "(\"women's shoes\", \"mules & clogs\"); `category.subcategory_path` "
        "shows where the node sits and `available_subcategories` its children.",
    ),
}


def _find_rows(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...] | None:
    """Path to the list of rows, or None.

    The lookup used to test `payload[key]` and nothing else, which is true
    only of `search`. Every other list endpoint nests its rows one level
    down -- `deals.data`, `data.products`, `data.reviews` -- so no key ever
    held a list, the shaping returned the payload untouched, and `limit`,
    `compact` and `_truncated` silently did nothing. `deals` answered with 30
    full rows, 75 KB of text, against a description promising 10 light ones.

    Top level is still tried first, so a tool whose rows are where they have
    always been behaves exactly as before.
    """
    for key in keys:
        if isinstance(payload.get(key), list):
            return (key,)
    for outer, container in payload.items():
        if isinstance(container, dict):
            for key in keys:
                if isinstance(container.get(key), list):
                    return (outer, key)
    return None


def _replace_at(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """A copy of `payload` with `path` set to `value`, copying as it descends."""
    head, *rest = path
    if not rest:
        return {**payload, head: value}
    return {**payload, head: _replace_at(payload[head], tuple(rest), value)}


def _flatten_delivery(row: dict[str, Any]) -> dict[str, Any]:
    """`delivery{}` + `product_delivery_info` -> one `delivery_date` string.

    The full text stays available with compact=false; it is the single
    biggest thing in a search response and the least often read.
    """
    delivery = row.get("delivery")
    if not isinstance(delivery, dict):
        return row
    date = delivery.get("free_delivery_date") or delivery.get("fastest_delivery_date")
    return {**row, "delivery_date": date}


def project_list_row(row: Any, *, compact: bool, fields: list[str]) -> Any:
    if not isinstance(row, dict):
        return row
    row = _flatten_delivery(row)
    if fields:
        keep = list(dict.fromkeys([*_LIST_ALWAYS, *fields]))
    elif compact:
        keep = list(COMPACT_LIST_FIELDS)
    else:
        return {k: v for k, v in row.items() if k != "delivery_date"}

    return {k: row[k] for k in keep if k in row and row[k] not in (None, "", [], {})}


def shape_list_payload(
    payload: Any,
    *,
    tool: str,
    compact: bool,
    fields: str | None,
    limit: int | None,
) -> Any:
    """Trim a list response to `limit` light rows, and say so in the answer."""
    if not isinstance(payload, dict):
        return payload
    wanted = parse_fields(fields)
    keys = LIST_KEYS.get(tool, ("products",))
    path = _find_rows(payload, keys)
    if path is None:
        return payload

    rows = payload[path[0]] if len(path) == 1 else payload[path[0]][path[1]]
    total = len(rows)
    if limit:  # 0 or None means "every row on this page"
        rows = rows[:limit]

    shaped = [project_list_row(row, compact=compact, fields=wanted) for row in rows]
    # The markers below stay at the top level whatever depth the rows sit at:
    # an agent should not have to know an endpoint's nesting to find out that
    # its answer was trimmed.
    out = _replace_at(payload, path, shaped)

    # The dropped keys are the same for every row, so name them once for the
    # whole answer rather than repeating a list on each of 48 rows. Compare
    # against the projection's field list, not against what survived: a field
    # that is simply empty on this page was not "omitted" by the projection.
    if (compact or wanted) and rows:
        seen: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                seen.extend(k for k in row if k not in seen)
        keep = set(wanted or COMPACT_LIST_FIELDS) | set(_LIST_ALWAYS)
        omitted = sorted(k for k in seen if k not in keep and k != "delivery_date")
        if omitted:
            out["_omitted_fields"] = omitted

    if len(shaped) < total:
        out["_truncated"] = {
            "returned": len(shaped),
            "of": total,
            "hint": f"this page held {total} rows; pass limit={total} for all of them, "
                    "or narrow the query with the filters this tool takes",
        }
    if compact and not wanted and shaped:
        out["_projection"] = (
            "light rows: the long delivery text is collapsed to `delivery_date`. "
            "Pass compact=false for the full record, or fields=\"a,b\" for named keys."
        )
    # Never on a true passthrough: `compact=false, limit=0` means "give me
    # exactly what upstream sent", and a caller diffing that against the REST
    # API should find nothing added.
    notes = LIST_NOTES.get(tool)
    if notes and shaped and (compact or wanted or len(shaped) < total):
        out["_notes"] = list(notes)
    return out
