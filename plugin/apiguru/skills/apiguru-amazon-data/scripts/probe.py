#!/usr/bin/env python3
"""Call the Apiguru Amazon Data API from the command line.

Standard library only. Talks to exactly two first-party hosts, both fixed
below and never taken from the environment or from arguments:

  * https://agent.apiguru.app  keyless gateway: free probes, then HTTP 402
  * https://dash.apiguru.app   keyed API when --api-key is given, and the
                               unauthenticated feedback wall (`feedback`)

Every request is pinned to one of those origins and redirects away from it are
refused, so an API key cannot be carried to a third host by a redirect.

This script never pays. A 402 is reported and the script exits; it contains
no wallet, no x402 client and no automatic retry with payment. Whether to
spend money is the user's decision.

A key is never taken from the command line, from the environment, or from any
file the user did not name -- a value in `argv` is visible in shell history and
in the process table to every other local user. Three explicit ways in, all
requiring the user to act:

  * --api-key             prompt (getpass; the key is not echoed and not stored)
  * --api-key-file PATH   read it from a file the user names
  * --api-key-stdin       read it from standard input, e.g. from a secret store

    python probe.py capabilities                      # prices, free probes left; costs nothing
    python probe.py product-details --asin B09DJLW458 --geo US
    python probe.py product --asins B09DJLW458,B0BSHF7WHW --geo DE
    python probe.py search --query "wireless earbuds" --geo UK
    python probe.py product-details --asin B09DJLW458 --geo US --api-key
    pass show apiguru | python probe.py product-details --asin B09DJLW458 --api-key-stdin
    python probe.py feedback --message "search: titles are brand-only" --category bug
"""

from __future__ import annotations

import argparse
import getpass
import json
import pathlib
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Fixed on purpose. Making these configurable would let a poisoned
# environment or prompt redirect requests (and an API key) elsewhere.
KEYLESS_BASE = "https://agent.apiguru.app/agent/v1"
KEYED_BASE = "https://dash.apiguru.app/api/v1"
CAPABILITIES_URL = "https://agent.apiguru.app/.well-known/x402"
# The feedback wall. Unauthenticated, never billed, and the only POST this
# script makes. It sends exactly the text passed on the command line.
FEEDBACK_URL = "https://dash.apiguru.app/api/v1/feedback"
GITHUB_ISSUES = "https://github.com/apiguru-app/agent-kit/issues"

# command -> path. Mirrors the endpoint list; see references/endpoints.md.
COMMANDS = {
    "product-details": "/v2/product-details",
    "product-reviews": "/v2/product-reviews",
    "search": "/search",
    "product": "/product",
    "stock": "/stock",
    "best-sellers": "/v2/best-sellers",
    "deals": "/v2/deals",
    "seller-profile": "/seller-profile",
    "seller-products": "/v2/seller-products",
    "seller-reviews": "/v2/seller-reviews",
}

# Transient AND unbilled, so retrying is free. Nothing else is retried.
RETRYABLE = {429, 503}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    Fixing the base URL in the source is not enough on its own: urlopen
    follows redirects by default and keeps the request headers, so a redirect
    from the API host would carry `X-API-KEY` to wherever it pointed. Neither
    of these two endpoints redirects, so any redirect is either a
    misconfiguration or an attempt to move the credential -- both are errors.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(
            f"refused a {code} redirect to {newurl!r}: this script does not follow "
            "redirects, so an API key can never leave the host it was sent to"
        )


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirect)


def open_url(req, timeout: int):
    """Every request in this file goes through here, redirects refused."""
    return opener().open(req, timeout=timeout)


def read_api_key(args) -> str | None:
    """Get the key from the one place the user chose, never from argv.

    A secret passed as a command-line value lands in shell history and in the
    process table, where any other local user can read it.
    """
    sources = [bool(args.api_key), bool(args.api_key_file), bool(args.api_key_stdin)]
    if sum(sources) > 1:
        print("Choose one of --api-key, --api-key-file, --api-key-stdin.", file=sys.stderr)
        raise SystemExit(2)

    if args.api_key_file:
        path = pathlib.Path(args.api_key_file)
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Could not read the key file: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        try:
            mode = path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                print(f"Warning: {path} is readable by other users; chmod 600 it.",
                      file=sys.stderr)
        except OSError:
            pass
        return key or None

    if args.api_key_stdin:
        key = sys.stdin.readline().strip()
        if not key:
            print("No key on stdin.", file=sys.stderr)
            raise SystemExit(2)
        return key

    if args.api_key:
        if not sys.stdin.isatty():
            print("--api-key prompts for the key and needs a terminal. "
                  "Use --api-key-stdin or --api-key-file PATH instead.", file=sys.stderr)
            raise SystemExit(2)
        key = getpass.getpass("Apiguru API key (not echoed, not stored): ").strip()
        return key or None

    return None


