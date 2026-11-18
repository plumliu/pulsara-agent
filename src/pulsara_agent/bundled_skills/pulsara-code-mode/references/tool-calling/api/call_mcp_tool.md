# `tools.call_mcp_tool(args)`

Call an MCP tool with invocation.target from query_capabilities. Pass arguments matching its input_schema. Read the interface before writing the call. Optional output_schema describes successful structuredContent. The reference works across native calls and Cells in this session. Query again if it becomes stale. Check uncertain effects before retrying.

```ts
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export interface CallMcpToolArgs {
  /**
   * Arguments matching the inspected input_schema; use {} when no inputs are needed.
   */
  arguments: {
    [k: string]: JsonValue;
  };
  target: {
    kind: "MCP_TOOL_REF";
    /**
     * Canonical mcpref ID copied from invocation.target, scoped to this agent and conversation.
     */
    tool_ref: string;
  };
}
export interface CallMcpToolSuccess {
  /**
   * 远端原样返回的任意 JSON 值；缺失与 null 不同。按实际 output_schema 理解成功值，失败值不假定符合成功 schema。
   */
  structuredContent?: JsonValue;
  meta: {
    status: "SUCCESS";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | {
        type: "image";
        value: {
          kind: "IMAGE_REF";
          image_ref: string;
        };
      }
  )[];
}
export type CallMcpToolFailure = Omit<CallMcpToolSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  call_mcp_tool(args: CallMcpToolArgs): Promise<CallMcpToolSuccess>;
};
```
