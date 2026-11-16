# `tools.run_agent(args)`

Submit an ordered array of assignments through tools.run_agent in ROOT Code Mode. Each item needs instruction; target:AGENT reuses an agent, omission creates one. Optional work_title names this Work. materials accepts CONTEXT_MATERIAL with turns 1–3 or a completed WORK, for new and reused agents. depends_on waits for the specified Work results. Success content records preserve assignment order and contain agent, work and work_status. Acceptance is not completion; use wait_agent or query_agents for progress and answers.

Pass one ordered assignments array, including for a single assignment. The generated argument type describes the resolved assignments. Before dispatch, `instruction` and `depends_on` may also be synchronous JavaScript functions of preallocated `{agents,works}` Targets. They return a string and a WORK Target array respectively; they cannot call tools, await, or return a Promise.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
/**
 * @minItems 1
 * @maxItems 16
 */
export type RunAgentArgs = [
  {
    target?: {
      kind: "AGENT";
      agent_id: string;
    };
    work_title?: string;
    instruction: string;
    materials?: (
      | {
          kind: "CONTEXT_MATERIAL";
          turns: 1 | 2 | 3;
        }
      | {
          kind: "WORK";
          work_id: string;
        }
    )[];
    /**
     * @maxItems 16
     */
    depends_on?: {
      kind: "WORK";
      work_id: string;
    }[];
  },
  ...{
    target?: {
      kind: "AGENT";
      agent_id: string;
    };
    work_title?: string;
    instruction: string;
    materials?: (
      | {
          kind: "CONTEXT_MATERIAL";
          turns: 1 | 2 | 3;
        }
      | {
          kind: "WORK";
          work_id: string;
        }
    )[];
    /**
     * @maxItems 16
     */
    depends_on?: {
      kind: "WORK";
      work_id: string;
    }[];
  }[]
];

export type RunAgentValue =
  | (
      | {
          meta: {
            status: "SUCCESS";
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

type Assignment = RunAgentArgs[number];
type BatchRefs = {
  agents: NonNullable<Assignment["target"]>[];
  works: NonNullable<Assignment["depends_on"]>;
};
type CallAssignment = Omit<Assignment, "instruction" | "depends_on"> & {
  instruction: Assignment["instruction"] | ((refs: BatchRefs) => Assignment["instruction"]);
  depends_on?: Assignment["depends_on"] | ((refs: BatchRefs) => NonNullable<Assignment["depends_on"]>);
};
type Successful<T> = T extends { meta: { status: infer S } }
  ? "SUCCESS" extends S ? T & { meta: { status: "SUCCESS" } } : never
  : never;

declare const tools: {
  run_agent(args: CallAssignment[]): Promise<Successful<RunAgentValue>>;
};
```
