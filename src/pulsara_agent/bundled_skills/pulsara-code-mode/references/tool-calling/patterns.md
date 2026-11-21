# Discover and call MCP tools

Before writing a business Cell, obtain the remote tool's parameters, any
available result schema and its invocation Target. Query natively, or run a
preparation Cell and send the result to ROOT:

```js
const matches = await tools.query_capabilities({kind:"MCP",query:"find issues"});
console.log(matches);
```

Search may return configured services and tool details. Use an MCP_CONNECTION
Target to list a service's tools, or an MCP_TOOL Target to inspect one tool.
For invocation, take the MCP_TOOL_REF from a tool record's `invocation.target`
and pass it with `arguments` matching `input_schema` to `tools.call_mcp_tool`.
If a search throws because part of the directory was unavailable, check
`err.result` for known hits and usable invocation references. References work in
native calls and across Cells in this session. Query again if a reference becomes stale.

Use the ordered `content` directly. If supplied, `output_schema` describes the
successful `structuredContent` value only. Without it, inspect the result before
accessing unknown fields. An uncertain side effect is a reason to check what
happened before calling again.
