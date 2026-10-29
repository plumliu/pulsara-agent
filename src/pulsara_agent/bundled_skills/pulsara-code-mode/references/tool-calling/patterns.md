# Tool patterns

For complete scenarios with inputs and result handling, see
[Ordinary tool examples](examples.md).

## Use the native arguments

`tools.read_file(args)` and native `read_file` are two entry points to the same
operation, with the same argument contract and workspace state. This also lets
you start a terminal process natively and follow its process target in JS, or
read a script-produced artifact with a native tool. Consult each tool definition
for its fields. JavaScript adds variables, conditions, loops, and Promise
composition; it does not add a common response wrapper or a separate workspace.

The JS subset is:

| Purpose | Tools |
| --- | --- |
| Files | `read_file`, `write_file`, `edit_file`, `find_files`, `search_content` |
| Processes | `terminal`, `terminal_process` |
| Artifacts | `artifact_read`, `artifact_export` |
| Conversation history | `search_sessions`, `search_session_content`, `read_session_content` |
| Capability discovery | `list_capabilities`, `inspect_capability` |
| MCP | `use_new_mcp_tool`, `read_mcp_resource`, `get_mcp_prompt`, and direct MCP tools in the script's initial directory |
| Agents | `run_agent`, `list_agents`, `inspect_agent`, `send_agent_message`, `wait_agent`, `stop_agent` |

JS also has `tools.run_agent.batch(...)` and `tools.wait_agent.works(...)`, described
in [Coordination](../agent-orchestration/coordination.md). Other native tools,
including `view_image`, `terminal_monitor`, memory, Plan, capability management,
and the `exec` controls, are called outside the script.

## Sequential and parallel calls

`await` makes a result available to a dependent call. For independent calls,
`Promise.all` can collect their results together:

```js
const paths = ["README.md", "pyproject.toml"];
const excerpts = await Promise.all(paths.map(async path => ({
  path,
  file: await tools.read_file({ target: { kind: "PATH", path }, limit: 100 })
})));
console.log(excerpts.map(item => ({
  path: item.path,
  content: item.file.content,
  truncated: item.file.truncated
})));
```

Result fields can feed later calls: file edits may use the read result's
`content_revision`, and paged results provide continuation fields. A result can also become an agent's `instruction` or an active agent's
message, using a string or `JSON.stringify(value)`.

`Promise.allSettled` is useful when each independent outcome matters even if
another call fails. `Promise.all` rejects on a failure; it does not cancel the other
already submitted calls. See [Results and errors](results-and-errors.md).

Tools may finish their own observation while the underlying activity continues.
For example, `terminal` can produce a running process result. Follow its actual
process target with `terminal_process` when completion or later output is needed.

## Discover an MCP operation

For a listed MCP tool, call `tools.inspect_capability` with its exact target.
Follow the response's `invocation.mode`: use the indicated direct tool or call
`tools.use_new_mcp_tool` with the provided target and arguments. JS inspection
can provide the callable reference directly; a preceding native inspection is
not required. For names that are not JavaScript identifiers, use
`tools[toolName](arguments)`.

Use the actual discovered targets for resources, resource templates, and prompts
as well. A script keeps its initial direct-tool directory. Newly discovered tools
can be accessed through inspection and the MCP meta tool. Actual capability
changes can invalidate a reference; inspect the current capability when the
reported failure indicates that change.
