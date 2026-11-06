# Workflows

For a user-authorized code change, the same script can read project context,
coordinate investigation and implementation, and decide whether a review calls
for another assignment. These stages illustrate one task, not a required workflow.
Use the tools and conditions that the actual request needs.

The creation calls below start new agents. If suitable agents already investigated
the problem in another script, use their actual targets without creation-only
`name` and `context` fields. ROOT can query, message or stop active work through
native controls while this script runs, without starting another Cell.

## Investigate, implement, then interpret the review

The investigators pass their finals to a critic. The next batch reuses the design
investigator and critic and gives the revised plan to an implementer. A semantic
condition combines the source context and code review to choose whether the same
implementer needs another Work. All AGENT and WORK targets come from actual calls.

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" }, limit: 160
});
const phase1 = await tools.run_agent.batch({
  design: {
    name: "Design investigator",
    instruction: `Investigate the requested change and propose its simplest complete design.
Read the relevant code. Starting README excerpt:
${readme.content}`,
    context: { mode: "last_n", turns: 1 }
  },
  integration: {
    name: "Integration investigator",
    instruction: "Inspect the requested change's callers, integration points, and meaningful validation. Explain the implementation boundaries.",
    context: { mode: "last_n", turns: 1 }
  },
  critic: {
    name: "Design critic",
    depends_on: ({ design, integration }) => [design.work, integration.work],
    instruction: ({ design, integration }) =>
      `Review the prerequisite findings against the user's requested change.
Identify concrete gaps and recommend a complete implementation plan.
The design investigator is ${JSON.stringify(design.agent)};
the integration investigator is ${JSON.stringify(integration.agent)}.
Ask an investigator for clarification if it is still active; otherwise state the unresolved question.`,
    context: { mode: "last_n", turns: 1 }
  }
});
console.log({ phase1 });
await tools.wait_agent.works({ targets: [phase1.critic.work] });
const proposalReview = await tools.query_agents({ target: phase1.critic.work });
if (proposalReview.status !== "completed") {
  console.log({ phase1, proposalReview });
} else {
  const phase2 = await tools.run_agent.batch({
    revision: {
      target: phase1.design.agent,
      materials: [phase1.critic.work],
      instruction: "Revise your proposal using the critic's findings. Resolve concrete gaps within the user's request and produce the implementation plan."
    },
    implementation: {
      name: "Implementer",
      depends_on: ({ revision }) => [revision.work],
      instruction: "Implement the user-authorized prerequisite plan and run the checks relevant to its behavior. Report the changes and actual verification."
    },
    review: {
      target: phase1.critic.agent,
      depends_on: ({ revision, implementation }) => [revision.work, implementation.work],
      instruction: "Review the implemented code against the plan and implementation report. Inspect the actual files. Report actionable findings or explain why the change is ready."
    }
  });
  console.log({ phase2 });
  await tools.wait_agent.works({ targets: [phase2.review.work] });
  const codeReview = await tools.query_agents({ target: phase2.review.work });
  if (codeReview.status !== "completed") {
    console.log({ phase2, codeReview });
  } else {
    const condition = "Does this code review identify a concrete defect in the requested implementation that needs a code or test change?";
    const inputs = [JSON.stringify({ project_context: readme.content, review: codeReview.final })];
    if (tools.decide.available()) {
      const { decisions } = await tools.decide({ condition, inputs });
      if (decisions[0]) {
        const fix = await tools.run_agent({
          target: phase2.implementation.agent,
          materials: [phase2.review.work],
          instruction: "Address the concrete code-review findings within the requested change. Inspect the current code, make the necessary fixes, and run relevant checks. Report any finding that requires a product decision to ROOT."
        });
        console.log({ fix });
        await tools.wait_agent.works({ targets: [fix.work] });
        console.log(await tools.query_agents({ target: fix.work }));
      } else {
        console.log({ phase2, codeReview });
      }
    } else {
      console.log({ condition, inputs, source: readme, phase2, codeReview });
    }
  }
}
```

The context setting copies the latest ROOT turn for the investigators. Supply any
additional task details in their instructions. Dependencies carry successful
finals into new work; `materials` carries the prior review into a continuation.
A `completed` Work only means the assignment ended normally. Its final contains
the assessment, which the boolean condition interprets. A stopped Work is reported
with its diagnostic instead of being treated as a successful review.

Unavailable Decision returns the condition, materials and actual Agent/Work
references to ROOT for judgment. The completed ordinary work remains usable;
configuration is not required to continue. ROOT can read the same review and
continue the implementer through `tools.run_agent` in a new script. When Decision
is available, the branch stays in this script. After a fix, further inspection or
another review can reuse the returned targets as the task requires.

## Continue after interruption

Native `query_agents` shows current and recent work. ROOT can reuse completed
findings, follow running work, send a message or stop a real target independently
of Code Mode availability. All new assignments and waits use Code Mode.
Reusing an unloaded agent loads its existing conversation automatically; it does
not rerun its previous assignment.

A stopped prerequisite stops its dependent work. Build the remaining phase with
new Work targets; old terminal Works do not become running again. Previously
completed effects stay in place. Inspect the current files or other tool results
when the interrupted operation's effects are uncertain.

Logging selected Agent/Work targets makes them available to later scripts.
Progress arrives only when the script outputs it; `manage_cell` with `action:"wait"`
observes execution state without fetching progress or result bodies. If its host
has closed, the old script cannot be resumed; agent state remains queryable and
further assignments use a new script with the actual targets.
