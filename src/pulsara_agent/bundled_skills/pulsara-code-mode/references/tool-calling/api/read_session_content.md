# `tools.read_session_content(args)`

Read saved dialogue. Pass target and required nonzero limit. SESSION: limit must be negative and reads from the latest message backwards. SESSION_ENTRY: positive reads forwards from that entry, negative reads backwards; either includes the anchor. Results preserve the chosen message order and labels. Tool calls, tool results and private reasoning are excluded. Images remain references until view_image is called.

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
      target: SessionEntryTarget;
    };

export interface SessionEntryTarget {
  entry_id: string;
  kind: "SESSION_ENTRY";
}
export interface ReadSessionContentSuccess {
  meta: {
    status: "SUCCESS";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | {
        type: "object";
        value: {
          target: SessionEntryTarget;
          role: "user message" | "user steer" | "assistant commentary" | "assistant final answer";
          at: string;
          text: string;
        };
      }
  )[];
}
export type ReadSessionContentFailure = Omit<ReadSessionContentSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  read_session_content(args: ReadSessionContentArgs): Promise<ReadSessionContentSuccess>;
};
```
