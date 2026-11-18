# `tools.read_mcp_resource(args)`

List resources and URI templates with target:MCP_CONNECTION. Read one resource with target:MCP_RESOURCE. Use a returned resource Target, or fill a template and use its server_id and concrete uri in the Target. Read content in order.

```ts
export interface ReadMcpResourceArgs {
  target:
    | {
        config_path: string;
        kind: "MCP_CONNECTION";
        server_id: string;
      }
    | McpResourceTarget;
}
export interface McpResourceTarget {
  kind: "MCP_RESOURCE";
  server_id: string;
  uri: string;
}
export interface ReadMcpResourceSuccess {
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
    | {
        type: "object";
        value:
          | {
              target: McpResourceTarget;
              name: string;
              description?: string;
              mime_type?: string;
            }
          | {
              server_id: string;
              uri_template: string;
              name: string;
              description?: string;
              mime_type?: string;
            };
      }
  )[];
}
export type ReadMcpResourceFailure = Omit<ReadMcpResourceSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  read_mcp_resource(args: ReadMcpResourceArgs): Promise<ReadMcpResourceSuccess>;
};
```
