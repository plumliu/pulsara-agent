# Agent examples

Use these building blocks with ordinary tools in the same Cell. Choose the
assignments that the user's task needs; the roles are examples, not required stages.

## 1. One assignment, then a continuation

```js
const investigation = await tools.run_agent([
  {
    work_title: "Investigate parser behavior",
    instruction: "Trace empty-input handling in the parser. Read source and tests; report the observable behavior. Do not edit files."
  }
]);
console.log({ agents: investigation.agents, works: investigation.works });
const investigationWait = await tools.wait_agent({
  agents: [...new Map(investigation.agents.map(target => [target.agent_id, target])).values()],
  until: "idle"
});
if (investigationWait.state !== "settled") console.log({ observation: investigationWait });
console.log(await tools.query_agents({ target: investigation.works[0] }));
```

`agents[0]` identifies the conversation. `works[0]` identifies this investigation.
The following continuation uses the first example's variable in the same Cell:

```js
const followup = await tools.run_agent([
  {
    target: investigation.agents[0],
    work_title: "Check whitespace test coverage",
    instruction: "Using your investigation, explain whether the tests distinguish empty input from whitespace-only input. Read additional tests as needed."
  }
]);
const followupWait = await tools.wait_agent({
  agents: [...new Map(followup.agents.map(target => [target.agent_id, target])).values()],
  until: "idle"
});
if (followupWait.state !== "settled") console.log({ observation: followupWait });
console.log(await tools.query_agents({ target: followup.works[0] }));
```

The Agent stays the same; the Work is new. Its title describes this assignment.
A new Cell needs the actual target from the first output or a fresh query, not
the old `investigation` variable. See the cross-Cell procedure below.

## 2. Two active teammates

```js
const team = await tools.run_agent([
  {
    work_title: "Inspect backend response",
    instruction: ({ agents }) =>
      `Inspect the response fields and errors for the requested API change.
The client investigator is ${JSON.stringify(agents[1])}.
Use native send_agent_message to compare findings while both are active.
Report agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  },
  {
    work_title: "Inspect client expectations",
    instruction: ({ agents }) =>
      `Inspect client expectations for the requested API change.
The backend investigator is ${JSON.stringify(agents[0])}.
Use native send_agent_message to compare findings while both are active.
Report agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  }
]);
console.log({ agents: team.agents, works: team.works });
const teamWait = await tools.wait_agent({
  agents: [...new Map(team.agents.map(target => [target.agent_id, target])).values()],
  until: "idle"
});
if (teamWait.state !== "settled") console.log({ observation: teamWait });
const findings = await Promise.all(team.works.map(target => tools.query_agents({ target })));
console.log(findings);
```

Both assignments are independent and can run as capacity permits. References do
not make the other Agent active: sending reports `not_started` or `inactive` if
it cannot accept a message. Then report the outstanding question to ROOT, which
can continue the appropriate Agent with a new Work.

## 3. A reviewer that needs a finished investigation

Continue the two Agents from example 2 with new work. In the same Cell, `team` is
already available. In another Cell, first declare `team` with the complete
`agents` array copied from that example's output, or recover the selected Targets
by querying their actual work. Do not refer to an undeclared variable from the
earlier Cell. The two array positions below are the chosen retained Agents, not
positions in an unrelated query page.

```js
const phase = await tools.run_agent([
  {
    target: team.agents[0],
    work_title: "Supplement concrete acceptance cases",
    instruction: "Continue your investigation. Supply minimal inputs, current outputs, expected results grounded in the requirements, and behavior a fix must preserve. Do not edit files."
  },
  {
    target: team.agents[1],
    work_title: "Review supplementary acceptance cases",
    depends_on: ({ works }) => [works[0]],
    instruction: "Review the prerequisite final supplied as input against the requirements and actual code. Check the expected results and preservation cases. Report contradictions with evidence, or explain why the cases are sound. Do not edit files."
  }
]);
console.log({ agents: phase.agents, works: phase.works });
const reviewWait = await tools.wait_agent({ agents: [phase.agents[1]], until: "idle" });
if (reviewWait.state !== "settled") console.log({ observation: reviewWait });
console.log(await tools.query_agents({ target: phase.works[1] }));
```

The review starts after the investigation succeeds and receives its final as a
separate input. The investigator should finish rather than wait for this reviewer
to reply: the reviewer is blocked until that prerequisite completes. The two
AGENT Targets stay the same; both WORK Targets are new. To wait in a further Cell,
copy the AGENT target from `phase.agents[1]` to observe its queue with
`tools.wait_agent({ agents: [reviewer], until: "idle" })`. Keep the new WORK target
from `phase.works[1]` to query this exact review. A new wait takes a new observation.

## 4. Reuse an Agent in another Cell

Follow this procedure when the next user request builds on an earlier assignment:

1. Inspect the actual AGENT and WORK targets in earlier Cell output. If necessary,
   call native `query_agents` to list retained Agents; query an Agent to list its
   Works, then use a Work's `inspect` to identify the relevant instruction/final.
   Follow `next` to continue a page. Titles help a person read the list but are not
   unique identity keys.
2. Copy the selected AGENT target into the new script's assignment `target`.
   Write a new `work_title` and instruction. Keep `context` out of continuations.
3. Observe the returned Agent with `until: "idle"`, then query the newly returned
   Work and log its actual state and result. The Agent keeps
   its previous conversation; there is no need to rerun the investigation.

For the same operation within one Cell, example 1 shows the exact continuation
syntax. In a later Cell, substitute the complete real AGENT target from output for
`investigation.agents[0]`; that variable belongs to the earlier Cell. Do not invent
an ID, assume another query's first item is the same Agent, or create a replacement
just because the previous Cell ended.

If ROOT needs the targets before the Work finishes, log them immediately after
acceptance. A Cell can then remain waiting while ROOT receives those references.

## 5. Query, steer or stop during a script

ROOT can use native `query_agents`, `send_agent_message`, and `stop_agent` on the
same targets without starting another Cell. Send an additional requirement to an
active Agent; if it is idle, assign a follow-up with `tools.run_agent` instead.

Stopping a WORK affects that assignment. Stopping an AGENT affects its current
and queued work. Stopping a Cell stops its script and further submissions; it
does not stop already accepted agent work. Inspect actual state before continuing.

A Cell's acceptance response is not its final output. `console.log` actively
delivers output to ROOT. `manage_cell({action:"wait", ...})` observes execution
state, not the logged result bodies. Do not submit the same script again to fetch
output; query the existing Cell or Work.
