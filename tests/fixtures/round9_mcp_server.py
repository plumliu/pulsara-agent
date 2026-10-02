"""Bounded local MCP fixture for the Round 9 real-provider dogfood.

The fixture deliberately exposes three shapes:

* one small tool that fits the cold native cohort;
* a bounded cohort large enough to force the all-meta aggregate branch; and
* one canonical-valid schema that the OpenAI native projection cannot lower
  without changing its meaning.

No request body is logged or persisted by this fixture.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from mcp.server.mcpserver import MCPServer
import mcp_types as types


server = MCPServer("pulsara-round9-fixture")
_READ_ONLY = types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
)


@server.tool(annotations=_READ_ONLY)
def direct_echo(text: str) -> str:
    """Return one bounded string for the cold DIRECT path."""

    return f"direct:{text}"


def _bulk_tool(index: int) -> Callable[[str], str]:
    def invoke(text: str) -> str:
        """Return one bounded string for the forced META cohort."""

        return f"bulk-{index:02d}:{text}"

    invoke.__name__ = f"bulk_{index:02d}"
    return invoke


for _index in range(48):
    server.tool(name=f"bulk_{_index:02d}", annotations=_READ_ONLY)(
        _bulk_tool(_index)
    )


@server.tool(annotations=_READ_ONLY)
def native_incompatible_echo(text: str) -> str:
    """Return one bounded string through the schema-incompatible META path."""

    return f"incompatible:{text}"


# A root-level simultaneous oneOf + anyOf is valid JSON Schema (the two
# constraints are conjunctive), but the shared OpenAI native-tool projection
# intentionally rejects this non-portable shape.  Keep the ordinary function
# metadata so the MCP server still validates and executes the exact argument.
_incompatible = server._tool_manager.get_tool("native_incompatible_echo")  # noqa: SLF001
if _incompatible is None:  # pragma: no cover - module construction invariant
    raise RuntimeError("Round 9 incompatible fixture tool was not registered")
_base = deepcopy(_incompatible.parameters)
_branch = {
    "type": "object",
    "properties": deepcopy(_base.get("properties", {})),
    "required": ["text"],
    "additionalProperties": False,
}
_incompatible.parameters = {
    **_base,
    "oneOf": [deepcopy(_branch)],
    "anyOf": [deepcopy(_branch)],
}


if __name__ == "__main__":
    server.run("stdio")
