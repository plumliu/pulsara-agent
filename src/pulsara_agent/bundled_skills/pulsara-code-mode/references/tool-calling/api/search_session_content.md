# `tools.search_session_content(args)`

Search saved dialogue by relevance. Pass target:SESSION and query. Results include user messages and steers, assistant commentary and final answers; tools and private reasoning are excluded. Each hit anchors a saved entry for read_session_content. Use view_image to inspect a referenced image.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface SearchSessionContentArgs {
  query: string;
  target: {
    kind: "SESSION";
    session_id: string;
  };
}

export type SearchSessionContentValue =
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
  search_session_content(args: SearchSessionContentArgs): Promise<Successful<SearchSessionContentValue>>;
};
```
