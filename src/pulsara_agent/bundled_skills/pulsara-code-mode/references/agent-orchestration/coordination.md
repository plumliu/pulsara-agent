# Agent coordination

An Agent is a reusable conversation. A Work is one assignment. Omit `target` to
create an Agent, or pass an AGENT Target to queue work in its existing context.
Assignments to one Agent run in order. Different Agents can work concurrently.

## Submit A, then B depends on A

Print the accepted records first so ROOT gets both Work Targets through Cell output.
To observe progress, wait for one unfinished Work to end at a time. On timeout,
the script hands the observed states back to ROOT and the accepted Work continues.

```js
const accepted = await tools.run_agent([
  {
    work_title: "A: Investigate the parser",
    instruction: "Investigate the parser and report evidence.",
    materials: [{kind: "CONTEXT_MATERIAL", turns: 1}]
  },
  {
    work_title: "B: Review the findings",
    instruction: ({works}) => "Review the findings supplied for " + JSON.stringify(works[0]),
    depends_on: ({works}) => [works[0]]
  }
]);
console.log(accepted);

function unfinishedWorks(result) {
  return result.content
    .filter(part => part.type === "object")
    .map(part => part.value)
    .filter(record => record.work_status === "queued" || record.work_status === "running")
    .map(record => record.work);
}

let pending = unfinishedWorks(accepted);
while (pending.length > 0) {
  const observed = await tools.wait_agent({works: pending, settle: "first", timeout_seconds: 60});
  console.log(observed);
  if (observed.meta.wait_reason === "timeout") break;
  pending = unfinishedWorks(observed);
}
```

`depends_on` starts B after A completes successfully and delivers A's result once,
even if also listed in `materials`. A failed dependency stops the dependent Work.
A CONTEXT_MATERIAL Target selects 1–3 ROOT turns. An ended WORK Target selects its
result. Both can be supplied as materials. ROOT history is omitted by default.
Instruction and dependency callbacks use preallocated `{agents, works}` Targets
synchronously, before dispatch. Keep tool calls out of these callbacks.

## Inspect the Work you care about

For B's current progress, call native `query_agents({target: workB})` with B's
WORK Target from the accepted records. The same query is available in a later
Cell. Copy the actual `work_id` into the example:

```js
const workB = {kind: "WORK", work_id: "queue:<B>"};
console.log(await tools.query_agents({target: workB}));
```

Read `work_status` for the assignment state and `final` or `diagnostic` when
present. A queued Work may be waiting for a dependency or an available Agent.
Use native `manage_cell` with `action:"wait"` when a later step needs the script
to end. Its `cell_status` tracks the script and `pending_calls` counts unsettled
script tool calls. Use the Work Target to inspect progress while the script waits.

A successful `wait_agent` reports `meta.wait_reason` and every input target in
order, even on `first` or timeout. Continue waiting with the Work Targets whose
latest `work_status` is `queued` or `running`. With the `agents` branch, the
condition is agent idleness or a message from a selected Agent. Message bodies
arrive separately. Use `stop_agent` to stop delegated work. A Cell can end or
a wait can be cancelled while accepted Work continues.

## Reuse an Agent in another Cell

Copy the AGENT Target from an accepted record to submit a new Work in that
Agent's conversation. Each Cell starts with its own JavaScript variables.

```js
const next = await tools.run_agent([{
  target: {kind: "AGENT", agent_id: "session:<copy the accepted agent_id>"},
  instruction: "Review the follow-up changes.",
  materials: [{kind: "CONTEXT_MATERIAL", turns: 1}]
}]);
console.log(next);
```
