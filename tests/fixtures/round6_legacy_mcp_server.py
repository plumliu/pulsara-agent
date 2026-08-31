"""Minimal MCP 1.x-style stdio peer for negotiated compatibility tests."""

from __future__ import annotations

import json
import sys


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        continue
    if method == "server/discover":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": "Invalid request parameters",
                },
            }
        )
    elif method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {
                            "subscribe": False,
                            "listChanged": False,
                        },
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "pulsara-round6-legacy-fixture",
                        "version": "1.0.0",
                    },
                    "instructions": "Legacy fixture instructions.",
                },
            }
        )
    elif method == "tools/list":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "legacy_echo",
                            "description": "Return one input string.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "openWorldHint": False,
                            },
                        }
                    ]
                },
            }
        )
    elif method == "tools/call":
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"legacy:{arguments.get('text', '')}",
                        }
                    ],
                    "isError": False,
                },
            }
        )
    elif method == "resources/list":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"resources": []},
            }
        )
    elif method == "resources/templates/list":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
    elif method == "prompts/list":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"prompts": []},
            }
        )
    else:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
