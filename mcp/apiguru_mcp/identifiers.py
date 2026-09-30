"""ASINs as callers have them.

A user hands an agent an Amazon link, not an ASIN; an agent copies an ASIN
out of prose in lower case. Both used to be refused by the tool schema
before the call was made, and the agent had to work out the fix. Now an ASIN
may arrive as:

- the ASIN itself, in any case, with stray spaces;
- an Amazon product URL: /dp/<ASIN>, /gp/product/<ASIN>, /gp/aw/d/<ASIN>,
  /product-reviews/<ASIN>, or ?asin=<ASIN>.

A URL also names its marketplace (amazon.de -> DE). That is used when the
call left `geo` at its default; a URL on one marketplace with an explicit
`geo` of another is refused rather than guessed. Every rewrite is reported
back under `_input_interpreted`, so nothing changes silently.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ApiguruError

_ASIN = re.compile(r"[A-Za-z0-9]{10}")
_IN_PATH = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|product-reviews|exec/obidos/asin|o/asin)/([A-Za-z0-9]{10})(?=[/?#&]|$)",
    re.IGNORECASE,
)
_IN_QUERY = re.compile(r"[?&]asin=([A-Za-z0-9]{10})(?=[&#]|$)", re.IGNORECASE)

DEFAULT_GEO = "US"

# Which parameters hold ASINs, and whether they are lists.
ASIN_PARAMS = {"asin": False, "asins": True}


def parse(raw: Any) -> tuple[str | None, str | None]:
    """(ASIN, host of the URL it came from) -- or (None, None) if there is none."""
    value = str(raw or "").strip()
    if _ASIN.fullmatch(value):
        return value.upper(), None
    match = _IN_PATH.search(value) or _IN_QUERY.search(value)
    if not match:
        return None, None
    host = re.sub(r"^[a-z]+://", "", value.lower()).split("/", 1)[0].split("?", 1)[0]
    return match.group(1).upper(), host


def marketplace_of(host: str | None, geos: dict[str, str]) -> str | None:
    """amazon.co.uk -> UK, using the spec's code -> domain-suffix table."""
    if not host:
        return None
    suffix = re.sub(r"^(?:www\.|smile\.|m\.)?amazon\.", "", host)
    for code, tld in geos.items():
        if suffix == tld:
            return code
    return None


def relaxed_schema(name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """The tool-schema property for an ASIN parameter that also takes URLs."""
    prop = dict(prop)
    prop.pop("pattern", None)
    if not ASIN_PARAMS[name]:
        prop["pattern"] = r"^\s*(?:[A-Za-z0-9]{10}|https?://\S+)\s*$"
    prop["description"] = (prop.get("description", "").rstrip()
                           + (" An Amazon product URL (…/dp/ASIN) works too, in place of an ASIN;"
                              " lower case is fine. A URL's domain sets the marketplace unless geo says"
                              " otherwise."))
    return prop


def normalise(arguments: dict[str, Any], geos: dict[str, str]) -> dict[str, str]:
    """Rewrite ASIN parameters in place; return what was reinterpreted.

    Raises a 400-shaped ApiguruError (never billed) for a value that holds no
    ASIN, or for URLs that contradict each other or an explicit geo.
    """
    interpreted: dict[str, str] = {}
    url_markets: set[str] = set()
    for name, is_list in ASIN_PARAMS.items():
        raw = arguments.get(name)
        if raw is None:
            continue
        items = [p for p in str(raw).split(",") if p.strip()] if is_list else [raw]
        asins = []
        for item in items:
            asin, host = parse(item)
            if asin is None:
                raise ApiguruError(
                    f"Bad input: {str(item).strip()[:120]!r} is neither an ASIN nor an Amazon product URL.",
                    http_status=400, billed=False, retryable=False,
                    next_step=("Send a 10-character ASIN (e.g. B09DJLW458) or a product URL such as "
                               "https://www.amazon.com/dp/B09DJLW458. Nothing was fetched or charged."),
                )
            market = marketplace_of(host, geos)
            if market:
                url_markets.add(market)
            asins.append(asin)
        value = ",".join(asins)
        if value != str(raw):
            arguments[name] = value
            interpreted[name] = value
    if len(url_markets) > 1:
        raise ApiguruError(
            f"Bad input: the URLs are on different marketplaces ({', '.join(sorted(url_markets))}).",
            http_status=400, billed=False, retryable=False,
            next_step="One call covers one marketplace; split the URLs by marketplace. Nothing was charged.",
        )
    if url_markets:
        market = url_markets.pop()
        geo = str(arguments.get("geo") or DEFAULT_GEO).upper()
        if geo != market:
            if geo != DEFAULT_GEO:
                raise ApiguruError(
                    f"Bad input: geo={geo}, but the URL is on the {market} marketplace.",
                    http_status=400, billed=False, retryable=False,
                    next_step=f"Drop geo, or send geo={market}. Nothing was fetched or charged.",
                )
            arguments["geo"] = market
            interpreted["geo"] = f"{market} (from the URL)"
    return interpreted
