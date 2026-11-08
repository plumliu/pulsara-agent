# Workflows

Combine tools and agent work according to the actual task. A success dependency
passes prerequisite finals directly to the next Work; materials carry an ended
Work's findings into a continuation. Use actual targets to preserve Agent history.

## Investigate, implement and review

This script reads project context, investigates a change, and submits implementation
and review. It illustrates composition rather than a mandatory sequence.

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" }, limit: 160
});
const investigation = await tools.run_agent([
  {
    work_title: "Design the requested change",
    instruction: `Investigate the user's requested change and propose its simplest complete design.
Read relevant code. README excerpt:
${readme.content}`,
    context: { mode: "last_n", turns: 1 }
  },
  {
    work_title: "Check integration and validation",
    instruction: "Inspect callers and integration points for the requested change. Propose meaningful validation.",
    context: { mode: "last_n", turns: 1 }
  }
]);
console.log({ agents: investigation.agents, works: investigation.works });
const investigationWait = await tools.wait_agent({
  agents: [...new Map(investigation.agents.map(target => [target.agent_id, target])).values()],
  until: "idle"
});
if (investigationWait.state !== "settled") console.log({ observation: investigationWait });
const findings = await Promise.all(investigation.works.map(target => tools.query_agents({ target })));
if (findings.some(work => work.status !== "completed")) {
  console.log({ findings });
} else {
  const phase = await tools.run_agent([
    {
      target: investigation.agents[0],
      work_title: "Implement the change",
      materials: investigation.works,
      instruction: "Implement the user-authorized change using the investigation findings. Run meaningful checks and report actual results."
    },
    {
      target: investigation.agents[1],
      work_title: "Review the implementation",
      depends_on: ({ works }) => [works[0]],
      instruction: "Review the actual changed code and its validation. Report concrete defects or explain why the requested change is ready."
    }
  ]);
  console.log({ agents: phase.agents, works: phase.works });
  const reviewWait = await tools.wait_agent({ agents: [phase.agents[1]], until: "idle" });
  if (reviewWait.state !== "settled") console.log({ observation: reviewWait });
  const review = await tools.query_agents({ target: phase.works[1] });
  if (review.status !== "completed") {
    console.log({ review });
  } else {
    const condition = "Does this review identify a concrete defect in the requested implementation that requires a code or test change?";
    const inputs = [JSON.stringify({ project_context: readme.content, review: review.final })];
    if (tools.decide.available()) {
      const { decisions } = await tools.decide({ condition, inputs });
      if (decisions[0]) {
        const fix = await tools.run_agent([
          {
            target: phase.agents[0],
            work_title: "Address review findings",
            materials: [phase.works[1]],
            instruction: "Address the concrete review findings within the requested change. Inspect the code, make necessary fixes, and run relevant checks. Report unresolved product choices to ROOT."
          }
        ]);
        const fixWait = await tools.wait_agent({
          agents: [...new Map(fix.agents.map(target => [target.agent_id, target])).values()],
          until: "idle"
        });
        if (fixWait.state !== "settled") console.log({ observation: fixWait });
        console.log(await tools.query_agents({ target: fix.works[0] }));
      } else {
        console.log({ agents: phase.agents, works: phase.works, review });
      }
    } else {
      console.log({ condition, inputs, source: readme, agents: phase.agents, works: phase.works, review });
    }
  }
}
```

Decision is optional. When unavailable, the logged condition, material and targets
let ROOT interpret the review and continue the same implementer in a new Cell.
A completed Work means the assignment ended normally; its actual final determines
whether the proposed design or implementation is satisfactory.

## Continue after interruption

Use native queries to inspect retained Agents and exact Works. Finished results
remain usable. An Agent target can receive another Work after a previous one
finishes or stops; an old Work target continues to identify only that old Work.

Stopping a prerequisite stops success-dependent work. Build the remaining phase
with new Work targets and reuse the actual Agents when useful. Do not repeat
completed changes just to recreate a local JavaScript variable.

Each Cell has fresh variables. Pass actual targets through console output, then
copy them into a later Cell or query their objects. If the host has closed, the
old script cannot resume; query retained agent work and submit any continuation
through a new Cell. Native control stays available independently of Cell capacity.
