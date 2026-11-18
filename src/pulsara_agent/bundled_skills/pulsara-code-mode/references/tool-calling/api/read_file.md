# `tools.read_file(args)`

Read a local UTF-8 file. Pass target:PATH with optional nonzero offset and positive limit in lines. Omission reads the full file; negative offset locates from the tail while preserving original line order. Returns the complete saved business value without native pages or NEXT.

```ts
export interface ReadFileArgs {
  limit?: number;
  offset?: number;
  target: PathTarget;
}
export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface ReadFileSuccess {
  target: PathTarget;
  meta: {
    status: "SUCCESS";
    total_lines: number;
    file_size: number;
    had_utf8_bom: boolean;
  };
  content: TextPart[];
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface ReadFileFailure {
  target: PathTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  read_file(args: ReadFileArgs): Promise<ReadFileSuccess>;
};
```
