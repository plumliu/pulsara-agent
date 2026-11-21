---
name: pulsara-code-mode
description: Compose tool calls in JavaScript Cells. Available to ROOT in bypass-permissions mode.
---

# Pulsara Code Mode

**Read the relevant [JS API pages](references/tool-calling/api/index.md) before writing a Cell.**
Each page gives the arguments, successful result and any tool-specific failure fields.

Start with native `exec_cell({code})` or `exec_cell({target:{kind:"PATH",path}})`.
Each Cell has its own variables and supports top-level `await`.
Imports, Node modules, fetch and timers are unavailable.

A successful `await tools.name(args)` returns the complete saved business value in memory.
Read its `content` directly in JavaScript. `NEXT` is reserved for native result pagination.
Send values to ROOT through `console.log` or a non-`undefined` top-level `return`.
Pass image content items intact.

```js
const notes = await tools.read_file({target:{kind:"PATH",path:"README.md"}});
const picture = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({content:[...notes.content, ...picture.content]});
```

`exec_cell` returns a CELL Target when the script is accepted. Script output arrives separately.
Use native `query_cells` for the current Host's Cell list or a CELL's frozen source and status.
Use native `manage_cell` with `action:"wait"` when a next step depends on the script ending,
or with `action:"stop"` to stop a Cell held by the current Host. Use `query_agents` with a
WORK Target to check delegated work.

- [Results, errors and images](references/tool-calling/results-and-errors.md)
- [Discover an MCP tool before calling it](references/tool-calling/patterns.md)
- [Dispatch and wait for Agent work](references/agent-orchestration/coordination.md)
