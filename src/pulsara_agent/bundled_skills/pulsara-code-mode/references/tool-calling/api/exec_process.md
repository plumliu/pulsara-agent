# `tools.exec_process(args)`

Start a shell command and observe its complete available public output. A PROCESS Target supports later metadata queries and wait/write/control. Initial wait does not limit process lifetime. Native NEXT reads a frozen result; JS returns the full observation.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
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

export type ExecProcessValue =
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
          target?: {
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
  exec_process(args: ExecProcessArgs): Promise<Successful<ExecProcessValue>>;
};
```
