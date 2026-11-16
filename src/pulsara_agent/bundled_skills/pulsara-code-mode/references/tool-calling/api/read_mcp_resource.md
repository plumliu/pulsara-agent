# `tools.read_mcp_resource(args)`

Discover or read MCP resources. target:MCP_CONNECTION lists that service's resources and URI templates; no uri is accepted. target:MCP_RESOURCE reads its exact URI; supply values for template variables in the URI before reading. Resource listings are metadata, not resource content. Omit unused fields; use the returned content in its given order.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface ReadMcpResourceArgs {
  target:
    | {
        config_path: string;
        kind: "MCP_CONNECTION";
        server_id: string;
      }
    | {
        kind: "MCP_RESOURCE";
        server_id: string;
        uri: string;
      };
}

export type ReadMcpResourceValue =
  | {
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
        | {
            type: "object";
            value:
              | {
                  target: {
                    kind: "MCP_RESOURCE";
                    server_id: string;
                    uri: string;
                  };
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
  read_mcp_resource(args: ReadMcpResourceArgs): Promise<Successful<ReadMcpResourceValue>>;
};
```
