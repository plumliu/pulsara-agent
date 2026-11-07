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
await tools.wait_agent({ works: investigation.works });
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
await tools.wait_agent({ works: followup.works });
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
await tools.wait_agent({ works: team.works });
const findings = await Promise.all(team.works.map(target => tools.query_agents({ target })));
console.log(findings);
```

Both assignments are independent and can run as capacity permits. References do
not make the other Agent active: sending reports `not_started` or `inactive` if
it cannot accept a message. Then report the outstanding question to ROOT, which
can continue the appropriate Agent with a new Work.

## 3. A reviewer that needs a finished investigation

```js
const phase = await tools.run_agent([
  {
    work_title: "Investigate the change",
    instruction: "Inspect the requested change and propose a complete implementation plan with meaningful validation.",
    context: { mode: "last_n", turns: 1 }
  },
  {
    work_title: "Review the proposal",
    depends_on: ({ works }) => [works[0]],
    instruction: "Review the prerequisite plan against the request and actual code. Identify actionable gaps or explain why it is ready.",
    context: { mode: "last_n", turns: 1 }
  }
]);
await tools.wait_agent({ works: [phase.works[1]] });
console.log(await tools.query_agents({ target: phase.works[1] }));
```

The review starts after the investigation succeeds and receives its final as a
separate input. The investigator should finish rather than wait for this reviewer
to reply: the reviewer is blocked until that prerequisite completes.

## 4. Reuse an Agent in another Cell

Follow this procedure when the next user request builds on an earlier assignment:

1. Inspect the actual AGENT and WORK targets in earlier Cell output. If necessary,
   call native `query_agents` to list retained Agents; query an Agent to list its
   Works, then use a Work's `inspect` to identify the relevant instruction/final.
   Follow `next` to continue a page. Titles help a person read the list but are not
   unique identity keys.
2. Copy the selected AGENT target into the new script's assignment `target`.
   Write a new `work_title` and instruction. Keep `context` out of continuations.
3. Wait on the newly returned Work, then query and log its result. The Agent keeps
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
