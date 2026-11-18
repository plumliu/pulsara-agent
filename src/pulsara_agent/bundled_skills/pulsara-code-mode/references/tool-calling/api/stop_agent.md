# `tools.stop_agent(args)`

Stop target:AGENT or target:WORK. An agent target stops its running and queued work; a work target stops only that work. Submitted tool calls settle their actual effects. meta reports queued_work_count, running_work_count, completed_work_count and stopped_work_count for the listed work. Stopping does not undo effects already performed.

```ts
export interface StopAgentArgs {
  target:
    | {
        agent_id: string;
        kind: "AGENT";
      }
    | WorkTarget;
}
export interface WorkTarget {
  kind: "WORK";
  work_id: string;
}
export interface StopAgentSuccess {
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
  content: [TextPart | ObjectPart, ...(TextPart | ObjectPart)[]];
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface ObjectPart {
  type: "object";
  value: Value;
}
export interface Value {
  target: WorkTarget;
  work_status: "queued" | "running" | "completed" | "stopped";
}
export interface StopAgentFailure {
  meta: {
    status: "ERROR" | "CANCELLED";
    queued_work_count?: number;
    running_work_count?: number;
    completed_work_count?: number;
    stopped_work_count?: number;
  };
  content: (TextPart | ObjectPart)[];
}

declare const tools: {
  stop_agent(args: StopAgentArgs): Promise<StopAgentSuccess>;
};
```
