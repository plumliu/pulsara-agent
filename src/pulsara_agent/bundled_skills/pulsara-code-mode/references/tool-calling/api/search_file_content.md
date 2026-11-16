# `tools.search_file_content(args)`

Search file text with a regular expression and return matching lines with paths and line numbers. Pass pattern, optional target:PATH, file_glob and output_mode. An omitted target searches the workspace; file_glob filters relative paths. output_mode selects matching lines, matching file paths, or counts. The complete search result is saved before display pagination. Use find_files to search names instead.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface SearchFileContentArgs {
  file_glob?: string;
  output_mode?: "content" | "files_only" | "count";
  pattern: string;
  target?: {
    kind: "PATH";
    path: string;
  };
}

export type SearchFileContentValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "target";
                value: {
                  kind: "PATH";
                  path: string;
                };
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "PATH";
                    path: string;
                  };
                  line: number;
                  text: string;
                };
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "PATH";
                    path: string;
                  };
                  count: number;
                };
              }
          )[];
        }
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "ERROR" | "CANCELLED";
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "target";
                value: {
                  kind: "PATH";
                  path: string;
                };
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "PATH";
                    path: string;
                  };
                  line: number;
                  text: string;
                };
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "PATH";
                    path: string;
                  };
                  count: number;
                };
              }
          )[];
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
  search_file_content(args: SearchFileContentArgs): Promise<Successful<SearchFileContentValue>>;
};
```
