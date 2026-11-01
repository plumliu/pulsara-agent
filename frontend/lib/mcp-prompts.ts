export interface McpPromptTarget { kind: 'MCP_PROMPT'; server_id: string; name: string }
export interface McpPrompt {
  target: McpPromptTarget;
  name: string;
  description: string;
  server_name: string;
  arguments: { name: string; description: string; required: boolean }[];
}
export interface McpPromptPage { state: 'ready' | 'updating'; items: McpPrompt[]; next_offset: number | null }
export type McpPromptPreview = { state: 'ready'; text: string } | {
  state: 'invalid_arguments' | 'unavailable' | 'failed' | 'unsupported_content' | 'empty' | 'resource_boundary';
  message: string; fields?: string[];
};
export type ListMcpPrompts = (query: string, offset: number, signal: AbortSignal) => Promise<McpPromptPage>;
export type PreviewMcpPrompt = (target: McpPromptTarget, args: Record<string, string>, signal: AbortSignal) => Promise<McpPromptPreview>;
export function mcpPromptPath(prompt: McpPrompt) {
  return `/${encodeURIComponent(prompt.target.server_id)}/${encodeURIComponent(prompt.name)}`;
}
