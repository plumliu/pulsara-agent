# `tools.call_mcp_tool(args)`

Call an MCP tool using the invocation.target returned by query_capabilities. Supply arguments matching its input_schema. Read the relevant parameter and available output descriptions before composing calls; output_schema may be absent. The reference can be reused in this conversation, including Code Mode. If it is stale or unavailable, query the tool again; do not automatically repeat a call whose effect is uncertain.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

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

export type CallMcpToolValue =
  | {
      /**
       * 远端原样返回的任意 JSON 值；缺失与 null 不同。按实际 output_schema 理解成功值，失败值不假定符合成功 schema。
       */
      structuredContent?: JsonValue;
      meta: {
        status: "SUCCESS" | "ERROR" | "CANCELLED";
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
  call_mcp_tool(args: CallMcpToolArgs): Promise<Successful<CallMcpToolValue>>;
};
```
