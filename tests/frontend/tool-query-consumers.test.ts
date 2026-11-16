import { describe, expect, it } from 'vitest';
import { builtinToolSummary } from '../../frontend/lib/builtin-tool-summary';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';
import type { ToolBusinessValue } from '../../frontend/lib/tool-business-value';

function trace(toolName: string, args: unknown, result: ToolBusinessValue): ToolTrace {
  return { id: 'query', kind: 'tool', toolName, title: toolName, subtitle: '',
    status: 'completed', resultState: 'SUCCESS', argumentsJson: JSON.stringify(args),
    businessValue: result };
}

describe('query and cell tool consumers', () => {
  it('selects capability list and detail presentation from the request branch', () => {
    expect(builtinToolSummary(trace('query_capabilities', {}, {meta:{status:'SUCCESS'},content:[]}))?.title).toBe('查看能力');
    expect(builtinToolSummary(trace('query_capabilities', {
      target: { kind: 'MCP_TOOL', server_id: 'local', remote_tool_name: 'read' },
    }, {meta:{status:'SUCCESS'},content:[]}))?.title).toBe('查看能力详情');
  });

  it('keeps process metadata queries separate from output observations', () => {
    expect(builtinToolSummary(trace('query_processes', {}, {meta:{status:'SUCCESS'},content:[{type:'object',value:{target:{kind:'PROCESS',process_id:'process:aaaaaaaaaaaaaaaaaaaaaaaaaa'},process_status:'RUNNING',exit_code:null,command:'echo started',cwd:'/project',stdin_closed:false,duration_seconds:1}}]}))?.subtitle)
      .toBe('本次列出 1 个命令进程');
    expect(builtinToolSummary(trace('query_processes', { target: { kind: 'PROCESS', process_id: 'process:aaaaaaaaaaaaaaaaaaaaaaaaaa' } }, {
      meta:{status:'SUCCESS'},content:[{type:'object',value:{target:{kind:'PROCESS',process_id:'process:aaaaaaaaaaaaaaaaaaaaaaaaaa'},process_status:'RUNNING',exit_code:null,command:'echo started',cwd:'/project',stdin_closed:false,duration_seconds:1}}],
    }))?.subtitle).toBe('命令仍在运行');
  });

  it('does not confuse completed cell observations with completed scripts', () => {
    const target = { kind: 'CELL', cell_id: 'cell:aaaaaaaaaaaaaaaaaaaaaaaaaa' };
    expect(builtinToolSummary(trace('exec_cell', { code: 'console.log("started");' }, {
      target,meta:{status:'SUCCESS'},content:[{type:'text',value:'已接纳脚本执行。'}],
    }))?.subtitle).toBe('脚本已接纳');
    const stopped = builtinToolSummary(trace('manage_cell', { action: 'stop', target }, {
      target,meta:{status:'SUCCESS',cell_status:'terminated',pending_calls:1},content:[{type:'text',value:'已停止脚本执行。'}],
    }));
    expect(stopped).toEqual({ title: '停止脚本', subtitle: '脚本已停止' });
    expect(builtinToolSummary(trace('manage_cell', {action:'wait',target}, {
      target,meta:{status:'SUCCESS',cell_status:'running',pending_calls:1},content:[],
    }))?.subtitle).toBe('脚本仍在运行');
    expect(builtinToolSummary(trace('query_cells', {target}, {
      target,meta:{status:'SUCCESS',cell_status:'running',pending_calls:1},content:[{type:'text',value:'console.log("started");'}],
    }))?.subtitle).toBe('脚本仍在运行');
    expect(builtinToolSummary(trace('query_cells', {}, {meta:{status:'SUCCESS'},content:[{type:'object',value:{target,state:'running',pending_calls:1}}]}))?.subtitle)
      .toBe('本次列出 1 个脚本');
  });
});
