# `tools.call_mcp_tool(args)`

Call an MCP tool with the invocation.target returned by query_capabilities and
arguments matching its input_schema. Obtain the relevant parameter and available
return descriptions before composing a business Cell. References work across
native calls and Cells in the same session.

content contains ordered text/image items. Optional structuredContent preserves
the remote JSON value; output_schema, when supplied, describes its successful
value. On failure, read the message and actual err.result for completed effects
and whether the call was dispatched. Refresh an invalid reference through query;
check an uncertain dispatched outcome before deciding on a retry.

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
