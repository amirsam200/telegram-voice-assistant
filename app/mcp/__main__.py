"""Run the MCP server over stdio: python -m app.mcp

stdout carries the MCP protocol, so logs go to stderr only (JSON lines).
"""

from app.logging_setup import configure_logging
from app.mcp.server import create_server

# Before create_server: FastMCP only adds its own log handler when none is configured.
configure_logging()
create_server().run("stdio")
