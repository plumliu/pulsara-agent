# `tools.find_files(args)`

Find files and directories by name or path pattern. Pass glob and optional target:PATH; an omitted target searches the workspace. Results contain paths, not file contents. Use search_file_content for text inside files.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface FindFilesArgs {
  glob: string;
  target?: {
    kind: "PATH";
    path: string;
  };
}

export type FindFilesValue =
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
  find_files(args: FindFilesArgs): Promise<Successful<FindFilesValue>>;
};
```
