# `tools.wait_agent(args)`

Wait with either works or agents. settle is all (default) or first; timeout_seconds is 0–300 (default 30). WORK waits for terminal state. AGENT waits for idle or a message from one of the specified Agents. Already satisfied targets count immediately. Check meta.wait_reason and the records, which cover all targets in input order.

```ts
export type WaitAgentArgs =
  | {
      settle?: "all" | "first";
      timeout_seconds?: number;
      /**
       * @minItems 1
       */
      works: [WorkTarget, ...WorkTarget[]];
    }
  | {
      /**
       * @minItems 1
       */
      agents: [AgentTarget, ...AgentTarget[]];
      settle?: "all" | "first";
      timeout_seconds?: number;
    };
export type WaitAgentSuccess =
  | {
      meta: Meta;
      /**
       * @minItems 1
       */
      content: [ObjectPart, ...ObjectPart[]];
    }
  | {
      meta: Meta;
      /**
       * @minItems 1
       */
      content: [ObjectPart2, ...ObjectPart2[]];
    }
  | {
      meta: {
        status: "SUCCESS";
        wait_reason: "message_available";
        /**
         * @minItems 1
         */
        message_sources: [AgentTarget, ...AgentTarget[]];
      };
      /**
       * @minItems 1
       */
      content: [ObjectPart2, ...ObjectPart2[]];
    };

export interface WorkTarget {
  kind: "WORK";
  work_id: string;
}
export interface AgentTarget {
  agent_id: string;
  kind: "AGENT";
}
export interface Meta {
  status: "SUCCESS";
  wait_reason: "condition_met" | "timeout";
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
export interface ObjectPart2 {
  type: "object";
  value: Value2;
}
export interface Value2 {
  agent: AgentTarget;
  is_idle: boolean;
}
export interface WaitAgentFailure {
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | ObjectPart
    | ObjectPart2
  )[];
}

declare const tools: {
  wait_agent(args: WaitAgentArgs): Promise<WaitAgentSuccess>;
};
```
