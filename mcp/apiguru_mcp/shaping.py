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
    "customers_say", "variation", "product_variations",
)

# Always present in a projected record, whatever `fields` says.
_ALWAYS = ("asin",)

# Fields that are large and rarely needed; named so the model knows what
# `fields=` can request.
KNOWN_LARGE_FIELDS: tuple[str, ...] = (
    "from_manufacturer", "product_reviews", "brand_section", "tech_specs",
    "manufacture_section", "product_information", "product_videos_users",
    "product_videos_main", "description_enhanced", "all_product_variations",
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

# Kept by the light projection of one search-result row, in this order.
COMPACT_LIST_FIELDS: tuple[str, ...] = (
    "asin", "product_title", "product_brand",
    "product_price", "product_original_price", "product_price_per_unit",
    "product_star_rating", "product_num_ratings",
    "sales_volume", "is_prime", "is_sponsored", "badges", "promotion",
    "product_stock_message", "delivery_date",
    "product_url", "product_photo",
    # best-sellers / deals rows carry these instead of some of the above
    "rank", "product_minimum_offer_price", "deal_id", "deal_title",
    "deal_price", "list_price", "savings_percentage", "deal_url", "deal_photo",
    "seller_id", "seller_name",
)

# The keys a list row is expected to have; used to name what was dropped.
_LIST_ALWAYS = ("asin",)

DEFAULT_LIST_LIMIT = 10

# Which key holds the rows, per tool.
LIST_KEYS: dict[str, tuple[str, ...]] = {
    "search": ("products",),
    "best_sellers": ("products", "data"),
    "deals": ("deals", "products", "data"),
    "seller_products": ("products",),
    "seller_reviews": ("reviews", "data"),
}


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
    key = next((k for k in keys if isinstance(payload.get(k), list)), None)
    if key is None:
        return payload

    rows = payload[key]
    total = len(rows)
    if limit:  # 0 or None means "every row on this page"
        rows = rows[:limit]

    shaped = [project_list_row(row, compact=compact, fields=wanted) for row in rows]
    out = {**payload, key: shaped}

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
    return out
