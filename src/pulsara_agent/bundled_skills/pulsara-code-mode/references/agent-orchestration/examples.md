# Agent examples

## 1. Investigate one task

```js
const accepted = await tools.run_agent([{instruction:"Inspect the parser and report concrete defects."}]);
console.log(accepted);
const work = accepted.content[0].value.work;
console.log(await tools.wait_agent({works:[work],timeout_seconds:60}));
console.log(await tools.query_agents({target:work}));
```

## 2. Give teammates actual identities

```js
const team = await tools.run_agent([
  {instruction:({agents})=>"Review requirements; coordinate with "+JSON.stringify(agents[1])},
  {instruction:({agents})=>"Review edge cases; coordinate with "+JSON.stringify(agents[0])}
]);
console.log(team);
```

## 3. Require a successful prerequisite

```js
const team = await tools.run_agent([
  {instruction:"Investigate the defect."},
  {instruction:"Review the prerequisite's evidence.",depends_on:({works})=>[works[0]]}
]);
console.log(await tools.wait_agent({works:team.content.map(p=>p.value.work),settle:"all"}));
```

## 4. Reuse an Agent in another Cell

After ROOT has received the actual AGENT Target, copy it into the new script:

```js
const next = await tools.run_agent([{
  target:{kind:"AGENT",agent_id:"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  instruction:"Review the latest changes against the new requirements.",
  materials:[{kind:"CONTEXT_MATERIAL",turns:1}]
}]);
console.log(next);
```

The identity above illustrates Target shape; use the actual returned identity.
Every Cell has private variables. Native query_agents can recover visible Targets.
