# `tools.read_file(args)`

Read a local UTF-8 file with target:PATH and optional offset and limit in lines. The complete selected range is returned in JavaScript. meta.total_lines counts the whole file. To select later lines, make another PATH call with offset.

```ts
export interface ReadFileArgs {
  /**
   * Positive maximum number of lines selected by this call. Omit to read through the end of the file.
   */
  limit?: number;
  /**
   * Starting LF line (nonzero, default 1). Negative offsets count from the end. Reading proceeds forward.
   */
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
    /**
     * Total LF lines in the source file.
     */
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
