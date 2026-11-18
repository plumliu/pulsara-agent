# `tools.search_session_content(args)`

Search saved dialogue by relevance. Pass target:SESSION and query. Results include user messages and steers, assistant commentary and final answers; tools and private reasoning are excluded. Each hit anchors a saved entry for read_session_content. Use view_image to inspect a referenced image.

```ts
export interface SearchSessionContentArgs {
  query: string;
  target: {
    kind: "SESSION";
    session_id: string;
  };
}
export interface SearchSessionContentSuccess {
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
          target: {
            kind: "SESSION_ENTRY";
            entry_id: string;
          };
          text: string;
        };
      }
  )[];
}
export type SearchSessionContentFailure = Omit<SearchSessionContentSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  search_session_content(args: SearchSessionContentArgs): Promise<SearchSessionContentSuccess>;
};
```
