# `tools.query_capabilities(args)`

Discover installed capabilities with a required kind: SKILL, HOOK, MCP or PLUGIN.

- List SKILL, HOOK or MCP with optional scope and PLUGIN parent; list PLUGIN with optional scope.
- Inspect one Target with kind and target only. Read Skill or Hook files for detailed instructions or configuration.
- Search MCP with query and optional scope/parent. Tool hits come first and include input_schema, available output_schema and invocation; service hits provide connection records.

An MCP_CONNECTION query lists tools; an MCP_TOOL query gives its full interface.
Use invocation.target for call_mcp_tool. Print the needed interface when preparing
a later business Cell; its reference is reusable in the same session.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type QueryCapabilitiesArgs =
  | {
      kind: "SKILL";
      parent?: {
        kind: "PLUGIN";
        plugin_path: string;
      };
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "SKILL";
      target: {
        kind: "SKILL";
        skill_path: string;
      };
    }
  | {
      kind: "HOOK";
      parent?: {
        kind: "PLUGIN";
        plugin_path: string;
      };
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "HOOK";
      target: {
        config_path: string;
        kind: "HOOK_SOURCE";
      };
    }
  | {
      kind: "MCP";
      parent?: {
        kind: "PLUGIN";
        plugin_path: string;
      };
      query?: string;
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "MCP";
      target:
        | {
            config_path: string;
            kind: "MCP_CONNECTION";
            server_id: string;
          }
        | {
            kind: "MCP_TOOL";
            remote_tool_name: string;
            server_id: string;
          };
    }
  | {
      kind: "PLUGIN";
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "PLUGIN";
      target: {
        kind: "PLUGIN";
        plugin_path: string;
      };
    };

export type QueryCapabilitiesValue =
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
            type: "target";
            value:
              | {
                  kind: "SKILL";
                  skill_path: string;
                }
              | {
                  kind: "HOOK_SOURCE";
                  config_path: string;
                }
              | {
                  kind: "MCP_CONNECTION";
                  config_path: string;
                  server_id: string;
                };
          }
        | {
            type: "object";
            value:
              | {
                  target: {
                    kind: "SKILL";
                    skill_path: string;
                  };
                  name?: string;
                  description?: string;
                  source:
                    | {
                        kind: "LOCAL";
                        scope: "USER" | "WORKSPACE";
                      }
                    | {
                        kind: "PLUGIN";
                        target: {
                          kind: "PLUGIN";
                          plugin_path: string;
                        };
                      }
                    | {
                        kind: "BUNDLED";
                      };
                  status: string;
                }
              | {
                  target: {
                    kind: "HOOK_SOURCE";
                    config_path: string;
                  };
                  status: string;
                  definitions: {
                    event: string;
                    matcher: string;
                    command: string;
                  }[];
                }
              | {
                  target: {
                    kind: "PLUGIN";
                    plugin_path: string;
                  };
                  name?: string;
                  description?: string;
                  status: string;
                  components: {
                    skills: number | null;
                    hooks: number | null;
                    mcp_servers: number | null;
                  };
                }
              | {
                  target: {
                    kind: "MCP_CONNECTION";
                    config_path: string;
                    server_id: string;
                  };
                  name: string;
                  status: string;
                  instructions?: string;
                  resource_count?: number;
                  resource_template_count?: number;
                }
              | {
                  target: {
                    kind: "MCP_TOOL";
                    server_id: string;
                    remote_tool_name: string;
                  };
                  description?: string;
                  status: string;
                }
              | {
                  target: {
                    kind: "MCP_TOOL";
                    server_id: string;
                    remote_tool_name: string;
                  };
                  /**
                   * 精确查询提供完整描述；关键词查询可能提供描述原文的连续摘录。
                   */
                  description?: string;
                  /**
                   * 远端完整参数 JSON Schema；按它构造 call_mcp_tool.arguments。
                   */
                  input_schema: {
                    [k: string]: JsonValue;
                  };
                  /**
                   * 仅在远端实际声明时提供，描述成功 structuredContent，不是整个 Pulsara 返回包。
                   */
                  output_schema?: {
                    [k: string]: JsonValue;
                  };
                  invocation: {
                    mode: "META";
                    target: {
                      kind: "MCP_TOOL_REF";
                      tool_ref: string;
                    };
                  };
                }
              | {
                  target: {
                    kind: "MCP_TOOL";
                    server_id: string;
                    remote_tool_name: string;
                  };
                  /**
                   * 精确查询提供完整描述；关键词查询可能提供描述原文的连续摘录。
                   */
                  description?: string;
                  /**
                   * 远端完整参数 JSON Schema；按它构造 call_mcp_tool.arguments。
                   */
                  input_schema?: {
                    [k: string]: JsonValue;
                  };
                  /**
                   * 仅在远端实际声明时提供，描述成功 structuredContent，不是整个 Pulsara 返回包。
                   */
                  output_schema?: {
                    [k: string]: JsonValue;
                  };
                  invocation: {
                    mode: "UNAVAILABLE";
                  };
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
  query_capabilities(args: QueryCapabilitiesArgs): Promise<Successful<QueryCapabilitiesValue>>;
};
```
