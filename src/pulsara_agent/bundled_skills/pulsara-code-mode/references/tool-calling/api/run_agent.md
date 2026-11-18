# `tools.run_agent(args)`

Submit an array of assignments. Omit target to create an Agent or pass an AGENT Target to queue work for it. Use materials for selected ROOT turns or ended Works and depends_on for successful prerequisites. Accepted records follow assignment order. Use wait_agent or query_agents for progress and answers.

An assignment can include up to 16 WORK materials and 16 dependencies, with at most 64 dependency edges per batch. Instructions and titles have UTF-8 limits of 65,536 and 256 bytes. See [Agent coordination](../../agent-orchestration/coordination.md).

```ts
/**
 * @minItems 1
 * @maxItems 16
 */
export type RunAgentArgs = [Item, ...Item[]];

export interface Item {
  target?: AgentTarget;
  work_title?: string;
  instruction: string;
  materials?: (ContextMaterialTarget | WorkTarget)[];
  /**
   * @maxItems 16
   */
  depends_on?: WorkTarget[];
}
export interface AgentTarget {
  kind: "AGENT";
  agent_id: string;
}
export interface ContextMaterialTarget {
  kind: "CONTEXT_MATERIAL";
  turns: 1 | 2 | 3;
}
export interface WorkTarget {
  kind: "WORK";
  work_id: string;
}
export interface RunAgentSuccess {
  meta: {
    status: "SUCCESS";
  };
  /**
   * @minItems 1
   */
  content: [ObjectPart, ...ObjectPart[]];
}
export interface ObjectPart {
  type: "object";
  value: Value;
}
export interface Value {
  agent: AgentTarget;
  work: WorkTarget;
  work_status: "queued" | "running" | "completed" | "stopped";
}
export interface RunAgentFailure {
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | ObjectPart
  )[];
}

type Assignment = RunAgentArgs[number];
type BatchRefs = {
  agents: NonNullable<Assignment["target"]>[];
  works: NonNullable<Assignment["depends_on"]>;
};
type CallAssignment = Omit<Assignment, "instruction" | "depends_on"> & {
  instruction: Assignment["instruction"] | ((refs: BatchRefs) => Assignment["instruction"]);
  depends_on?: Assignment["depends_on"] | ((refs: BatchRefs) => NonNullable<Assignment["depends_on"]>);
};
declare const tools: {
  run_agent(args: [CallAssignment, ...CallAssignment[]]): Promise<RunAgentSuccess>;
};
```
