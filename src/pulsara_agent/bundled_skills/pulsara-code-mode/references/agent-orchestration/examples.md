# Agent examples

These are smaller building blocks than the full
[investigation-to-code-review workflow](workflows.md). ROOT can combine them with
ordinary tool calls in the same script. The roles and stages illustrate possible
arrangements, adaptable to the actual question and desired deliverable.

## 1. Ask one specialist, then continue with the same agent

**Situation:** You want a focused explanation of a parser's behavior. One option
is to ask an agent to investigate and use its findings to inform the next step.

```js
const investigation = await tools.run_agent({
  name: "Parser investigator",
  instruction: "Trace how the parser handles an empty input. Identify the relevant source and tests, and explain the observable behavior. Do not edit files."
});
console.log({ agent: investigation.agent, work: investigation.work });
await tools.wait_agent.works({ targets: [investigation.work] });
console.log(await tools.query_agents({ target: investigation.work }));
```

**How it works:** `run_agent` accepts one assignment and supplies both targets.
The WORK wait follows that exact assignment, even if the agent later receives
another one. The log preserves the agent target for reuse.

**Read the result:** The wait reports states only; the explicit query reads the final or stopped diagnostic.
The findings can inform a follow-up. For example, `run_agent` with the same
agent target continues the investigation:

```js
const followup = await tools.run_agent({
  target: investigation.agent,
  instruction: "Using your investigation, explain whether the current tests distinguish an empty string from whitespace-only input. Read additional tests if needed."
});
console.log({ agent: followup.agent, work: followup.work });
await tools.wait_agent.works({ targets: [followup.work] });
console.log(await tools.query_agents({ target: followup.work }));
```

This continuation uses `investigation.agent` returned earlier in the same script.
For a new script, obtain the actual target with `query_agents` or copy the complete
target from the earlier output. The agent keeps its conversation; ROOT need not
repeat the investigation.
A terminal state can mean either completion or a stopped assignment.

## 2. Let two active teammates compare their findings

**Situation:** A backend and its client must agree on an API response. One approach is for two agents
to inspect different sides in parallel and exchange findings. The example puts
each teammate's role and target in the other's initial instruction.

```js
const team = await tools.run_agent.batch({
  backend: {
    name: "Backend investigator",
    instruction: ({ client }) =>
      `Inspect the backend response fields and error cases for the requested API change.
