# `tools.decide(args)`

Evaluate one natural-language yes/no condition independently for each supplied text using the configured Decision model. Returns one content object with {decisions: boolean[]} in input order. Compose with any tools or agent operations for filtering, branching, loops or further calls. Include needed context in each input. Guard calls with if (tools.decide.available()); this local synchronous method reports configuration availability captured at Cell startup. Otherwise pass the condition and material to ROOT to continue. Use JavaScript for exact comparisons. Empty inputs return an empty array. Service failures reject the call. Decision configuration is independent of memory reranking.

`tools.decide.available()` is a local synchronous boolean, frozen at Cell startup; it does not call a service or test service health. Actual decide calls are asynchronous. The successful decisions array follows inputs order.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

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

export type DecideValue =
  | (
      | {
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
            },
            ...{
              type: "object";
              value: {
                decisions: boolean[];
              };
            }[]
          ];
        }
      | {
          meta: {
            status: "ERROR" | "CANCELLED";
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
    )
  | {
      meta: {
        status: "ERROR" | "CANCELLED";
      };
      content: {
        type: "text";
        value: string;
      }[];
    };

type Successful<T> = T extends { meta: { status: infer S } }
  ? "SUCCESS" extends S ? T & { meta: { status: "SUCCESS" } } : never
  : never;

declare const tools: {
  decide: ((args: DecideArgs) => Promise<Successful<DecideValue>>) & { available(): boolean };
};
```
