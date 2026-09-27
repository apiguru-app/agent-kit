"""CLI entry point.

    apiguru-mcp                       # stdio (Claude Code, Codex CLI, OpenClaw)
    apiguru-mcp --http                # streamable HTTP on 127.0.0.1:8790/mcp
    apiguru-mcp --http --host 0.0.0.0 --port 8790
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apiguru-mcp",
        description="MCP server for the Apiguru Amazon Data API.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable HTTP instead of stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for --http.")
    parser.add_argument("--port", type=int, default=8790, help="Bind port for --http.")
    parser.add_argument(
        "--path", default="/mcp", help="URL path for the MCP endpoint under --http."
    )
    args = parser.parse_args(argv)

    if args.http:
        import logging

        import uvicorn

        from .http_app import create_app

        # Timestamped lines for our own loggers; the MCP traffic log replaces
        # uvicorn's bare access line, which had no caller, method or reason.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        # One line per stateless request ("Terminating session: None") drowned
        # everything else: ~2000 of them a day. Its warnings still show.
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)

        uvicorn.run(
            create_app(streamable_http_path=args.path, host=args.host),
            host=args.host,
            port=args.port,
            log_level="info",
            access_log=False,
        )
        return 0

    import anyio

    from .server import build_server

    anyio.run(build_server().run_stdio_async)
    return 0


if __name__ == "__main__":
    sys.exit(main())
