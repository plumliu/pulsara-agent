# `tools.decide(args)`

Evaluate one natural-language yes/no condition independently for each supplied text using the configured Decision model. Returns one content object with {decisions: boolean[]} in input order. Include needed context in each input. tools.decide.available() is a synchronous configuration check fixed at Cell startup. Use JavaScript for exact comparisons. Empty inputs return an empty array.

`available()` checks configuration without testing service health.

```ts
export interface DecideArgs {
  /**
   * One explicit yes/no condition to evaluate independently for each input.
   */
  condition: string;
  /**
   * Texts to judge in order. Include the context needed for each judgment in that text.
   */
  inputs: string[];
}
export interface DecideSuccess {
  meta: {
    status: "SUCCESS";
  };
  /**
   * @minItems 1
   * @maxItems 1
   */
  content: [
    {
      type: "object";
      value: {
        decisions: boolean[];
      };
    }
  ];
}

declare const tools: {
  decide: ((args: DecideArgs) => Promise<DecideSuccess>) & { available(): boolean };
};
```

```js
const condition = "Does this describe an export failure?";
const inputs = ["Export did not create a file.", "The file opens correctly."];
if (tools.decide.available()) {
  const result = await tools.decide({condition, inputs});
  console.log(inputs.filter((_, i) => result.content[0].value.decisions[i]));
} else {
  console.log({condition, inputs});
}
```
