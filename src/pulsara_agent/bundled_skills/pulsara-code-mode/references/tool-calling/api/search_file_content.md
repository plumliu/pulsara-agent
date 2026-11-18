# `tools.search_file_content(args)`

Search file text with a regular expression and return matching lines with paths and line numbers. Pass pattern, optional target:PATH, file_glob and output_mode. An omitted target searches the workspace; file_glob filters relative paths. output_mode selects matching lines, matching file paths, or counts. The complete search result is saved before display pagination. Use find_files to search names instead.

```ts
export interface SearchFileContentArgs {
  file_glob?: string;
  output_mode?: "content" | "files_only" | "count";
  pattern: string;
  target?: PathTarget;
}
export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface SearchFileContentSuccess {
  target: PathTarget;
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
        value: PathTarget;
      }
    | {
        type: "object";
        value: {
          target: PathTarget;
          line: number;
          text: string;
        };
      }
    | {
        type: "object";
        value: {
          target: PathTarget;
          count: number;
        };
      }
  )[];
}
export type SearchFileContentFailure = Omit<SearchFileContentSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  search_file_content(args: SearchFileContentArgs): Promise<SearchFileContentSuccess>;
};
```