The client investigator is ${JSON.stringify(client.agent)}.
Send it your proposed response shape through native send_agent_message and compare findings.
Report the agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  },
  client: {
    name: "Client investigator",
    instruction: ({ backend }) =>
      `Inspect the client expectations for the requested API change.
The backend investigator is ${JSON.stringify(backend.agent)}.
Send it the fields and error behavior the client needs through native send_agent_message.
Report the agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  }
});
await tools.wait_agent.works({ targets: [team.backend.work, team.client.work] });
const findings = await Promise.all([team.backend.work, team.client.work].map(
  target => tools.query_agents({ target })
));
console.log(findings.map(work => ({ target: work.target, final: work.final, diagnostic: work.diagnostic })));
```

**How it works:** Batch parameter functions put the real teammate targets into
the initial instructions. There is no `depends_on`, so both assignments can run
concurrently as capacity permits. The agents communicate through native tools;
they do not need their own scripts.

**Read the result:** The two finals can help ROOT assess any remaining mismatch.
Messages only reach active work. If a teammate has already finished or has not
started, the sending tool reports non-delivery. The open question can then be
raised with ROOT, which can give either agent another Work.

## 3. Start a reviewer only after an investigation succeeds

**Situation:** A reviewer needs the investigation's final before it can begin.
This is a success dependency, unlike the parallel discussion above. ROOT can
submit both requests now and let their declared dependency determine startup.

```js
const investigation = await tools.run_agent({
  name: "Change investigator",
  instruction: "Investigate the requested change. Read the relevant code and propose a complete implementation plan with affected interfaces and validation.",
  context: { mode: "last_n", turns: 1 }
});
const review = await tools.run_agent({
  name: "Plan reviewer",
  depends_on: [investigation.work],
  instruction: "Review the prerequisite plan against the user's request and the actual code. Identify concrete gaps and explain what is ready to implement.",
  context: { mode: "last_n", turns: 1 }
});
await tools.wait_agent.works({ targets: [review.work] });
console.log(await tools.query_agents({ target: review.work }));
```

**How it works:** Each call supplies usable targets immediately after acceptance.
The review can remain `queued` while the investigation runs. On success, the
investigator's final is supplied to the review as a separate input; ROOT does not
need to fetch and paste it. If the prerequisite stops, the review stops without
beginning its assignment.

**Read the result:** A completed review means the reviewer finished its analysis,
not that the plan passed; the final contains the assessment. If a later assignment
only needs an already ended Work as background, `materials` supplies that content
without establishing a success dependency.

## 4. Reuse an existing agent in a scripted workflow

**Situation:** An earlier investigation explained parser behavior. The user now
asks for whitespace-only input to behave like empty input, with tests. Query the
retained agents and reuse the investigator rather than repeating its research.
This example looks for the role created in example 1. If the needed agent is on
another page, copy the returned `next` call and continue the lookup.

```js
const agents = await tools.query_agents({});
const investigator = agents.agents.find(agent => agent.name === "Parser investigator");
if (investigator) {
  const phase = await tools.run_agent.batch({
    plan: {
      target: investigator.agent,
      instruction: ({ assessment }) =>
        `Use your earlier investigation to plan the requested whitespace-only input change.
Compare behavior with ${JSON.stringify(assessment.agent)} using native send_agent_message.
Report the implementation plan and relevant tests.`
    },
    assessment: {
      name: "Parser behavior assessor",
      instruction: ({ plan }) =>
        `Inspect callers and tests affected by treating whitespace-only input as empty input.
Compare findings with ${JSON.stringify(plan.agent)} using native send_agent_message.
Report compatibility requirements and unresolved differences.`
    },
    implementation: {
      name: "Parser implementer",
      depends_on: ({ plan, assessment }) => [plan.work, assessment.work],
      instruction: "Implement the user-authorized change using the prerequisite findings. Test spaces, tabs and newlines. Report concrete changes and checks; return any unresolved product choice to ROOT."
    }
  });
  console.log({ implementation: phase.implementation });
  await tools.wait_agent.works({ targets: [phase.implementation.work] });
  console.log(await tools.query_agents({ target: phase.implementation.work }));
} else {
  console.log({ agents: agents.agents, next: agents.next });
}
```

**How it works:** The query supplies the real AGENT target. The investigator gets
a new Work in its existing conversation; the new assessor works alongside it.
The implementer receives both prerequisite finals. Creation and continuation use
Code Mode, and native queries see the same work.

**Read the result:** The implementation final can guide further work. Reusing the
agent preserves its earlier findings; a script's lifetime does not own that history.

## 5. Use native controls while a script runs

**Situation:** An implementer is still working when the user adds a requirement.
ROOT can call native `query_agents` without a target to find actual retained
agents, or with the logged `phase.implementation.work` to inspect that assignment.
For another page, copy the returned `next` call.

Use native `send_agent_message` with `target` set to the actual
`phase.implementation.agent` and `message` set to the new requirement, for example:
“Also cover mixed spaces and tabs in the tests, and report their observed behavior.”
Use native `stop_agent` with the actual WORK target to stop that assignment, or
its AGENT target to stop current and queued work. These are routine controls;
ROOT need not stop the script, acquire a new Cell slot or start the Code Mode
runtime first.

**Read the result:** `delivery: "queued"` means the active work accepted the
message; `inactive` or `not_started` means it did not. If the agent has finished,
continue with `tools.run_agent` in a new Cell using the same AGENT target. A message
does not create a Work. Selected script output arrives actively; `manage_cell`
with `action:"wait"` reads execution state when needed.
