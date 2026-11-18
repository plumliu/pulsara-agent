# `tools.exec_process(args)`

Start a shell command and observe its complete available public output. A PROCESS Target supports later metadata queries and wait/write/control. Initial wait does not limit process lifetime. Native NEXT reads a frozen result; JS returns the full observation.

```ts
export type ExecProcessSuccess =
  | {
      target: ProcessTarget;
      meta: {
        status: "SUCCESS";
        process_status: "RUNNING";
        exit_code: null;
        cwd: string;
      };
      content: TextPart[];
    }
  | {
      target: ProcessTarget;
      meta: {
        status: "SUCCESS";
        process_status: "EXITED";
        exit_code: number;
        cwd: string;
      };
      content: TextPart[];
    };

export interface ExecProcessArgs {
  command: string;
  /**
   * Enable terminal interaction for programs needing line editing or a full-screen interface.
   */
  tty?: boolean;
  /**
   * Existing directory for this call; workspace-relative, absolute and ~ paths are supported. Omit for the workspace root.
   */
  workdir?: string;
  /**
   * Initial observation wait; 0 returns immediately.
   */
  yield_time_ms?: number;
}
export interface ProcessTarget {
  kind: "PROCESS";
  /**
   * Exact process_id from this terminal environment.
   */
  process_id: string;
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface ExecProcessFailure {
  target?: ProcessTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  exec_process(args: ExecProcessArgs): Promise<ExecProcessSuccess>;
};
```
