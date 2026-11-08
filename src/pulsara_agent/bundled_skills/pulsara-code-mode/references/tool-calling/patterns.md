# Tool patterns

For complete scenarios with inputs and result handling, see
[Ordinary tool examples](examples.md).

## Use each tool's arguments

`tools.read_file(args)` and native `read_file` are two entry points to the same
operation, with the same argument contract and workspace state. This also lets
you start a terminal process natively and follow its process target in JS, or
read a formally published script-output entry with native read_file. Consult each tool definition
for its fields. JavaScript adds variables, conditions, loops, and Promise
composition; it does not add a common response wrapper or a separate workspace.

The JS directory includes:

| Purpose | Tools |
| --- | --- |
| Files | `read_file`, `write_file`, `edit_file`, `find_files`, `search_content` |
| Processes | `exec_process`, `query_processes`, `manage_process` |
| Conversation history | `search_sessions`, `search_session_content`, `read_session_content` |
| Capability discovery | `query_capabilities` |
| MCP | `use_new_mcp_tool`, `read_mcp_resource`, and direct MCP tools in the script's initial directory |
| Agents | `run_agent`, `query_agents`, `send_agent_message`, `wait_agent`, `stop_agent` |
| Semantic conditions | `decide`, with its local synchronous `available()` method |

JS also has array-based `tools.run_agent([...])` and `tools.wait_agent({agents: [...], until: "idle"})`, described
in [Coordination](../agent-orchestration/coordination.md). ROOT assigns, continues
and waits for agent work through these JS operations, including single calls.
Native `query_agents`, `send_agent_message` and `stop_agent` remain independent
controls for the same work.
`decide` is a script API; `available()` is a local method, not another tool call.
Other native tools,
including `view_image`, memory, Plan, capability management,
and the `exec_cell`, `query_cells`, and `manage_cell` controls, are called outside the script.

## Semantic conditions

```ts
tools.decide({ condition: string, inputs: string[] }): Promise<{ decisions: boolean[] }>
tools.decide.available(): boolean
```

Give one explicit yes/no condition and an ordered array of text, including the
context needed for each item. A single input uses the same array interface.
Only `condition` and `inputs` are accepted. `decisions[i]` corresponds to
`inputs[i]`; values are booleans, without scores or explanations. Empty inputs
return an empty decisions array without a model request. Empty strings and
duplicate inputs retain their positions. Format structured material explicitly,
for example with `JSON.stringify`, keeping speakers and relevant context.

Guard every call with `if (tools.decide.available())`. The method is synchronous,
does not invoke a tool or network request, and returns configuration availability
captured when the Cell started. The configured Jev-like Decision connection is
independent of memory reranking. New Cells read updated availability; an actual
call still uses the current settings and may fail if they changed.

Use the returned booleans to select any next computation, tool or agent operation.
For exact counts, field checks, dates and string matches, use JavaScript instead.
Keep source targets and Work references with the original objects. If unavailable,
output the condition and material with those references for ROOT to continue;
ordinary tasks do not need Decision configuration to proceed. Do not treat missing
configuration or a failed call as a semantic `false`.

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
  next: item.file.next
})));
```

Result fields can feed later calls: file edits may use the read result's
`content_revision`, and paged results provide complete next calls. A result can also become an agent's `instruction` or an active agent's
message, using a string or `JSON.stringify(value)`.

`Promise.allSettled` is useful when each independent outcome matters even if
another call fails. `Promise.all` rejects on a failure; it does not cancel the other
already submitted calls. See [Results and errors](results-and-errors.md).

Tools may finish their own observation while the underlying activity continues.
For example, `exec_process` can produce a running process result. Follow its actual
process target with `manage_process` when completion or later output is needed.

## Discover an MCP operation

For a listed MCP tool, call `tools.query_capabilities` with its exact target.
Follow the response's `invocation.mode`: use the indicated direct tool or call
`tools.use_new_mcp_tool` with the provided target and arguments. JS inspection
can provide the callable reference directly; a preceding native inspection is
not required. For names that are not JavaScript identifiers, use
`tools[toolName](arguments)`.

Use the actual discovered targets for resources, resource templates, and prompts
as well. Prompt queries return metadata only; users expand prompts in the composer
slash menu. A script keeps its initial direct-tool directory. Newly discovered tools
can be accessed through inspection and the MCP meta tool. Actual capability
changes can invalidate a reference; inspect the current capability when the
reported failure indicates that change.
