import { createContext } from 'react';
import { uiPreferences } from './ui-preferences';
import type { ToolTrace } from './pulsara-types';

// Desktop presentation preference, shared by the main chat and task views.
export const ToolResultDisplayContext = createContext<{
  showBuiltinToolResults: boolean;
  onChange: (show: boolean) => void;
}>({
  showBuiltinToolResults: false,
  onChange: () => {},
});

export function readSavedToolResultDisplay(): boolean {
  return uiPreferences.getItem('pulsara-show-builtin-tool-results') === 'true';
}

// Default disclosures are evidence the reader can inspect or act on. Catalogs,
// orchestration records and acknowledgements remain in the trace unchanged.
const evidenceTools = new Set([
  'read_file', 'write_file', 'edit_file', 'find_files', 'search_file_content',
  'exec_process', 'manage_process', 'view_image', 'read_video_frames',
  'read_transcript', 'open_in_pulsara', 'call_mcp_tool',
  'web_search', 'web_fetch',
]);

export function isUserFacingToolResult(trace: ToolTrace): boolean {
  if (trace.toolName === 'exec_cell' && trace.programCalls?.some(call =>
    isUserFacingToolResult(call) || Boolean(toolResultDiagnosticText(call)))) return true;
  if (evidenceTools.has(trace.toolName ?? '')) return true;
  if (trace.toolName !== 'read_mcp_resource') return false;
  try {
    return JSON.parse(trace.argumentsJson ?? '{}').target?.kind === 'MCP_RESOURCE';
  } catch { return false; }
}

export function toolResultDiagnosticText(trace: ToolTrace): string | undefined {
  if (trace.status !== 'failed' && trace.status !== 'cancelled') return undefined;
  const text = trace.businessValue
    ? trace.businessValue.content.filter(part => part.type === 'text').map(part => part.value).join('')
    : trace.resultText;
  return text?.trim() ? text : undefined;
}

/** The preference only unlocks disclosure; it never selects a richer body. */
export function toolResultCanExpand(trace: ToolTrace, showBuiltinToolResults: boolean): boolean {
  return showBuiltinToolResults || isUserFacingToolResult(trace) || Boolean(toolResultDiagnosticText(trace));
}
