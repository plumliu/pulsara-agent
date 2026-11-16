---
name: pulsara-code-mode
description: Compose ROOT tool calls, explicit text/image output, reusable Agent work, and optional semantic judgments in independent JavaScript Cells. Requires bypass-permissions.
---

# Pulsara Code Mode

Use native `exec_cell({code})`, or `exec_cell({target:{kind:"PATH",path}})` to
run an independent script. Top-level await and return are available. Each Cell
has private variables; copy actual Targets from output or query them in a later
Cell. Node modules, imports, fetch, and timers are unavailable.

**Before writing a business Cell, read the relevant [API pages](references/tool-calling/api/index.md)**
for its arguments, complete return fields and errors. Select the APIs you need.

## Compose tools and agent work

`tools.name(args)` returns the tool's complete saved business value. Its declared
`content` is an ordered array of `{type,value}` items. A text item's value is a
string, an image item's value is an IMAGE_REF Target, and an object item's value
is the declared JSON object. Tool failures throw; `err.result`, when present,
contains the actual complete business value. A save or delivery failure can throw
while that value still has `meta.status:"SUCCESS"`; read the error message as
well as the business outcome. Do not repeat an action to recover its response.

Use `console.log(...)` or a top-level return to disclose selected values to ROOT.
Actual image items are recognized recursively; bare Targets, paths and strings
remain text. Use the declared result fields directly:

```js
const notes = await tools.read_file({target:{kind:"PATH",path:"README.md"}});
const picture = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({content:[...notes.content, ...picture.content]});
```

## Start with one script

`tools.run_agent` accepts an ordered array, including a one-item array. Omit
`target` to create an Agent; supply an actual AGENT Target to append a Work to an
existing Agent. `work_title` is optional display text. Identities are Targets,
not names or titles.

```js
const accepted = await tools.run_agent([
  {instruction:"Investigate the parser and report evidence.",
   work_title:"Investigate parser", materials:[{kind:"CONTEXT_MATERIAL",turns:1}]},
  {instruction:"Review the investigation's reasoning.",
   depends_on:({works})=>[works[0]]}
]);
const works = accepted.content.map(part=>part.value.work);
console.log(accepted);
const observed = await tools.wait_agent({works,settle:"all",timeout_seconds:60});
console.log(observed);
console.log(await tools.query_agents({target:works[0]}));
```

Each result object contains `agent`, `work`, and `work_status`, in assignment
order. Acceptance does not imply completion. A wait can time out or wake on a
message; check `meta.wait_reason` and every record before treating work as done.
Query a retained WORK for its current state and full
instruction and actual final answer or diagnostic. Reuse its `agent` Target for
follow-up work. An Agent's existing context remains in order.

`materials` can explicitly select ROOT history with
`{kind:"CONTEXT_MATERIAL",turns:1|2|3}` or an already ended WORK. Selection works
for both new and reused Agents and freezes the history at dispatch time. No
history is copied by default. `depends_on` requires successful completion; a WORK
also listed in materials is delivered only once as a dependency. The main
instruction is separate from all history and Work materials. Independent
teammates can coordinate by messages; dependencies wait for successful results.
Do not make a Work wait for a reply from one blocked on its own completion.

Instruction and depends_on can be synchronous functions over preallocated
`{agents,works}` references. They may not call tools or return promises. References
identify the current batch, not reports that have not happened yet.

`tools.wait_agent({works})` observes exact Works, including queued ones.
`tools.wait_agent({agents})` observes empty running and queued workloads. Provide
one branch, not both. Both support `settle:"all"|"first"` and a 0–300 second
observation timeout. Every SUCCESS returns all requested records in input order.
Read `meta.wait_reason`: condition_met, timeout, or AGENT-only message_available.
Messages do not complete a WORK wait and message notification does not consume
its body. Native query_agents, send_agent_message, and stop_agent remain direct
ROOT controls, independent of Cell slots. Subagents have native query/message
and wait_message; they have no Code Mode API.

## Use semantic judgments in the same script

```js
const inputs = ["The export failed.", "The file is correct."];
if (tools.decide.available()) {
  try {
    const result = await tools.decide({condition:"Does this report an export failure?",inputs});
    const decisions = result.content[0].value.decisions;
    console.log(inputs.filter((_,i)=>decisions[i]));
  } catch (err) {
    console.log(err.result !== undefined ? err.result : String(err.message ?? err));
  }
} else {
  console.log({condition:"Does this report an export failure?",inputs});
}
```

Read enough material before judging it; a decision cannot supply missing facts.
Decision configuration is optional: continue from readable evidence when absent.
available() is local, synchronous, and fixed at Cell startup; it is not a service
health check. Actual calls use the current saved Decision connection. Empty inputs
need no configuration. Decisions are booleans in input order; a failure never
substitutes false or a successful prefix. Use JavaScript for exact comparisons.

## Follow the running script

Native query_cells lists currently retained Cells or reads a selected Cell's full
frozen source. Native manage_cell waits for state or stops further script
execution and submissions. Neither consumes or replays output. Stopping a Cell
preserves accepted effects and does not stop its accepted Agent Works. Use
stop_agent for those Works. Already queued output retains its normal lifecycle;
explicit stop suppresses later unaccepted output and late-result supplements.

See [results and failures](references/tool-calling/results-and-errors.md) for
actual err.result handling, image failures and stopped scripts. Cell output has
no read_file or export Target; native NEXT belongs to saved tool results.

## Reference guide

- [Tool calling patterns](references/tool-calling/patterns.md)
- [Results, failures and explicit images](references/tool-calling/results-and-errors.md)
- [Tool examples](references/tool-calling/examples.md)
- [Agent coordination](references/agent-orchestration/coordination.md)
- [Agent examples](references/agent-orchestration/examples.md)
- [Workflows](references/agent-orchestration/workflows.md)
