"""Streamable HTTP form of the bounded Round 6 MCP fixture."""

from __future__ import annotations

import os

from round6_mcp_server import server


if __name__ == "__main__":
    server.run(
        "streamable-http",
        host="127.0.0.1",
        port=int(os.environ["PULSARA_ROUND6_MCP_PORT"]),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=False,
    )
