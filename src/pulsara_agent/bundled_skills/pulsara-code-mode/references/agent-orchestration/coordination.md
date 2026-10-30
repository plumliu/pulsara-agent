# Agent coordination

For single-agent, teammate, dependency, and native/JS handoff scenarios, see
[Agent examples](examples.md).

## An Agent persists; a Work is one assignment

`run_agent` accepts an `instruction` and immediately gives you an `agent` target,
a `work` target, and the current `status`. Acceptance is not completion.

These identities belong to the agent and assignment, not to the tool entry point
or script. An agent created natively can be reused in a batch; an agent started
by a batch can receive a native message or follow-up assignment. Query the same
state with native `query_agents` or `tools.query_agents`. Switching entry points
does not start work by itself or reset the agent's conversation.

- Omit `target` to create an agent. Optional `name` identifies its role. New agents
  receive the instruction and supplied context; ROOT history is not copied by
  default. For recent ROOT context, use `context: { mode: "last_n", turns: 1 }`
  with one to three turns.
- Pass a prior `agent` target to give that same agent another Work. Its conversation
  continues, and assignments run in queue order. `name` and `context` are creation
  options, so omit them when reusing an agent.

Use the complete targets supplied by the tools: AGENT has a `session:` ID and
WORK has a `queue:` ID. `agent_id: "ROOT"` addresses ROOT for messages or waiting;
it is not a target for assigning new subagent work.

ROOT can query its retained agents. A subagent can use native `query_agents` to
find itself and teammates introduced through accepted instructions, context,
materials, or messages; the list also returns the `ROOT` communication alias.
Query results do not introduce additional teammates. A valid teammate ID may be
used for messaging or waiting even before it appears in the query list.

## Give results to the next assignment

| Field | Use |
| --- | --- |
| `depends_on: [workTarget]` | When this assignment needs a prerequisite to succeed before it can begin. |
| `materials: [workTarget]` | When an earlier outcome is useful background for this assignment. |

Delegated instructions, initial context, prerequisite results, explicit materials, and
agent messages are plain text. To share an image, supply its accessible path or
reference in text so the agent can use `view_image`.

The new instruction and each prerequisite result remain separate input messages.
ROOT does not need to copy finals into an instruction just to pass them along.
Alternatively, inspect a result, make a decision, then create the next Work.

## Introduce teammates in the first instruction

`tools.run_agent.batch(specs)` accepts an object of locally named assignments.
Before any member starts, it supplies each parameter function with all members'
exact `agent` and `work` targets. The resulting assignments are accepted together.

```js
const pair = await tools.run_agent.batch({
  parser: {
    name: "Parser developer",
    instruction: ({ caller }) =>
      `Implement the parser. Agree on parse(input) with the caller developer:
${JSON.stringify(caller.agent)}. Use native send_agent_message to coordinate.`
  },
  caller: {
    name: "Caller developer",
    instruction: ({ parser }) =>
      `Integrate the parser's caller. Agree on parse(input) with the parser developer:
${JSON.stringify(parser.agent)}. Use native send_agent_message to coordinate.`
  }
});
console.log(pair);
console.log(await tools.wait_agent.works({
  targets: [pair.parser.work, pair.caller.work]
}));
```

Only `instruction` and `depends_on` accept synchronous parameter functions.
Use them to construct text or target arrays from the supplied references; perform
tool calls before or after the batch. The references contain identities, not
future results. Teammate references alone do not impose execution dependencies.

Each batch accepts up to 16 assignments, each with up to 16 dependencies and 16
materials, with up to 64 dependency edges per batch. More work can be submitted
in subsequent batches. Work assigned to the same agent runs in declaration order.
Queue order requires the earlier Work to end; only an explicit `depends_on`
requires it to succeed. Later queue entries cannot supply a prerequisite to an
earlier one. A batch with a dependency cycle is rejected before members start.

For example, two agents could implement separate parts and then review each other:

```text
Agent A queue: implement A → review B
Agent B queue: implement B → review A
review B depends_on implement B
review A depends_on implement A
```

Each review starts after its own agent's implementation ends and the other
agent's implementation succeeds.

## Message, wait, or stop

`send_agent_message` supplements an active Work. It does not start work or load an
idle agent. Its result reports whether delivery was accepted; a known target can
still be inactive or not yet started. `run_agent` can assign work to an idle agent.
Subagents can use native messaging and waiting without Code Mode.

| Operation | Observe |
| --- | --- |
| Native `wait_agent` or JS `tools.wait_agent` | AGENT targets: messages from those agents or the end of their work bound at the start of this wait. It does not follow a later assignment automatically. |
| `tools.wait_agent.works({ targets: [...], settle: "all" })` | Exact WORK targets until all end; ordinary messages do not wake this wait. `all` is the default. |
| `tools.wait_agent.works({ targets: [...], settle: "first" })` | Any selected Work ending. Read `pending_targets` for the rest. |

WORK waits have no observation timeout; `exec_cell` can yield while the wait continues.
AGENT waits use the native timeout parameter. Waiting does not notify or start
the target. Waiting for ROOT is useful when a reply has been arranged; an idle
ROOT may have no work or reply to finish.

Work states are `queued`, `running`, `completed`, and `stopped`.
`tools.wait_agent.works` returns `state` (`settled` or `interrupted`), `works`, and
`pending_targets`. `settled` means the requested wait condition was met, not that
every Work succeeded. `works` reports all selected Works, including those still
queued or running, with `final` or `diagnostic` when available.

For example, selected fields after a `settle: "first"` wait could be:

```json
{
  "state": "settled",
  "works": [
    {
      "target": {"kind": "WORK", "work_id": "queue:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
      "status": "completed",
      "final": "Investigation complete."
    },
    {
      "target": {"kind": "WORK", "work_id": "queue:bbbbbbbbbbbbbbbbbbbbbbbbbe"},
      "status": "running"
    }
  ],
  "pending_targets": [
    {"kind": "WORK", "work_id": "queue:bbbbbbbbbbbbbbbbbbbbbbbbbe"}
  ]
}
```

`pending_targets` contains complete WORK targets and can be passed to the next
wait when nonempty. It is empty when all selected Works have ended. An interrupted
wait includes the last observed states and remaining targets as well.

`stop_agent` with a WORK target stops that assignment. With an AGENT target, it
stops current and already queued work. Read actual outcomes; completed work stays
completed, and the agent remains reusable. A stopped prerequisite propagates to
its success-dependent work; independent work can continue. Stopping a script
with `manage_cell` with `action:"stop"` does not perform these agent stops.
