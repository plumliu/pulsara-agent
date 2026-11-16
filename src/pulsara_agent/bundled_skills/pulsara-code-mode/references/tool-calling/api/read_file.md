# `tools.read_file(args)`

Read a local UTF-8 file. Pass target:PATH with optional nonzero offset and positive limit in lines. Omission reads the full file; negative offset locates from the tail while preserving original line order. Returns the complete saved business value without native pages or NEXT.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface ReadFileArgs {
  limit?: number;
  offset?: number;
  target: {
    kind: "PATH";
    path: string;
  };
}

export type ReadFileValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
            total_lines: number;
            file_size: number;
            had_utf8_bom: boolean;
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
  read_file(args: ReadFileArgs): Promise<Successful<ReadFileValue>>;
};
```
