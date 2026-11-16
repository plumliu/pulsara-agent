# `tools.manage_process(args)`

Observe or control a process in this session. wait: target, optional timeout_seconds (0–30; default 30); write: target,data,optional yield_time_ms (0–30000; default 1000); close_stdin/kill: target only. wait/write return all pending public output and frozen process facts. Nonzero process exit is a successful observation. No action is rerun by NEXT.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type ManageProcessArgs =
  | {
      action: "wait";
      target: {
        kind: "PROCESS";
        /**
         * Exact process_id from this terminal environment.
         */
        process_id: string;
      };
      timeout_seconds?: number;
    }
  | {
      action: "write";
      /**
       * Exact UTF-8 text; an empty string is allowed.
       */
      data: string;
      target: {
        kind: "PROCESS";
        /**
         * Exact process_id from this terminal environment.
         */
        process_id: string;
      };
      /**
       * Wait for process exit after input is flushed; 0 returns immediately.
       */
      yield_time_ms?: number;
    }
  | {
      action: "close_stdin";
      target: {
        kind: "PROCESS";
        /**
         * Exact process_id from this terminal environment.
         */
        process_id: string;
      };
    }
  | {
      action: "kill";
      target: {
        kind: "PROCESS";
        /**
         * Exact process_id from this terminal environment.
         */
        process_id: string;
      };
    };

export type ManageProcessValue =
  | (
      | {
          target: {
            kind: "PROCESS";
            /**
             * Exact process_id from this terminal environment.
             */
            process_id: string;
          };
          meta: {
            status: "SUCCESS";
            process_status: "RUNNING";
            exit_code: null;
            cwd: string;
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
      | {
          target: {
            kind: "PROCESS";
            /**
             * Exact process_id from this terminal environment.
             */
            process_id: string;
          };
          meta: {
            status: "SUCCESS";
            process_status: "EXITED";
            exit_code: number;
            cwd: string;
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
      | {
          target: {
            kind: "PROCESS";
            /**
             * Exact process_id from this terminal environment.
             */
            process_id: string;
          };
          meta: {
            status: "ERROR" | "CANCELLED";
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
    )
  | {
      meta: {
        status: "ERROR" | "CANCELLED";
      };
      content: {
        type: "text";
        value: string;
      }[];
    };

type Successful<T> = T extends { meta: { status: infer S } }
  ? "SUCCESS" extends S ? T & { meta: { status: "SUCCESS" } } : never
  : never;

declare const tools: {
  manage_process(args: ManageProcessArgs): Promise<Successful<ManageProcessValue>>;
};
```
