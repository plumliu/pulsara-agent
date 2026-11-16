# `tools.stop_agent(args)`

Stop target:AGENT or target:WORK. An agent target stops its running and queued work; a work target stops only that work. Submitted tool calls settle their actual effects. meta reports queued_work_count, running_work_count, completed_work_count and stopped_work_count for the listed work. Stopping does not undo effects already performed.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface StopAgentArgs {
  target:
    | {
        agent_id: string;
        kind: "AGENT";
      }
    | {
        kind: "WORK";
        work_id: string;
      };
}

export type StopAgentValue =
  | (
      | {
          meta: {
            status: "SUCCESS";
            queued_work_count: number;
            running_work_count: number;
            completed_work_count: number;
            stopped_work_count: number;
          };
          /**
           * @minItems 1
           */
          content: [
            (
              | {
                  type: "text";
                  value: string;
                }
              | {
                  type: "object";
                  value: {
                    target: {
                      kind: "WORK";
                      work_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                  };
                }
            ),
            ...(
              | {
                  type: "text";
                  value: string;
                }
              | {
                  type: "object";
                  value: {
                    target: {
                      kind: "WORK";
                      work_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                  };
                }
            )[]
          ];
        }
      | {
          meta: {
            status: "ERROR" | "CANCELLED";
            queued_work_count?: number;
            running_work_count?: number;
            completed_work_count?: number;
            stopped_work_count?: number;
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "WORK";
                    work_id: string;
                  };
                  work_status: "queued" | "running" | "completed" | "stopped";
                };
              }
          )[];
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
  stop_agent(args: StopAgentArgs): Promise<Successful<StopAgentValue>>;
};
```
