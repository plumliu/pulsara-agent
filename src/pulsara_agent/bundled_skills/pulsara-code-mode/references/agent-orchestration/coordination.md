# Agent coordination

An Agent is a reusable conversation; a Work is one assignment. Use full Targets,
not titles, query positions or display names. Assignments to the same Agent run
in queue order; independent Agents use available physical slots and can queue.

```ts
type ContextMaterial = {kind:"CONTEXT_MATERIAL";turns:1|2|3};
type BatchReferences = {agents:AgentTarget[];works:WorkTarget[]};
type Assignment = {
  target?:AgentTarget; work_title?:string;
  instruction:string|((refs:BatchReferences)=>string);
  materials?:(ContextMaterial|WorkTarget)[];
  depends_on?:WorkTarget[]|((refs:BatchReferences)=>WorkTarget[]);
};
interface RunAgentSuccess {meta:{status:"SUCCESS"};content:ObjectPart<RunAgentWorkRecord>[]}
declare function run_agent(assignments:Assignment[]):Promise<RunAgentSuccess>;
```

One call accepts 1–16 Works, each with at most 16 dependencies and 16 WORK
materials, and at most 64 explicit dependency edges in the batch. These are
per-call input boundaries, not a total Work or Agent limit. Instruction and title
retain their existing 65,536 and 256 UTF-8 byte boundaries.

CONTEXT_MATERIAL explicitly selects 1–3 ROOT turns from the accepted dispatch
snapshot, for new or reused Agents. No ROOT history is included by default.
Ended WORK materials provide their actual final answer or stop diagnostic.
A WORK also in depends_on is handled once as a successful prerequisite. The main
instruction precedes separate history, dependency and other Work material entries.
A failed prerequisite stops dependent work through the existing owner.

## Choose what to wait for

```ts
type WaitOptions = {settle?:"all"|"first";timeout_seconds?:number};
type WaitArgs = WaitOptions & ({works:WorkTarget[];agents?:never}|{agents:AgentTarget[];works?:never});
```

WORK waits bind exact identities, including queued Works, and do not follow a
successor or wake on Agent messages. AGENT waits observe whether each Agent has
no running or queued Work. all requires the same snapshot to satisfy every
condition. first changes the exit condition and still returns every input record.
Timeout is an observation deadline, not a Work lifetime bound.

SUCCESS includes meta.wait_reason: condition_met, timeout, or AGENT-only
message_available. Work records are agent/work/work_status; Agent records are
agent/is_idle. Message sources are Targets, not bodies or read acknowledgements.
Cancellation stops this wait, not its targets. An actual failed/cancelled result
can be present as err.result and retains only known observations.

## Messages and stop

query_agents reads complete summaries or exact Work details. send_agent_message
adds text only to the recipient's current active turn; idle and queued-only
recipients are errors. Native wait_message is Subagent-only and never consumes
message text. stop_agent is ROOT-only and accepts one WORK or AGENT Target.
Cell stop does not stop its Agent Works or retract already queued output.
