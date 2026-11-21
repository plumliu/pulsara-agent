# `tools.exec_process(args)`

Start a shell command and observe its complete available public output. meta.status SUCCESS means the observation succeeded. Check meta.process_status and meta.exit_code for the command outcome. RUNNING has exit_code null. EXITED reports the exit code, with 0 indicating a successful exit. Interpret nonzero codes according to the command. A PROCESS Target supports later metadata queries and wait/write/control. Initial wait does not limit process lifetime. Native NEXT reads a frozen result. JS returns the full observation.

```ts
export type ExecProcessSuccess =
  | {
      target: ProcessTarget;
      meta: {
        status: "SUCCESS";
        process_status: "RUNNING";
        /**
         * The process was still running at observation time.
         */
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
        /**
         * Observed exit code. Zero indicates a successful exit.
         */
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
