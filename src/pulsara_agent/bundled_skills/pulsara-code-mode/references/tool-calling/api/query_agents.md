# `tools.query_agents(args)`

Query collaborating agents and work. No target lists agents; target:AGENT lists that agent's work; target:WORK reads one work's submitted answer or stop explanation. Pass only target when selecting one object. A missing title does not establish that the agent is idle.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface QueryAgentsArgs {
  target?:
    | {
        agent_id: string;
        kind: "AGENT";
      }
    | {
        kind: "WORK";
        work_id: string;
      };
}

export type QueryAgentsValue =
  | (
      | {
          meta: {
            status: "SUCCESS";
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
                      kind: "AGENT";
                      agent_id: string;
                    };
                    /**
                     * 当前 Work；没有当前工作时为 null。
                     */
                    current_work: {
                      kind: "WORK";
                      work_id: string;
                    } | null;
                    /**
                     * 当前 Work 标题；空闲或该 Work 无标题时为 null。
                     */
                    current_work_title: string | null;
                    queued_work_count: number;
                    name?: string;
                  };
                }
              | {
                  type: "object";
                  value: {
                    target: {
                      kind: "WORK";
                      work_id: string;
                    };
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                    /**
                     * 本次 Work 的标题；无标题时为 null。
                     */
                    work_title: string | null;
                    instruction?: string;
                    final?: string;
                    diagnostic?: string;
                    failed_dependency?: {
                      kind: "WORK";
                      work_id: string;
                    };
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
                      kind: "AGENT";
                      agent_id: string;
                    };
                    /**
                     * 当前 Work；没有当前工作时为 null。
                     */
                    current_work: {
                      kind: "WORK";
                      work_id: string;
                    } | null;
                    /**
                     * 当前 Work 标题；空闲或该 Work 无标题时为 null。
                     */
                    current_work_title: string | null;
                    queued_work_count: number;
                    name?: string;
                  };
                }
              | {
                  type: "object";
                  value: {
                    target: {
                      kind: "WORK";
                      work_id: string;
                    };
                    agent: {
                      kind: "AGENT";
                      agent_id: string;
                    };
                    work_status: "queued" | "running" | "completed" | "stopped";
                    /**
                     * 本次 Work 的标题；无标题时为 null。
                     */
                    work_title: string | null;
                    instruction?: string;
                    final?: string;
                    diagnostic?: string;
                    failed_dependency?: {
                      kind: "WORK";
                      work_id: string;
                    };
                  };
                }
            )[]
          ];
        }
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
                  target: {
                    kind: "AGENT";
                    agent_id: string;
                  };
                  /**
                   * 当前 Work；没有当前工作时为 null。
                   */
                  current_work: {
                    kind: "WORK";
                    work_id: string;
                  } | null;
                  /**
                   * 当前 Work 标题；空闲或该 Work 无标题时为 null。
                   */
                  current_work_title: string | null;
                  queued_work_count: number;
                  name?: string;
                };
              }
            | {
                type: "object";
                value: {
                  target: {
                    kind: "WORK";
                    work_id: string;
                  };
                  agent: {
                    kind: "AGENT";
                    agent_id: string;
                  };
                  work_status: "queued" | "running" | "completed" | "stopped";
                  /**
                   * 本次 Work 的标题；无标题时为 null。
                   */
                  work_title: string | null;
                  instruction?: string;
                  final?: string;
                  diagnostic?: string;
                  failed_dependency?: {
                    kind: "WORK";
                    work_id: string;
                  };
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
  query_agents(args: QueryAgentsArgs): Promise<Successful<QueryAgentsValue>>;
};
```
