# Discover and call MCP tools

Before writing a business Cell, obtain the remote tool's parameters, any
available result schema and its invocation Target. Query natively, or run a
preparation Cell and send the result to ROOT:

```js
const matches = await tools.query_capabilities({kind:"MCP",query:"find issues"});
console.log(matches);
```

Search may return service records as well as tool details. Query a service's
MCP_CONNECTION Target for its tool directory, or a MCP_TOOL Target for exact tool
details. Pass the actual `invocation.target` (MCP_TOOL_REF) and matching
`arguments` to `tools.call_mcp_tool`. The reference works in native calls and
across Cells in this session. If it becomes stale, query again.

Use the ordered `content` directly. If supplied, `output_schema` describes the
successful `structuredContent` value only. Without it, inspect the result before
accessing unknown fields. An uncertain side effect is a reason to check what
happened before calling again.
