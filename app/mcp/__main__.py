"""Run the MCP server over stdio: python -m app.mcp

stdout carries the MCP protocol, so logs go to stderr only.
"""

import logging
import sys

from app.mcp.server import create_server

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

create_server().run("stdio")
