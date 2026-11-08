# Agent coordination

An Agent is a reusable conversation. A Work is one assignment to that Agent.
Use the returned complete targets to identify both; display titles are not keys.

## Dispatch an array of assignments

```ts
type BatchReferences = { agents: AgentTarget[]; works: WorkTarget[] };
tools.run_agent([
  { work_title?: string,
    instruction: string | ((refs: BatchReferences) => string), target?: AgentTarget,
    context?: {mode: "none"} | {mode: "last_n", turns: number},
    depends_on?: WorkTarget[] | ((refs: BatchReferences) => WorkTarget[]),
    materials?: WorkTarget[] }
]): Promise<{agents: AgentTarget[], works: WorkTarget[], details: object[]}>
```

Each input position corresponds to the same position in all three result arrays.
`details[i]` contains `work_title`, acceptance `status`, and `inspect`, a query for
that Work. Acceptance means the Work exists; use a wait and query for its outcome.
One assignment uses a one-element array. More assignments use the same form.

Omit `target` to create an Agent. Pass an actual AGENT target to append a Work to
that Agent's queue and preserve its conversation. `work_title` describes this Work
and can be supplied for creation or continuation. Titles may repeat. Omitting it
leaves the Work untitled. `context` is creation-only: the default copies no ROOT
history; `last_n` accepts one to three recent ROOT turns. Put necessary task detail
in the instruction. Images can be supplied as accessible paths or references so
the agent can read them with its own tools.

Assignments to one Agent run in input order. Different Agents can run concurrently
as capacity permits. Each call accepts 1–16 assignments, each with up to 16
dependencies and 16 materials, and at most 64 explicit dependency edges in total.
Submit further work in later calls as needed.

## Supply prerequisites and prior results

- `depends_on: [workTarget]` starts this Work after its prerequisites succeed;
  their finals are supplied as separate inputs. A stopped prerequisite stops
  dependent work without starting it.
- `materials: [workTarget]` supplies an already ended Work's outcome as background.
- Queue order waits for the earlier Work to end; only `depends_on` requires success.

A dependency cycle, including a cycle through the same Agent's queue, is rejected
before the batch starts. An earlier queued assignment cannot depend on a later
assignment to the same Agent. Do not reorder targets or invent IDs to bypass this.

## Refer to members before they start

`instruction` and `depends_on` also accept synchronous functions. Each receives
`{agents, works}`, the exact targets for all input positions. These are references,
not future results. Compute parameters inside the function; call tools before or
after `run_agent`, not from its parameter functions. Keep these functions pure and
synchronous: do not use `async`, return a Promise, or schedule asynchronous work.
An async function or returned Promise is a Cell contract error and blocks further tool submissions
from that Cell, even if the script catches it.

The callback parameter is local to that function. For example,
`` instruction: ({ agents }) => `Compare with ${JSON.stringify(agents[1])}` ``
can use this batch's peer Target. Writing the same template directly as
`` instruction: `Compare with ${JSON.stringify(agents[1])}` `` evaluates it before
dispatch, when no callback has supplied `agents`. Likewise, the outer `batch`
variable is not available until `await run_agent(...)` returns. Existing local
values such as file contents may be captured normally by a callback.

Use `works[i]` inside `depends_on` to describe a prerequisite, not to fetch its
result. The scheduler starts the dependent Work after success and supplies the
prerequisite final as a separate input. Its instruction should tell the agent what
to check in that supplied material; ROOT need not copy the report into a message.

```js
const batch = await tools.run_agent([
  {
    work_title: "Investigate the change",
    instruction: "Inspect the requested behavior and report a proposed design."
  },
  {
    work_title: "Review the proposal",
    instruction: "Review the prerequisite proposal against the relevant code.",
    depends_on: ({ works }) => [works[0]]
  }
]);
const reviewWait = await tools.wait_agent({ agents: [batch.agents[1]], until: "idle" });
if (reviewWait.state !== "settled") console.log({ observation: reviewWait });
console.log(await tools.query_agents({ target: batch.works[1] }));
```

For parallel discussion, embed `JSON.stringify(agents[i])` in the other member's
instruction. Leave them independent if they need to communicate while active.
An Agent blocked on your own completion cannot reply before you finish.

## Choose what to wait for

```ts
tools.wait_agent({agents: AgentTarget[], until?: "idle" | "work_end",
                  settle?: "all" | "first", timeout_seconds?: number})
```

Pass a nonempty array of distinct subagent targets. ROOT itself is not accepted.
Defaults are `until: "idle"`, `settle: "all"`, and `timeout_seconds: 30`.
Timeout accepts 0–300 seconds; 0 performs the first check without suspending.
An initially satisfied condition can return immediately: no new event is required.

| Condition | `first` | `all` |
| --- | --- | --- |
| `idle` | Any selected Agent currently has no running or queued Work. | The entire original set is idle in one current snapshot. |
| `work_end` | Any initially idle Agent or any initially bound Work reaches terminal state. Queued-only targets do not block other bound Works from satisfying first. | All initially idle targets and bound Works satisfy; a queued-only target makes this observation unavailable. |

`idle` includes waiting dependencies and assignments accepted during the wait.
Waiting for tools, messages or a physical slot is still busy. A prior idle
observation is not accumulated if that Agent becomes busy again. Do not shrink a
first result's pending_targets to prove the original set is simultaneously idle;
use the original set with `settle: "all"` for that goal.

