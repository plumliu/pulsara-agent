# Workflows

This example divides a task into two scripts, leaving a point for ROOT to assess
findings between them. The same APIs also support a single script or smaller
steps interleaved with native calls, depending on how the task develops.

The creation calls below illustrate starting with new agents. If suitable agents
already investigated the problem through native tools or another script, their
targets can fill those roles, without creation-only `name` and `context` fields.
The workflow continues their conversations. ROOT can inspect or message any
active member through native tools while the script waits for its work.

## Phase 1: investigate, then review

This script reads project context, starts two investigators, and gives their
finals to a critic. The critic also learns whom to contact if clarification is
needed; inactive investigators require another Work rather than a message.

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" }, limit: 160
});
const phase1 = await tools.run_agent.batch({
  design: {
    name: "Design investigator",
    profile: "research_worker",
    instruction: `Investigate the requested change and propose its simplest complete design.
Read the relevant code. Starting README excerpt:
${readme.content}`,
    context: { mode: "last_n", turns: 1 }
  },
  integration: {
    name: "Integration investigator",
    profile: "research_worker",
    instruction: "Inspect the requested change's callers, integration points, and meaningful validation. Explain the implementation boundaries.",
    context: { mode: "last_n", turns: 1 }
  },
  critic: {
    name: "Design critic",
    profile: "review_worker",
    depends_on: ({ design, integration }) => [design.work, integration.work],
    instruction: ({ design, integration }) =>
      `Review the two prerequisite findings against the user's requested change.
Identify concrete gaps and recommend a complete implementation plan.
The design investigator is ${JSON.stringify(design.agent)};
the integration investigator is ${JSON.stringify(integration.agent)}.
Ask an investigator for clarification if it is still active; otherwise state the unresolved question.`,
    context: { mode: "last_n", turns: 1 }
  }
});
console.log({ phase1 });
console.log(await tools.wait_agent.works({ targets: [phase1.critic.work] }));
```

This template copies the latest ROOT turn as starting context. Instructions can
supply any additional task details. The critic's result offers a point for ROOT
to assess findings and decide how to continue.

## Phase 2: revise, implement, and reuse the critic

Each `exec` has fresh variables. Copy the actual `phase1` object printed above
into the next script; the example IDs below are illustrative and must be replaced
with the tool-provided targets.

```js
const phase1 = {
  design: {
    agent: { kind: "AGENT", agent_id: "session:aaaaaaaaaaaaaaaaaaaaaaaaaa" }
  },
  critic: {
    agent: { kind: "AGENT", agent_id: "session:bbbbbbbbbbbbbbbbbbbbbbbbbb" },
    work: { kind: "WORK", work_id: "queue:cccccccccccccccccccccccccc" }
  }
};
const phase2 = await tools.run_agent.batch({
  revision: {
    target: phase1.design.agent,
    materials: [phase1.critic.work],
    instruction: "Revise your proposal using the critic's findings and ROOT's decision. Produce the final implementation plan."
  },
  implementation: {
    name: "Implementer",
    depends_on: ({ revision }) => [revision.work],
    instruction: "Implement the prerequisite plan and run the checks relevant to its behavior. Report the changes and actual verification."
  },
  review: {
    target: phase1.critic.agent,
    depends_on: ({ revision, implementation }) => [revision.work, implementation.work],
    instruction: "Review the implemented code against the agreed plan and the implementation report. Inspect the actual files. Report actionable findings or explain why the change is ready."
  }
});
console.log({ phase2 });
console.log(await tools.wait_agent.works({ targets: [phase2.review.work] }));
```

The revision instruction can include ROOT's conclusions from the first phase.
The investigator and critic retain their conversations; the implementer receives
the plan as its prerequisite input. If code review calls for fixes, one option
is a new Work for the implementer with the review Work in `materials`, followed
by another assignment to the same critic.
That is another phase over the same agents, not a restarted workflow.

Ordinary tools can be used between these stages: inspect a patch, read an output
file, or run a verification command, then include the actual results in a new
instruction. Waiting for `completed` only establishes that a Work ended normally;
the review assessment is in its final, rather than encoded in that state.

## Continue after interruption

`list_agents` and `inspect_agent` show current and recent work. These results
can help ROOT reuse completed findings, follow running work, or assign new Work. Reusing an unloaded agent loads its existing
conversation automatically; it does not rerun its previous assignment.

A stopped prerequisite stops its dependent work. Build the remaining phase with
new Work targets; old terminal Works do not become running again. Previously
completed effects stay in place. Inspect the current files or other tool results
when the interrupted operation's effects are uncertain.

Logging agent/work targets makes them available to later scripts. `wait_exec`
can reveal progress in a script that is still running. If its host has closed,
the old script cannot be resumed; agent state remains queryable, and further work
can be assigned through a new script or native tools.
