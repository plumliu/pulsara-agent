import { describe, expect, it } from 'vitest';
import { builtinToolSummary } from '../../frontend/lib/builtin-tool-summary';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';

function trace(toolName: string, args: unknown, result: unknown): ToolTrace {
  return { id: 'query', kind: 'artifact', toolName, title: toolName, subtitle: '',
    status: 'completed', resultState: 'SUCCESS', argumentsJson: JSON.stringify(args),
    resultText: JSON.stringify(result) };
}

describe('query and cell tool consumers', () => {
  it('selects capability list and detail presentation from the request branch', () => {
    expect(builtinToolSummary(trace('query_capabilities', {}, { items: [] }))?.title).toBe('查看能力');
    expect(builtinToolSummary(trace('query_capabilities', {
      target: { kind: 'MCP_TOOL', server_id: 'local', tool_name: 'read' },
    }, {}))?.title).toBe('查看能力详情');
  });

  it('keeps process metadata queries separate from output observations', () => {
    expect(builtinToolSummary(trace('query_processes', {}, { processes: [{ status: 'running' }] }))?.subtitle)
      .toBe('本次列出 1 个命令进程');
    expect(builtinToolSummary(trace('query_processes', { target: { kind: 'PROCESS', process_id: 'process:aaaaaaaaaaaaaaaaaaaaaaaaaa' } }, {
      process: { status: 'running' },
    }))?.subtitle).toBe('命令仍在运行');
  });

  it('does not confuse completed cell observations with completed scripts', () => {
    const target = { kind: 'CELL', cell_id: 'cell:aaaaaaaaaaaaaaaaaaaaaaaaaa' };
    expect(builtinToolSummary(trace('exec_cell', { code: 'console.log("started");' }, {
      target, state: 'running', output: [], pending_calls: 1, has_more: false,
    }))?.subtitle).toBe('脚本仍在运行');
    const stopped = builtinToolSummary(trace('manage_cell', { action: 'stop', target }, {
      target, state: 'terminated', pending_calls: 1, has_more: true,
    }));
    expect(stopped).toEqual({ title: '停止脚本', subtitle: '脚本已停止 · 还有后续输出' });
    expect(builtinToolSummary(trace('query_cells', {}, { cells: [{ target, state: 'running' }] }))?.subtitle)
      .toBe('本次列出 1 个脚本');
  });
});
