---
name: pulsara-code-mode
description: Compose tool calls in JavaScript Cells. Available to ROOT in bypass-permissions mode.
---

# Pulsara Code Mode

**Read the relevant [JS API pages](references/tool-calling/api/index.md) before writing a Cell.**
Each page gives the arguments, successful result and any tool-specific failure fields.

Start with native `exec_cell({code})` or `exec_cell({target:{kind:"PATH",path}})`.
Top-level `await` works. Each Cell has private variables. Imports, Node modules, fetch and timers are unavailable.

Await `tools.name(args)` to get its complete saved business value.
Use `console.log` to send selected values to ROOT. Pass image content items intact.

```js
const notes = await tools.read_file({target:{kind:"PATH",path:"README.md"}});
const picture = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({content:[...notes.content, ...picture.content]});
```

Use native `query_cells` to list Cells held by the current Host. Pass a CELL Target for frozen source
and live or saved status in this session. Unconfirmed final status is `unknown`.
Native `manage_cell` waits for or stops a live Cell. Output arrives separately.

- [Results, errors and images](references/tool-calling/results-and-errors.md)
- [Discover an MCP tool before calling it](references/tool-calling/patterns.md)
- [Dispatch and wait for Agent work](references/agent-orchestration/coordination.md)
