# `tools.edit_file(args)`

Edit an existing UTF-8 text file. Exact replacement: target:PATH, old_text, new_text; old_text must match exactly once, counting overlapping occurrences. Whole-file replacement: target:PATH, content. Use only one branch's fields. Empty content empties the file. Reading first is optional. The result contains the complete diff; a concurrent file change detected before commit rejects the edit.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type EditFileArgs =
  | {
      content: string;
      target: {
        kind: "PATH";
        path: string;
      };
    }
  | {
      new_text: string;
      old_text: string;
      target: {
        kind: "PATH";
        path: string;
      };
    };

export type EditFileValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
      | {
          target: {
            kind: "PATH";
            path: string;
          };
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
  edit_file(args: EditFileArgs): Promise<Successful<EditFileValue>>;
};
```
