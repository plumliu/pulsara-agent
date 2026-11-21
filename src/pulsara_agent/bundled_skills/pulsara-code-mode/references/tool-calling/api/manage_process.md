# `tools.manage_process(args)`

Observe or control a process in this session. wait: target, optional timeout_seconds (0–30; default 30); write: target,data,optional yield_time_ms (0–30000; default 1000); close_stdin/kill: target only. wait/write return all pending public output and frozen process facts. Nonzero process exit is a successful observation. No action is rerun by NEXT.

```ts
export type ManageProcessArgs =
  | {
      action: "wait";
      target: ProcessTarget;
      timeout_seconds?: number;
    }
  | {
      action: "write";
      /**
       * Exact UTF-8 text; an empty string is allowed.
       */
      data: string;
      target: ProcessTarget;
      /**
       * Wait for process exit after input is flushed; 0 returns immediately.
       */
      yield_time_ms?: number;
    }
  | {
      action: "close_stdin";
      target: ProcessTarget;
    }
  | {
      action: "kill";
      target: ProcessTarget;
    };
export type ManageProcessSuccess =
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
export interface ManageProcessFailure {
  target: ProcessTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  manage_process(args: ManageProcessArgs): Promise<ManageProcessSuccess>;
};
```
