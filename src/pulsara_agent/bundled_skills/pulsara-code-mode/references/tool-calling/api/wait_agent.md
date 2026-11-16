# `tools.wait_agent(args)`

Wait in ROOT Code Mode with exactly one of works:WORK[] or agents:AGENT[]. settle is all (default) or first; timeout_seconds is 0–300 (default 30). WORK waits for terminal states, AGENT for idle or a selected sender's message. Already satisfied targets count immediately. Success returns every target in input order: WORK records have agent/work/work_status; AGENT records have agent/is_idle. meta.wait_reason explains return; timeout is successful observation, not work completion. Message bodies arrive separately in normal conversation input; failures reject with err.result when available.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export type WaitAgentArgs =
  | {
      settle?: "all" | "first";
      timeout_seconds?: number;
      /**
       * @minItems 1
       */
      works: [
        {
          kind: "WORK";
          work_id: string;
        },
        ...{
          kind: "WORK";
          work_id: string;
        }[]
      ];
    }
  | {
      /**
       * @minItems 1
       */
      agents: [
        {
          agent_id: string;
          kind: "AGENT";
        },
        ...{
          agent_id: string;
          kind: "AGENT";
        }[]
      ];
      settle?: "all" | "first";
      timeout_seconds?: number;
    };

export type WaitAgentValue =
  | (
      | (
          | {
              meta: {
                status: "SUCCESS";
                wait_reason: "condition_met" | "timeout";
              };
              /**
               * @minItems 1
               */
              content: [
                {
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    work: {
                      kind: "WORK";
                      work_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                  };
                },
                ...{
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    work: {
                      kind: "WORK";
                      work_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                  };
                }[]
              ];
            }
          | {
              meta: {
                status: "SUCCESS";
                wait_reason: "condition_met" | "timeout";
              };
              /**
               * @minItems 1
               */
              content: [
                {
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    is_idle: boolean;
                  };
                },
                ...{
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    is_idle: boolean;
                  };
                }[]
              ];
            }
          | {
              meta: {
                status: "SUCCESS";
                wait_reason: "message_available";
                /**
                 * @minItems 1
                 */
                message_sources: [
                  {
                    kind: "AGENT";
                    agent_id: string;
                  },
                  ...{
                    kind: "AGENT";
                    agent_id: string;
                  }[]
                ];
              };
              /**
               * @minItems 1
               */
              content: [
                {
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    is_idle: boolean;
                  };
                },
                ...{
                  type: "object";
                  value: {
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    is_idle: boolean;
                  };
                }[]
              ];
            }
        )
      | {
          meta: {
            status: "ERROR" | "CANCELLED";
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "object";
                value: {
                  agent: {
                    kind: "AGENT";
                    agent_id: string;
                  };
                  work: {
                    kind: "WORK";
                    work_id: string;
                  };
                  work_status: "queued" | "running" | "completed" | "stopped";
                };
              }
            | {
                type: "object";
                value: {
                  agent: {
                    kind: "AGENT";
                    agent_id: string;
                  };
                  is_idle: boolean;
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
  wait_agent(args: WaitAgentArgs): Promise<Successful<WaitAgentValue>>;
};
```
