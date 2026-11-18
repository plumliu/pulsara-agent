# `tools.write_file(args)`

Create a new file without overwriting an existing path. Pass target:PATH and exactly one of content or tool_result_target. content writes the supplied text; tool_result_target:SESSION_ENTRY exports that complete saved tool result, including unread pages, as UTF-8. A NEXT target, ordinary message or Cell output is not an export source. The result reports the created path and bytes written.

```ts
export type WriteFileArgs =
  | {
      content: string;
      target: PathTarget;
    }
  | {
      target: PathTarget;
      tool_result_target: {
        entry_id: string;
        kind: "SESSION_ENTRY";
      };
    };

export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface WriteFileSuccess {
  target: PathTarget;
  meta: {
    status: "SUCCESS";
    bytes_written: number;
  };
  content: TextPart[];
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface WriteFileFailure {
  target: PathTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  write_file(args: WriteFileArgs): Promise<WriteFileSuccess>;
};
```
