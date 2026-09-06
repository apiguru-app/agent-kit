"""MCP server for the Apiguru Amazon Data API."""

from .server import build_server
from .spec import api_info, endpoints, load_spec

__version__ = "1.1.18"

__all__ = ["build_server", "api_info", "endpoints", "load_spec", "__version__"]
