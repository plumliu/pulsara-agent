import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConversationMessages } from '../../frontend/components/workbench-view';
import { DesktopWorkspaceProvider, useDesktopWorkspace } from '../../frontend/components/desktop-workspace';
import { agentCellApi } from '../../frontend/lib/agent-cell-api';
import { randomIdentity } from '../../frontend/lib/identity';
import { ToolResultDisplayContext } from '../../frontend/lib/tool-result-display';
import type { Message, ToolTrace } from '../../frontend/lib/pulsara-types';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function view(traces: ToolTrace[], showBuiltinToolResults: boolean) {
  const messages: Message[] = [{id:'live',role:'assistant',assistantKind:'live',turnId:'turn',time:'',body:'',status:'running',traces}];
  return <ToolResultDisplayContext.Provider value={{showBuiltinToolResults,onChange:()=>{}}}>
    <ConversationMessages messages={messages} isRunning activeTurnId="turn" outputOwnerKey="test"
      onReadToolOutput={vi.fn()} onNotify={vi.fn()}/>
  </ToolResultDisplayContext.Provider>;
}
const card = (toolName: string) => screen.getByRole('button', {name:`收起工具详情：${toolName}`}).closest('article')!;

describe('tool result disclosure preference', () => {
  it('unlocks typed internal results without changing or discarding them', () => {
    const result = {meta:{status:'SUCCESS' as const},content:[{type:'object' as const,value:{target:{kind:'AGENT',agent_id:'session:aaaaaaaaaaaaaaaaaaaaaaaaaa'},name:'worker',current_work:null,current_work_title:null,queued_work_count:0}}]};
    const trace: ToolTrace = {id:'agents',kind:'tool',toolName:'query_agents',title:'查看任务',subtitle:'已完成',status:'completed',businessValue:result};
    const display = render(view([trace],false));
    expect(screen.queryByRole('button',{name:'展开工具详情：query_agents'})).toBeNull();
    display.rerender(view([trace],true));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：query_agents'}));
    expect(card('query_agents').textContent).toContain('worker');
    display.rerender(view([trace],false));
    expect(screen.queryByRole('button',{name:/工具详情：query_agents/})).toBeNull();
    display.rerender(view([trace],true));
    const expand = screen.queryByRole('button',{name:'展开工具详情：query_agents'});
    if (expand) fireEvent.click(expand);
    expect(card('query_agents').textContent).toContain('worker');
    expect(result).toEqual({meta:{status:'SUCCESS'},content:[{type:'object',value:{target:{kind:'AGENT',agent_id:'session:aaaaaaaaaaaaaaaaaaaaaaaaaa'},name:'worker',current_work:null,current_work_title:null,queued_work_count:0}}]});
  });

  it('keeps already expandable terminal and MCP content identical across preference changes', () => {
    const traces: ToolTrace[] = [
      {id:'terminal',kind:'terminal',toolName:'exec_process',title:'运行命令',subtitle:'已完成',status:'completed',command:'printf hello',resultText:'raw envelope',businessValue:{meta:{status:'SUCCESS',process_status:'EXITED',exit_code:0},content:[{type:'text',value:'hello'}]}},
      {id:'mcp',kind:'mcp',toolName:'call_mcp_tool',title:'天气',subtitle:'已完成',status:'completed',businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'sunny'}],structuredContent:{high:23}}},
    ];
    const display=render(view(traces,false));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：exec_process'}));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：call_mcp_tool'}));
    expect(within(card('exec_process')).getByRole('region',{name:'命令输出'}).textContent).toContain('hello');
    expect(card('call_mcp_tool').textContent).toContain('sunny');
    const before = traces.map(trace=>card(trace.toolName!).textContent);
    display.rerender(view(traces,true));
    expect(traces.map(trace=>card(trace.toolName!).textContent)).toEqual(before);
    display.rerender(view(traces,false));
    expect(traces.map(trace=>card(trace.toolName!).textContent)).toEqual(before);
  });

  it('keeps an internal tool failure available with the preference off', () => {
    const failed: ToolTrace = {id:'failed',kind:'tool',toolName:'query_agents',title:'查看任务',subtitle:'失败',status:'failed',resultState:'ERROR',businessValue:{meta:{status:'ERROR'},content:[{type:'text',value:'任务状态暂时不可用，请稍后重试。'},{type:'object',value:{schema:'private schema details'}}]}};
    const display=render(view([failed],false));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：query_agents'}));
    expect(card('query_agents').textContent).toContain('任务状态暂时不可用，请稍后重试。');
    expect(card('query_agents').textContent).not.toContain('private schema details');
    display.rerender(view([failed],true));
    expect(card('query_agents').textContent).toContain('任务状态暂时不可用，请稍后重试。');
    expect(card('query_agents').textContent).not.toContain('private schema details');
  });
  it('opens a concrete MCP resource by default while retaining a connection listing for the preference', () => {
    const resource: ToolTrace = {id:'resource',kind:'mcp',toolName:'read_mcp_resource',title:'资源',subtitle:'已完成',status:'completed',argumentsJson:JSON.stringify({target:{kind:'MCP_RESOURCE',server_id:'docs',uri:'file:///guide'}}),businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'Guide text'}]}};
    const listing: ToolTrace = {id:'listing',kind:'mcp',toolName:'read_mcp_resource',title:'资源列表',subtitle:'已完成',status:'completed',argumentsJson:JSON.stringify({target:{kind:'MCP_CONNECTION',config_path:'/project/.pulsara/mcp.yaml',server_id:'docs'}}),businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'Resource listing'}]}};
    const display=render(view([resource,listing],false));
    expect(screen.getAllByRole('button',{name:'展开工具详情：read_mcp_resource'})).toHaveLength(1);
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：read_mcp_resource'}));
    expect(card('read_mcp_resource').textContent).toContain('Guide text');
    display.rerender(view([resource,listing],true));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：read_mcp_resource'}));
    expect(screen.getByText('Resource listing')).toBeTruthy();
  });

  it('keeps cell source navigation independent from a useful child result disclosure', async () => {
    const sessionId=randomIdentity('session'), attemptId=randomIdentity('attempt');
    const readSource=vi.spyOn(agentCellApi,'source').mockResolvedValue({source_exec_attempt_id:attemptId,source:{status:'AVAILABLE',text:'console.log(1)',offset_chars:0,returned_chars:14,total_chars:14,has_more:false,next_offset_chars:null},live_cell:null});
    let workspace: ReturnType<typeof useDesktopWorkspace>;
    function Probe() { workspace=useDesktopWorkspace(); return null; }
    const trace: ToolTrace={id:'cell',kind:'tool',toolName:'exec_cell',title:'运行脚本',subtitle:'已接纳',status:'completed',attemptId,
      businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'已接纳脚本执行。'}]},
      programCalls:[{id:'child',kind:'read',toolName:'read_file',title:'读取文件',subtitle:'完成',status:'completed',businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'child evidence'}]}}]};
    const onShow=vi.fn();
    render(<DesktopWorkspaceProvider sessionId={sessionId} onShow={onShow}><Probe/>{view([trace],false)}</DesktopWorkspaceProvider>);
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：exec_cell'}));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：read_file'}));
    expect(screen.getByText('child evidence')).toBeTruthy();
    expect(readSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button',{name:'查看运行脚本'}));
    await waitFor(()=>expect(onShow).toHaveBeenCalledOnce());
    const tab=workspace!.tabs.find(item=>item.id===workspace!.activeId);
    expect(tab?.kind).toBe('cell');
    if(tab?.kind==='cell') expect(tab.sourceExecAttemptId).toBe(attemptId);
    expect(readSource).toHaveBeenCalledOnce();
    expect(screen.getByText('child evidence')).toBeTruthy();
  });

  it('shows a cell failure together with a useful child result', () => {
    const trace: ToolTrace={id:'cell-failure',kind:'tool',toolName:'exec_cell',title:'运行脚本',subtitle:'失败',status:'failed',resultState:'ERROR',
      businessValue:{meta:{status:'ERROR'},content:[{type:'text',value:'脚本在后续步骤失败。'}]},
      programCalls:[{id:'completed-child',kind:'read',toolName:'read_file',title:'读取文件',subtitle:'完成',status:'completed',businessValue:{meta:{status:'SUCCESS'},content:[{type:'text',value:'先前读取成功。'}]}}]};
    const display=render(view([trace],false));
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：exec_cell'}));
    expect(screen.getByText('脚本在后续步骤失败。')).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'展开工具详情：read_file'}));
    expect(screen.getByText('先前读取成功。')).toBeTruthy();
    display.rerender(view([trace],true));
    expect(screen.getByText('脚本在后续步骤失败。')).toBeTruthy();
    expect(screen.getByText('先前读取成功。')).toBeTruthy();
  });

});
