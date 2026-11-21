# `tools.query_capabilities(args)`

Inspect capability sources. kind is required (SKILL, HOOK, MCP, or PLUGIN). List SKILL, HOOK or MCP with kind, scope? and parent?. List PLUGIN with kind and scope?. Inspect with kind and target only. SKILL lists definitions by source, including disabled and unselected copies. Use each record's status to identify the currently selected definitions. MCP service records describe configured connections and their current connection or discovery status. MCP also accepts query to search tool names, descriptions and services. Use query separately from target. For callable MCP tools, pass invocation.target to call_mcp_tool with arguments matching input_schema. A service hit describes the connection itself. Read Skill or Hook files when details are needed. Subagents may query MCP only. Omit unused fields.

```ts
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type QueryCapabilitiesArgs =
  | {
      kind: "SKILL";
      parent?: PluginTarget;
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "SKILL";
      target: SkillTarget;
    }
  | {
      kind: "HOOK";
      parent?: PluginTarget;
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "HOOK";
      target: HookSourceTarget;
    }
  | {
      kind: "MCP";
      parent?: PluginTarget;
      query?: string;
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "MCP";
      target: McpConnectionTarget | McpToolTarget;
    }
  | {
      kind: "PLUGIN";
      scope?: "USER" | "WORKSPACE";
    }
  | {
      kind: "PLUGIN";
      target: PluginTarget;
    };

export interface PluginTarget {
  kind: "PLUGIN";
  plugin_path: string;
}
export interface SkillTarget {
  kind: "SKILL";
  skill_path: string;
}
export interface HookSourceTarget {
  config_path: string;
  kind: "HOOK_SOURCE";
}
export interface McpConnectionTarget {
  config_path: string;
  kind: "MCP_CONNECTION";
  server_id: string;
}
export interface McpToolTarget {
  kind: "MCP_TOOL";
  remote_tool_name: string;
  server_id: string;
}
export interface QueryCapabilitiesSuccess {
  meta: {
    status: "SUCCESS";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | {
        type: "target";
        value: SkillTarget | HookSourceTarget | McpConnectionTarget;
      }
    | {
        type: "object";
        value:
          | {
              target: SkillTarget;
              name?: string;
              description?: string;
              source:
                | {
                    kind: "LOCAL";
                    scope: "USER" | "WORKSPACE";
                  }
                | {
                    kind: "PLUGIN";
                    target: PluginTarget;
                  }
                | {
                    kind: "BUNDLED";
                  };
              /**
               * 该定义的启用或选择状态。当前已选择表示当前来源解析选中的定义。
               */
              status: string;
            }
          | {
              target: HookSourceTarget;
              status: string;
              definitions: {
                event: string;
                matcher: string;
                command: string;
              }[];
            }
          | {
              target: PluginTarget;
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
              target: McpConnectionTarget;
              name: string;
              /**
               * 该配置连接的启停、连接和目录发现状态。
               */
              status: string;
              instructions?: string;
              resource_count?: number;
              resource_template_count?: number;
            }
          | {
              target: McpToolTarget;
              description?: string;
              status: string;
            }
          | {
              target: McpToolTarget;
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
              target: McpToolTarget;
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
export type QueryCapabilitiesFailure = Omit<QueryCapabilitiesSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  query_capabilities(args: QueryCapabilitiesArgs): Promise<QueryCapabilitiesSuccess>;
};
```
