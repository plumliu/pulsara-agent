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
  profile: "research_worker",
  instruction: "Trace how the parser handles an empty input. Identify the relevant source and tests, and explain the observable behavior. Do not edit files."
});
console.log(investigation);
console.log(await tools.wait_agent.works({ targets: [investigation.work] }));
```

**How it works:** `run_agent` accepts one assignment and supplies both targets.
The WORK wait follows that exact assignment, even if the agent later receives
another one. The log preserves the agent target for reuse.

**Read the result:** The wait's `works` contains the final or stopped outcome.
The findings can inform a follow-up. For example, `run_agent` with the same
agent target continues the investigation:

```js
const followup = await tools.run_agent({
  target: { kind: "AGENT", agent_id: "session:<actual-agent-id>" },
  instruction: "Using your investigation, explain whether the current tests distinguish an empty string from whitespace-only input. Read additional tests if needed."
});
console.log(followup);
console.log(await tools.wait_agent.works({ targets: [followup.work] }));
```

Replace the illustrative target with `investigation.agent` from the actual output.
This follow-up can be a new script: it does not rely on a variable from the first
script. The agent keeps its conversation; ROOT need not repeat the investigation.
A terminal state can mean either completion or a stopped assignment.

## 2. Let two active teammates compare their findings

**Situation:** A backend and its client must agree on an API response. One approach is for two agents
to inspect different sides in parallel and exchange findings. The example puts
each teammate's role and target in the other's initial instruction.

```js
const team = await tools.run_agent.batch({
  backend: {
    name: "Backend investigator",
    profile: "research_worker",
    instruction: ({ client }) =>
      `Inspect the backend response fields and error cases for the requested API change.
The client investigator is ${JSON.stringify(client.agent)}.
Send it your proposed response shape through native send_agent_message and compare findings.
Report the agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  },
  client: {
    name: "Client investigator",
    profile: "research_worker",
    instruction: ({ backend }) =>
      `Inspect the client expectations for the requested API change.
The backend investigator is ${JSON.stringify(backend.agent)}.
Send it the fields and error behavior the client needs through native send_agent_message.
Report the agreed fields and unresolved differences. Do not edit files.`,
    context: { mode: "last_n", turns: 1 }
  }
});
console.log(team);
console.log(await tools.wait_agent.works({
  targets: [team.backend.work, team.client.work]
}));
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
  profile: "research_worker",
  instruction: "Investigate the requested change. Read the relevant code and propose a complete implementation plan with affected interfaces and validation.",
  context: { mode: "last_n", turns: 1 }
});
const review = await tools.run_agent({
  name: "Plan reviewer",
  profile: "review_worker",
  depends_on: [investigation.work],
  instruction: "Review the prerequisite plan against the user's request and the actual code. Identify concrete gaps and explain what is ready to implement.",
  context: { mode: "last_n", turns: 1 }
});
console.log({ investigation, review });
console.log(await tools.wait_agent.works({ targets: [review.work] }));
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

## 4. Expand native research into a scripted workflow

**Situation:** The user initially asks only how a parser handles empty and
whitespace-only input. One option is to delegate through native `run_agent`,
read the findings, and answer the user. A short `exec` script could also do this.

Native `run_agent` arguments:

```json
{
  "name": "Parser investigator",
  "profile": "research_worker",
  "instruction": "Investigate how the parser handles empty and whitespace-only input. Explain the relevant code and tests. Do not edit files."
}
```

Later, the user asks ROOT to make whitespace-only input behave like empty input
and add the relevant tests. One possible arrangement is to reuse the investigator
for planning alongside a new assessor and implementer. The example below shows
that arrangement, using the investigator's actual agent target:

```js
const investigator = { kind: "AGENT", agent_id: "session:<actual-agent-id>" };
const phase = await tools.run_agent.batch({
  plan: {
    target: investigator,
    instruction: ({ assessment }) =>
      `Use your earlier investigation to plan the change: whitespace-only input must behave like empty input, with tests covering spaces, tabs, and newlines.
Compare your proposed behavior with the independent assessor ${JSON.stringify(assessment.agent)} using native send_agent_message.
Report a concrete implementation plan and any unresolved differences.`
  },
  assessment: {
    name: "Parser behavior assessor",
    profile: "review_worker",
    instruction: ({ plan }) =>
      `Independently inspect the effects of treating whitespace-only parser input as empty input, including existing callers and tests.
Compare findings with the investigator ${JSON.stringify(plan.agent)} using native send_agent_message.
Report compatibility requirements and any unresolved differences.`
  },
  implementation: {
    name: "Parser implementer",
    depends_on: ({ plan, assessment }) => [plan.work, assessment.work],
    instruction: "Implement whitespace-only input handling using the prerequisite plan and assessment. Add relevant tests. If the prerequisites leave a material behavior decision unresolved, report it for ROOT to decide before making that change. Report actual modifications and checks."
  }
});
console.log(phase);
console.log(await tools.wait_agent.works({ targets: [phase.implementation.work] }));
```

**How it works:** The reused investigator receives a new Work in its existing
conversation. The assessor is new. Their assignments can run in parallel; the
implementer starts after both succeed and receives both finals. Native and JS
assignments all appear under the same agents when queried with `list_agents` or
`inspect_agent`.

**Read the result:** The implementation final can inform any further action.
Introducing a script changed the orchestration,
not the investigator's identity or access to its previous conversation.

## 5. Use native tools while a scripted workflow runs

**Situation:** The script above has yielded, and its implementer is still working.
The user adds a concrete requirement. ROOT can send it directly through native
`send_agent_message`, using `phase.implementation.agent` from the logged output.
Replace the illustrative target below with that actual target:

```json
{
  "target": {"kind": "AGENT", "agent_id": "session:<actual-implementation-agent-id>"},
  "message": "Also cover mixed spaces and tabs in the tests, and include the observed behavior in your final."
}
```

To inspect this exact assignment, use native `inspect_agent` with
`phase.implementation.work`:

```json
{
  "target": {"kind": "WORK", "work_id": "queue:<actual-implementation-work-id>"}
}
```

Use an AGENT target with `inspect_agent` to inspect that agent's work history
instead. These native calls observe or affect the same work the script is
waiting for. No script restart or additional `exec` is needed.

**Read the result:** A message with `delivery: "queued"` was accepted for the
active work; `inactive` or `not_started` means it was not delivered. If the agent
has finished and the new requirement calls for more work, assign it a new Work
through native `run_agent` or JS, using the same agent target. Continue observing
the existing script with `wait_exec` when its output is needed.
