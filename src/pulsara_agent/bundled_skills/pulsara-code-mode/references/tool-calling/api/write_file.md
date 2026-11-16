# `tools.write_file(args)`

Create a new file without overwriting an existing path. Pass target:PATH and exactly one of content or tool_result_target. content writes the supplied text; tool_result_target:SESSION_ENTRY exports that complete saved tool result, including unread pages, as UTF-8. A NEXT target, ordinary message or Cell output is not an export source. The result reports the created path and bytes written.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type WriteFileArgs =
  | {
      content: string;
      target: {
        kind: "PATH";
        path: string;
      };
    }
  | {
      target: {
        kind: "PATH";
        path: string;
      };
      tool_result_target: {
        entry_id: string;
        kind: "SESSION_ENTRY";
      };
    };

export type WriteFileValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
            bytes_written: number;
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
  write_file(args: WriteFileArgs): Promise<Successful<WriteFileValue>>;
};
```
