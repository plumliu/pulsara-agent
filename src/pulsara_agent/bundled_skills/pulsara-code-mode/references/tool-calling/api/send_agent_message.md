# `tools.send_agent_message(args)`

Send a message to target:AGENT, with message. An active recipient receives it during its current work; an idle recipient is not started by the message. A recipient without a started work cannot receive it. Unconsumed messages are rejected when the intended recipient turn ends; they are not redirected to a later task.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface SendAgentMessageArgs {
  message: string;
  target: {
    agent_id: string | "ROOT";
    kind: "AGENT";
  };
}

export type SendAgentMessageValue =
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
              type: "text";
              value: string;
            },
            ...{
              type: "text";
              value: string;
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
  send_agent_message(args: SendAgentMessageArgs): Promise<Successful<SendAgentMessageValue>>;
};
```
