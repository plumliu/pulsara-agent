---
name: pulsara-code-mode
description: Use for ROOT agent assignments, continuations and waits, including a single assignment, and for composing ordinary tools and optional semantic judgments in JavaScript. Available to ROOT in bypass-permissions mode; native query, message and stop controls remain independently available.
---

# Pulsara Code Mode

## Compose tools and agent work

ROOT uses Code Mode to create agents, assign or continue work, and wait for their
messages or results, including a single assignment. Connect ordinary tools and
agent operations in the same script; a short script can be enough.

ROOT can also use native `query_agents`, `send_agent_message` and `stop_agent`
to inspect, supplement or stop work directly while scripts run. These are normal
control entry points, available independently of Cell capacity and the Code Mode
runtime. Their `tools.*` counterparts operate on the same Agent and Work targets.
New assignments and waits use Code Mode; native messages supplement active work.
Subagents keep native `query_agents`, `send_agent_message` and `wait_agent` for
their own visible work and known teammates. They do not run scripts.

For ordinary tools offered in both forms, native calls and `tools.name(...)`
share their arguments and operate on the same files, processes and saved results.
Use returned Agent/Work targets across scripts and native controls. Query existing
agents when their findings may help, then reuse their actual AGENT targets with
`tools.run_agent`. See [agent reuse](references/agent-orchestration/examples.md#4-reuse-an-existing-agent-in-a-scripted-workflow).

## Start with one script

Pass JavaScript through `exec_cell`'s `code`, or use `path` for an existing script.
Top-level `await` is supported. Call available tools as `tools.name(arguments)`;
the resolved value is the public JSON object or text, without a `.data` wrapper.

For a focused investigation, read project context and assign one agent:

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" }, limit: 120
});
const investigation = await tools.run_agent({
  name: "Entry-point investigator",
  instruction: `Explain how the project's main entry point initializes.
Read the relevant source files. This README excerpt is starting context:
${readme.content}`
});
console.log({ agent: investigation.agent, work: investigation.work });
await tools.wait_agent.works({ targets: [investigation.work] });
const result = await tools.query_agents({ target: investigation.work });
console.log(result);
```

To continue with the same agent, pass `investigation.agent` as `target` in a later
`tools.run_agent` call. Across scripts, copy the actual target from output or obtain
it with `query_agents`; each Cell has fresh variables.

`console.log(...)` actively sends selected values to ROOT. Each call is one
independent input, including repeated text. Internal tool results stay in the
script; Work finals are not automatically reported to ROOT. Agent messages retain
their active-turn delivery. ROOT receives script output at a safe point, or as a
queued new turn when idle. Output requests processing; it does not grant permission.

## Use semantic judgments in the same script

Use `tools.decide({ condition, inputs })` wherever the script needs a true/false
judgment about natural-language meaning, before, between or after other tools or
agent operations. Write one clear yes/no condition and choose the material for
each independent judgment. An input can combine tool results, attributed dialogue,
documents or agent reports; include the context that condition needs.

The only arguments are `condition` and `inputs`. The result is
`{ decisions: boolean[] }` in input order; one text still uses `inputs: [text]`.
Use the booleans in expressions, filters, branches or loop conditions, then continue
with the required tools. Keep source targets and Agent/Work references on the
original objects. Intermediate results can stay in the script until ROOT needs them.

Every call belongs inside `if (tools.decide.available())`. This local synchronous
method returns a boolean fixed at Cell startup; it makes no tool or network call.
When false, output the pending condition, material and relevant Work references
for ROOT to continue. Reuse work already completed and proceed without asking the
user to configure Decision as a prerequisite. A new Cell picks up configuration
changes; an actual call still handles settings changes and service errors normally.

Use JavaScript for exact comparisons, counts, dates and string matching. ROOT can
judge small amounts of material already in its context directly. Read missing
context before a semantic judgment; for example, include the preceding proposal
with “yes, do that.” Process paged sources as the task needs. See the
[interface](references/tool-calling/patterns.md#semantic-conditions) and
[worked examples](references/tool-calling/examples.md#4-identify-requirements-in-saved-discussions).

## Follow the running script

- Each `exec_cell` immediately returns accepted with its CELL target. Its variables
  are private to that script. Output actual targets needed by later scripts.
- Outputs arrive actively. Do not resubmit a script to retrieve logs. Native
  `manage_cell` with `action:"wait"` observes terminal state or its observation
  timeout; output does not finish that wait or appear in it.
- Native `query_cells` lists retained live cells, or reads a selected CELL's state
  and frozen source. Reading does not consume output. Historical source remains
  available through the original exec record after the live Cell retires.
- ROOT uses `tools.wait_agent` for AGENT messages/current work and
  `tools.wait_agent.works` for exact Work outcomes. These return states and targets;
  read selected finals or diagnostics with `query_agents`. Dependencies/materials
  pass results directly between Work inputs without ROOT relaying them.
- `manage_cell` with `action:"stop"` stops further script execution and submissions.
  Accepted agent work continues; use native `stop_agent` to stop that work too.

A result's `next` call continues its source. When native output omits part of an
accepted result, its `read` call reads that frozen body with `read_file` and the
exact SESSION_ENTRY target. Copy that target into `write_file.source` to create a
new file containing the whole body. These operations never repeat the original
action. A complete saved body covers that invocation's returned content, which
may be only one source page.

The environment provides JavaScript, Promises, `tools`, and `console.log`.
Access files, processes and remote services through tools; Node modules, `fetch`,
imports and timers are not available. Code Mode is ROOT-only in bypass-permissions
mode. Decision is optional and independent of memory reranking; its absence does
not disable other tools or agent work.

## Reference guide

| Need | Reference |
| --- | --- |
| JS tools, semantic conditions, sequential/parallel calls, MCP | [Tool patterns](references/tool-calling/patterns.md) |
| Search/read, follow a process, save output, judge text and continue | [Tool examples](references/tool-calling/examples.md) |
| Errors, active output, saved bodies, images | [Results and errors](references/tool-calling/results-and-errors.md) |
| Agent reuse, teammate references, messages, dependencies, waiting and stopping | [Coordination](references/agent-orchestration/coordination.md) |
| Specialists, teammates, dependencies, existing-agent reuse and native control | [Agent examples](references/agent-orchestration/examples.md) |
| Investigation, review, revision and implementation with semantic branches | [Workflows](references/agent-orchestration/workflows.md) |