def request(path: str, params: dict[str, str], api_key: str | None, retries: int = 3):
    """GET the endpoint; keyed when an API key was passed explicitly."""
    base = KEYED_BASE if api_key else KEYLESS_BASE
    query = {k: v for k, v in params.items() if v is not None}
    url = f"{base}{path}?{urllib.parse.urlencode(query)}"

    headers = {"Accept": "application/json", "User-Agent": "apiguru-skill-probe/1.1"}
    if api_key:
        # Only ever sent to KEYED_BASE above.
        headers["X-API-KEY"] = api_key

    last_error = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with open_url(req, timeout=180) as response:
                return response.status, json.load(response), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"error": raw[:500]}

            if exc.code in RETRYABLE and attempt < retries:
                delay = 2**attempt
                print(
                    f"  [{exc.code}] transient and not billed, retrying in {delay}s "
                    f"({attempt + 1}/{retries})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                last_error = (exc.code, body, dict(exc.headers))
                continue
            return exc.code, body, dict(exc.headers)
        except urllib.error.URLError as exc:
            last_error = (0, {"error": f"connection failed: {exc.reason}"}, {})
            if attempt < retries:
                time.sleep(2**attempt)
                continue
    return last_error or (0, {"error": "request failed"}, {})


def explain(status: int, headers: dict) -> None:
    """Say what a status means for cost and for what to do next."""
    left = headers.get("X-Free-Probes-Remaining")
    if left is not None:
        note = headers.get("X-Price-Next-Call", "")
        print(
            f"  free probes remaining: {left}"
            + (f" (next call would cost {note})" if note else ""),
            file=sys.stderr,
        )

    messages = {
        402: (
            "Payment required: the free probes for this machine are spent. "
            "This script does not pay. Stop and ask the user how to proceed: "
            "they can provide an Apiguru API key (--api-key, bills their account) "
            "or, if they explicitly want pay-per-call, run their own x402 client "
            "with a spend cap. Do not set either up on your own."
        ),
        404: "Not found. BILLED on the keyed path: the ASIN is absent from this marketplace; try another geo.",
        400: "Bad input, not billed. ASINs must be 10 UPPERCASE alphanumeric chars.",
        503: "Upstream failure, not billed. Safe to retry.",
        429: "Rate limited. Back off and retry.",
    }
    if status in messages:
        print(f"  {messages[status]}", file=sys.stderr)


def cmd_capabilities() -> int:
    """Show prices and the free-probe policy without spending a probe."""
    try:
        with open_url(urllib.request.Request(CAPABILITIES_URL), timeout=30) as response:
            data = json.load(response)
    except Exception as exc:
        print(f"Could not fetch capabilities: {exc}", file=sys.stderr)
        return 1

    print(f"{data['service']}")
    print(f"  rails: {', '.join(data['rails']) or 'none'}")
    print(
        f"  free probes: {data['freeProbesPerIp']} per machine "
        f"per {data['freeProbeWindowHours']}h; this script never pays a 402"
    )
    if data.get("howToPay"):
        print(f"  how paying works (for the user to read): {data['howToPay']}")
    print("  endpoints:")
    for resource in data["resources"]:
        print(f"    {resource['name']:24} {resource['price']:22} {resource['method']} "
              f"{resource['resource']}")
    return 0


def cmd_feedback(args) -> int:
    """Post one entry to the public feedback wall. Free, no key, no signup."""
    if not args.message:
        print("feedback needs --message. Example:", file=sys.stderr)
        print('  python probe.py feedback --message "search: product_title is the brand" '
              '--category bug --endpoint /search', file=sys.stderr)
        print(f"With a GitHub account, prefer an issue: {GITHUB_ISSUES}", file=sys.stderr)
        return 2

    payload = {
        "message": args.message,
        "category": (args.category or "other").lower(),
        "endpoint": args.endpoint,
        "agent": args.agent or "apiguru-skill-probe",
        "contact": args.contact,
        "source": "skill",
    }
    body = json.dumps({k: v for k, v in payload.items() if v}).encode("utf-8")
    request_obj = urllib.request.Request(
        FEEDBACK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "apiguru-skill-probe/1.1",
        },
    )
    try:
        with open_url(request_obj, timeout=30) as response:
            print(json.dumps(json.load(response), indent=2, ensure_ascii=False))
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"Could not reach the feedback wall: {exc}", file=sys.stderr)
    print(f"Open an issue instead: {GITHUB_ISSUES}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query the Apiguru Amazon Data API. Keyless by default; never pays a 402.",
    )
    parser.add_argument("command", choices=[*COMMANDS, "capabilities", "feedback"])
    for flag in (
        "asin", "asins", "geo", "query", "page", "sort_by", "seller_id",
        "seller_ids", "category", "subcategory_code", "offset", "categories",
        "brands", "brand", "min_price", "max_price", "condition",
        "offers_count", "from_rating", "to_rating",
        # feedback-only (`category` above is reused)
        "message", "endpoint", "agent", "contact",
    ):
        parser.add_argument(f"--{flag.replace('_', '-')}", dest=flag, default=None)
    parser.add_argument("--check-inventory", dest="check_inventory", action="store_true")
    # A key is never a command-line VALUE: argv is visible in shell history and
    # in the process table. These three make the user choose how it arrives.
    parser.add_argument(
        "--api-key",
        dest="api_key",
        action="store_true",
        help="Prompt for an Apiguru API key (not echoed, not stored) and bill that "
             "account instead of using free probes. Only with the user's explicit consent.",
    )
    parser.add_argument(
        "--api-key-file",
        dest="api_key_file",
        default=None,
        help="Read the API key from this file (chmod 600 it).",
    )
    parser.add_argument(
        "--api-key-stdin",
        dest="api_key_stdin",
        action="store_true",
        help="Read the API key from standard input, e.g. piped from a secret store.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw JSON only.")

    args = parser.parse_args(argv)

    if args.command == "capabilities":
        return cmd_capabilities()
    if args.command == "feedback":
        return cmd_feedback(args)

    params = {
        k: v
        for k, v in vars(args).items()
        if k not in ("command", "raw", "check_inventory",
                     "api_key", "api_key_file", "api_key_stdin",
                     "message", "endpoint", "agent", "contact")
        and v is not None
    }
    if args.check_inventory:
        params["check_inventory"] = "true"

    api_key = read_api_key(args)

    if not args.raw:
        mode = "keyed: this call bills the account behind that key" if api_key else "keyless: free probe"
        print(f"-> {args.command} ({mode})", file=sys.stderr)

    status, body, headers = request(COMMANDS[args.command], params, api_key)

    if not args.raw:
        explain(status, headers)

    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
