# `tools.query_processes(args)`

Query this session's retained process metadata without reading or consuming logs. Omit target to list all current records, or provide one PROCESS Target.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type QueryProcessesArgs =
  | {}
  | {
      target: {
        kind: "PROCESS";
        /**
         * Exact process_id from this terminal environment.
         */
        process_id: string;
      };
    };

export type QueryProcessesValue =
  | (
      | {
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
                      target: {
                        kind: "PROCESS";
                        /**
                         * Exact process_id from this terminal environment.
                         */
                        process_id: string;
                      };
                      command: string;
                      cwd: string;
                      stdin_closed: boolean;
                      duration_seconds: number;
                      process_status: "RUNNING";
                      exit_code: null;
                    }
                  | {
                      target: {
                        kind: "PROCESS";
                        /**
                         * Exact process_id from this terminal environment.
                         */
                        process_id: string;
                      };
                      command: string;
                      cwd: string;
                      stdin_closed: boolean;
                      duration_seconds: number;
                      process_status: "EXITED";
                      exit_code: number;
                    };
              }
          )[];
        }
      | {
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
  query_processes(args: QueryProcessesArgs): Promise<Successful<QueryProcessesValue>>;
};
```
