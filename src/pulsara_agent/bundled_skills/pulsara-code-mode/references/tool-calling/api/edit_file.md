# `tools.edit_file(args)`

Edit an existing UTF-8 text file. Exact replacement: target:PATH, old_text, new_text; old_text must match exactly once, counting overlapping occurrences. Whole-file replacement: target:PATH, content. Use only one branch's fields. Empty content empties the file. Reading first is optional. The result contains the complete diff; a concurrent file change detected before commit rejects the edit.

```ts
export type EditFileArgs =
  | {
      content: string;
      target: PathTarget;
    }
  | {
      new_text: string;
      old_text: string;
      target: PathTarget;
    };

export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface EditFileSuccess {
  target: PathTarget;
  meta: {
    status: "SUCCESS";
  };
  content: {
    type: "text";
    value: string;
  }[];
}
export type EditFileFailure = Omit<EditFileSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  edit_file(args: EditFileArgs): Promise<EditFileSuccess>;
};
```
