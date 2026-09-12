"""MCP server for the Apiguru Amazon Data API."""

# Before the imports: server.py reads it to stamp feedback entries.
__version__ = "1.1.25"

from .server import build_server  # noqa: E402
from .spec import api_info, endpoints, load_spec  # noqa: E402

__all__ = ["build_server", "api_info", "endpoints", "load_spec", "__version__"]
