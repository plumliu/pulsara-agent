# `tools.search_sessions(args)`

Find saved conversations by title or dialogue keywords; omit query to list conversations by activity. Optional lifecycle selects open or archived conversations. Body matches in this conversation cover earlier content outside the current context; other accessible conversations use saved dialogue. SESSION targets identify conversations; SESSION_ENTRY targets anchor dialogue hits. Images are references, not automatically loaded.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface SearchSessionsArgs {
  lifecycle?: "ALL" | "OPEN" | "ARCHIVED";
  query?: string;
}

export type SearchSessionsValue =
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
                kind: "SESSION";
                session_id: string;
              };
              title: string;
              workspace_path: string;
              updated_at: string;
              matched_in?: "ENTRY" | "TITLE";
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
  search_sessions(args: SearchSessionsArgs): Promise<Successful<SearchSessionsValue>>;
};
```
