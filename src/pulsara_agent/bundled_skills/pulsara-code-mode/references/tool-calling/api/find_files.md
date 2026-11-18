# `tools.find_files(args)`

Find files and directories by name or path pattern. Pass glob and optional target:PATH; an omitted target searches the workspace. Results contain paths, not file contents. Use search_file_content for text inside files.

```ts
export interface FindFilesArgs {
  glob: string;
  target?: PathTarget;
}
export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface FindFilesSuccess {
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
  )[];
}
export type FindFilesFailure = Omit<FindFilesSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  find_files(args: FindFilesArgs): Promise<FindFilesSuccess>;
};
```
