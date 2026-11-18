# Agent coordination

An Agent is a reusable conversation. A Work is one assignment. Omit `target` to
create an Agent, or pass an AGENT Target to queue work in its existing context.
Assignments to one Agent run in order. Different Agents can work concurrently.

```js
const accepted = await tools.run_agent([
  {instruction:"Investigate the parser and report evidence.",
   work_title:"Investigate parser", materials:[{kind:"CONTEXT_MATERIAL",turns:1}]},
  {instruction:({agents})=>"Review the investigation from "+JSON.stringify(agents[0]),
   depends_on:({works})=>[works[0]]}
]);
const works = accepted.content.map(part=>part.value.work);
console.log(accepted);
console.log(await tools.wait_agent({works,settle:"all",timeout_seconds:60}));
console.log(await tools.query_agents({target:works[0]}));
```

`materials` explicitly adds 1–3 ROOT turns or ended WORKs. ROOT history is omitted
by default. `depends_on` waits for successful work and delivers its result once,
even if also listed in `materials`. Failed dependencies stop dependent Work.
Instruction and dependency callbacks use preallocated `{agents,works}` Targets
synchronously, before dispatch. Keep tool calls out of these callbacks.

Wait on `works` for exact assignments or on `agents` for idle Agents or a message
from one of them. Check `meta.wait_reason` and the records. Even on `first` or
timeout, a successful wait returns every target in input order. Message bodies
arrive separately. Use `query_agents({target:work})` for the full instruction and
answer or diagnostic. Read object items by checking `part.type === "object"`.

Reuse the actual AGENT Target from an accepted assignment in a later Cell:

```js
const next = await tools.run_agent([{
  target:{kind:"AGENT",agent_id:"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  instruction:"Review the follow-up changes.",
  materials:[{kind:"CONTEXT_MATERIAL",turns:1}]
}]);
console.log(next);
```

Send messages to an Agent's active turn. Use `stop_agent` to stop work; stopping
a Cell or cancelling a wait leaves accepted Works and tool effects to settle.
