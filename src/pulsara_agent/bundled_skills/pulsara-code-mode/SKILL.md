---
name: pulsara-code-mode
description: Compose Pulsara tools and agents in JavaScript, including continuing work begun through native tools. Use for processing results, parallel calls, or expanding an investigation into a multi-step workflow when ROOT has Code Mode available in bypass-permissions mode.
---

# Pulsara Code Mode

## One tool system, two entry points

For tools available in both forms, native calls and `tools.name(...)` execute the
same operations on the same files, processes, artifacts, and agents. Either entry
point can suit a single call; even a one-line `exec_cell` is useful. JavaScript also
provides variables, conditions, and loops for connecting calls. The two entry
points can be mixed as the task develops.

An agent created through native `run_agent` can join a later scripted workflow:
pass its existing AGENT target to `tools.run_agent` or a batch member's `target`.
Its conversation continues. Conversely, native `send_agent_message`,
`query_agents`, or `run_agent` can act on an agent first assigned through JS. Its identity,
history, and Work targets do not change when you switch entry points.

When a task grows, `query_agents` can help identify existing
agents whose knowledge is useful. They can work alongside new agents in a later
assignment. See [cross-entry examples](references/agent-orchestration/examples.md#4-expand-native-research-into-a-scripted-workflow).

## Start with one script

Pass JavaScript through `exec_cell`'s `code`, or use `path` for an existing script file.
Top-level `await` is supported. Call available tools as `tools.name(arguments)`,
using the same parameters as their native tools. The resolved value is the tool's
public JSON object or text, without an extra `.data` wrapper.

`console.log(...)` exposes selected values to the ROOT model. Internal tool
results stay inside the script unless you output them; agent messages and
completion notifications still use their normal channels. Logs are formal
output, delivered through `exec_cell` or `manage_cell` with `action:"wait"`, including while a script runs.

For example, continue an earlier native investigation using newly read project
context. Replace the illustrative target with the actual `agent` target from
that native call:

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" },
  limit: 120
});
const investigation = await tools.run_agent({
  target: { kind: "AGENT", agent_id: "session:<actual-agent-id>" },
  instruction: `Using your earlier findings, explain how the project's main entry point initializes.
Read the relevant source files. This README excerpt is starting context:
${readme.content}`
});
console.log({ agent: investigation.agent, work: investigation.work });
console.log(await tools.wait_agent.works({ targets: [investigation.work] }));
```

Ordinary tool data becomes an instruction for the same agent. Omit `target` when
the task instead calls for a new agent. Adapt paths and instructions to the task.

## Follow the running script

- Each `exec_cell` starts an independent script with its own variables. Log identifiers
  or data you will need in a later script.
- An observation timeout leaves the script running. Use native `manage_cell` with `action:"wait"` and its
  complete CELL `target` for subsequent output; do not submit the same script again to continue it.
  `yield_time_ms` controls observation time, not execution lifetime.
- Use native `query_cells` without a target to list retained cells, or with a CELL
  target to read its state and frozen source. This does not consume execution output.
- Read further pages when `has_more` is true. A terminal state with
  `pending_calls` still needs later observation for outstanding tool outcomes.
- `manage_cell` with `action:"stop"` stops further script execution and submissions. Accepted agent work
  continues; use `stop_agent` when that work should stop too.

If one output record is too large for a page, `exec_cell` or `manage_cell` with `action:"wait"` gives ROOT an
`output_artifact.target`. Pass that exact target to native `read_artifact` or
`export_artifact`. For JS processing, start a new `exec_cell` and explicitly include
the target in its tool arguments; observation results are not injected into
script variables. The artifact contains that output record as JSON, not the
whole script's output.

The environment provides JavaScript, Promises, `tools`, and `console.log`.
Access files, processes, and remote services through tools; Node modules,
`fetch`, imports, and timers are not available. Code Mode is ROOT-only in bypass
mode. Subagents use their native tools.

## Reference guide

| Need | Reference |
| --- | --- |
| Available JS tools, sequential and parallel calls, MCP | [Tool patterns](references/tool-calling/patterns.md) |
| Worked examples: search then read, follow a process, inspect and export its log | [Ordinary tool examples](references/tool-calling/examples.md) |
| Errors, output pages, artifacts, images | [Results and errors](references/tool-calling/results-and-errors.md) |
| Agent reuse, batch teammate references, messages, dependencies, waiting and stopping | [Coordination](references/agent-orchestration/coordination.md) |
| Worked examples: specialists, teammates, dependencies, native → JS reuse and JS → native control | [Agent examples](references/agent-orchestration/examples.md) |
| Investigation → review → revision → implementation → code review; continuing after interruption | [Workflows](references/agent-orchestration/workflows.md) |