`work_end` binds each running Work once. An initially idle Agent stays satisfied;
an initially queued-only Agent stays `not_started`, without binding future Work.
Only an observation with no satisfied target and no bound Work is unavailable for
first. A bound Work remains satisfied after it completes or stops even if the
Agent has started a successor. Completion and stopping both meet the wait
condition; they do not establish success. Historical Works are not new completions.

Any listed sender's unread message can return early in either mode, including an
Agent already satisfied or initially not_started. Other senders' messages remain
on the normal input path. Caller control takes precedence, then a selected
message, the settle condition, the applicable unavailable condition and timeout.

Results contain `state`, `until`, ordered `observations` and `pending_targets`.
An observation has `target` and `condition` (pending, work_ended, idle or
not_started); a bound work_end observation also has its exact `work` target.
In work_end, idle and not_started describe the initial classification, not current
live activity. Its pending_targets can include not_started targets and exclude
Agents already busy on later Work. In idle, these fields describe the current
snapshot. message_available adds message_sources; unavailable adds
unavailable_targets with reason not_started. A message can return even when no
pending targets remain. No Work or message bodies are copied into this result.

Keep the dispatch's Work targets for queries. Deduplicate its aligned Agent array
before waiting; one Agent can receive several Works in one dispatch:

```js
const agents = [...new Map(batch.agents.map(target => [target.agent_id, target])).values()];
const observation = await tools.wait_agent({ agents, until: "idle" });
console.log(observation);
const results = await Promise.all(batch.works.map(target => tools.query_agents({ target })));
console.log(results);
```

Check observation state and actual Work status before proceeding. A timeout or
message is an opportunity to inspect, not proof of completion. Another call is a
new observation with fresh bindings; pending_targets does not resume an old
binding. If an Agent is only queued, use idle to observe queue completion and
query the retained Work for its exact result.

For an Agent already observed running, use work_end to read the Work actually
bound at the first wait snapshot. `runningAgent` below is its complete AGENT target
from an actual query; it does not freeze that earlier query's Work identity.

```js
const current = await tools.wait_agent({ agents: [runningAgent], until: "work_end" });
console.log(current);
for (const item of current.observations) {
  if (item.condition === "work_ended") {
    console.log(await tools.query_agents({ target: item.work }));
  }
}
```

If the Agent became idle or queued-only before that first snapshot, the result
reports that classification instead; do not infer a completed Work from its history.

A timeout or interruption ends only the observation. To stop one assignment use
`stop_agent` with its WORK target; to stop an Agent's current and queued work use
its AGENT target. Stopping a Cell does not stop accepted assignments.

## Keep targets across scripts

Each Cell starts with fresh variables. Log the actual targets and selected results
that ROOT needs. In a later Cell, reuse targets copied from that output, or query
retained agents and their works. Follow the returned `next` calls for further pages
and `inspect` for details. Use task evidence to choose the right retained Agent;
its position in a new query page or a matching title does not establish identity.

Redeclare local variables in the new Cell using complete Target objects from the
earlier output. Retain the `kind` and identity field unchanged. Pass an AGENT as
assignment `target` to preserve the conversation and create a new Work. Keep that
call's returned WORK targets for exact queries; an old WORK does not become
the new assignment when its Agent is reused. A synchronous `depends_on` callback
can connect new Works in the same batch even when their Agents are retained from
earlier Cells. Array order alone does not declare such a dependency.

The same Agent may appear more than once in a dispatch result when it receives
several Works. Deduplicate `agents` by agent_id before observing queue completion;
query each unique WORK target for that assignment's actual outcome.

## Direct controls and useful corrections

ROOT can natively query, message and stop the same targets while scripts run,
even when a Cell cannot start. Messages supplement active work and wake a subagent paused in native
`wait_message({timeout_seconds: ...})`, regardless of sender. The native wait needs
no target, returns only `message_available` or `timeout`, and leaves message bodies
to normal input processing. Appending another Work only queues it until the current
Work ends; it does not wake this inbox wait. Messages do not create assignments or
wake an idle Agent to perform a new task. For a follow-up assignment,
use `run_agent` with its retained AGENT target. A message uses
`send_agent_message({target: agentTarget, message: text})`: keep the complete Target
inside `target`; do not repeat its identity fields at the outer argument level.
An `inactive` result means the message was not delivered to active work. Do not
count it as a received instruction.

Agent and Work queries describe work, not a complete communication log. ROOT may
not see messages exchanged between subagents. Before judging whether they compared
evidence, verify what was actually sent, received and checked. A lack of visible
messages in ROOT context is not proof that no exchange occurred.


| Incorrect form or inference | Correct usage |
| --- | --- |
| `run_agent({instruction: ...})` | `run_agent([{instruction: ...}])` |
| `run_agent.batch(...)` or `wait_agent.works(...)` | `run_agent([...])` or `wait_agent({agents: [...], until: "idle"})` |
| `send_agent_message({...agentTarget, message})` | `send_agent_message({target: agentTarget, message})` |
| Sending `name` or using a title as target | Use `work_title` for display and the actual Target for identity |
| Interpolating current-batch `agents` in a plain instruction string | Use `instruction: ({ agents }) => ...` and interpolate inside the function |
| Reading `batch.works` while constructing that same dispatch | Use `depends_on: ({ works }) => [works[0]]` |
| Reading a previous Cell's local variable | Copy its logged Target into the new script or query the actual object |
| Waiting on an earlier Work after continuing its Agent | Wait on the new `run_agent` result's `works` |
| Treating a message or accepted Cell as completed work | Wait for the exact WORK and query its result |
| Retrying a dispatch just to obtain its output | Observe the accepted Cell and query the existing Work |
