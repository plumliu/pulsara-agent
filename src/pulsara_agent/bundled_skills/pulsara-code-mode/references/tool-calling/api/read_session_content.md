# `tools.read_session_content(args)`

Read saved dialogue. Pass target and required nonzero limit. SESSION: limit must be negative and reads from the latest message backwards. SESSION_ENTRY: positive reads forwards from that entry, negative reads backwards; either includes the anchor. Results preserve the chosen message order and labels. Tool calls, tool results and private reasoning are excluded. Images remain references until view_image is called.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type ReadSessionContentArgs =
  | {
      limit: number;
      target: {
        kind: "SESSION";
        session_id: string;
      };
    }
  | {
      limit: number;
      target: {
        entry_id: string;
        kind: "SESSION_ENTRY";
      };
    };

export type ReadSessionContentValue =
  | {
      meta: {
        status: "SUCCESS" | "ERROR" | "CANCELLED";
      };
      content: (
        | {
            type: "text";
            value: string;
          }
        | {
            type: "object";
            value: {
              target: {
                kind: "SESSION_ENTRY";
                entry_id: string;
              };
              role:
                "user message" | "user steer" | "assistant commentary" | "assistant final answer";
              at: string;
              text: string;
            };
          }
      )[];
    }
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
  read_session_content(args: ReadSessionContentArgs): Promise<Successful<ReadSessionContentValue>>;
};
```
