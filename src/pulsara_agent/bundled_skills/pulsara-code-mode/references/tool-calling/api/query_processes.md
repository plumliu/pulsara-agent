# `tools.query_processes(args)`

Query this session's retained process metadata without reading or consuming logs. Omit target to list all current records, or provide one PROCESS Target.

```ts
export type QueryProcessesArgs =
  | {}
  | {
      target: ProcessTarget;
    };

export interface ProcessTarget {
  kind: "PROCESS";
  /**
   * Exact process_id from this terminal environment.
   */
  process_id: string;
}
export interface QueryProcessesSuccess {
  meta: {
    status: "SUCCESS";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | {
        type: "object";
        value:
          | {
              target: ProcessTarget;
              command: string;
              cwd: string;
              stdin_closed: boolean;
              duration_seconds: number;
              process_status: "RUNNING";
              /**
               * The process was still running at observation time.
               */
              exit_code: null;
            }
          | {
              target: ProcessTarget;
              command: string;
              cwd: string;
              stdin_closed: boolean;
              duration_seconds: number;
              process_status: "EXITED";
              /**
               * Observed exit code. Zero indicates a successful exit.
               */
              exit_code: number;
            };
      }
  )[];
}

declare const tools: {
  query_processes(args: QueryProcessesArgs): Promise<QueryProcessesSuccess>;
};
```
