# `tools.query_agents(args)`

Query collaborating agents and work. No target lists agents; target:AGENT lists that agent's work; target:WORK reads one work's submitted answer or stop explanation. Pass only target when selecting one object. A missing title does not establish that the agent is idle.

```ts
export interface QueryAgentsArgs {
  target?: AgentTarget | WorkTarget;
}
export interface AgentTarget {
  agent_id: string;
  kind: "AGENT";
}
export interface WorkTarget {
  kind: "WORK";
  work_id: string;
}
export interface QueryAgentsSuccess {
  meta: {
    status: "SUCCESS";
  };
  /**
   * @minItems 1
   */
  content: [TextPart | ObjectPart | ObjectPart2, ...(TextPart | ObjectPart | ObjectPart2)[]];
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
  target: AgentTarget;
  /**
   * 当前 Work；没有当前工作时为 null。
   */
  current_work: WorkTarget | null;
  /**
   * 当前 Work 标题；空闲或该 Work 无标题时为 null。
   */
  current_work_title: string | null;
  queued_work_count: number;
  name?: string;
}
export interface ObjectPart2 {
  type: "object";
  value: Value2;
}
export interface Value2 {
  target: WorkTarget;
  agent: AgentTarget;
  work_status: "queued" | "running" | "completed" | "stopped";
  /**
   * 本次 Work 的标题；无标题时为 null。
   */
  work_title: string | null;
  instruction?: string;
  final?: string;
  diagnostic?: string;
  failed_dependency?: WorkTarget;
}
export interface QueryAgentsFailure {
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: (TextPart | ObjectPart | ObjectPart2)[];
}

declare const tools: {
  query_agents(args: QueryAgentsArgs): Promise<QueryAgentsSuccess>;
};
```
