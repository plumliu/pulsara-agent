import { StrictMode, useEffect, useMemo, useState, type ComponentProps } from 'react';
import { createRoot } from 'react-dom/client';
import { WorkbenchView } from '../components/workbench-view';
import { FileLinkContext } from '../components/file-link-context';
import { PromptDraftStore } from '../lib/prompt-draft';
import { randomIdentity } from '../lib/identity';
import { initializeUiPreferences } from '../lib/ui-preferences';
import { ToolResultDisplayContext } from '../lib/tool-result-display';
import type { ToolTrace } from '../lib/pulsara-types';
import type { ToolBusinessValue } from '../lib/tool-business-value';
import '../app/globals.css';
import '../app/styles/base.css';
import '../app/styles/shell.css';
import '../app/styles/workbench.css';
import '../app/styles/overlays.css';
import '../app/styles/responsive.css';
import '../app/styles/desktop.css';
import 'katex/dist/katex.min.css';
import 'yet-another-react-lightbox/styles.css';

// Isolated, in-memory examples rendered by the production workbench. No runtime or provider is contacted.
type Props = ComponentProps<typeof WorkbenchView>;
const noop = () => {};
const business = (content: ToolBusinessValue['content'], extra: Record<string, unknown> = {}): ToolBusinessValue => ({meta:{status:'SUCCESS'},content,...extra});
const trace = (id: string, toolName: string, kind: ToolTrace['kind'], extra: Partial<ToolTrace> = {}): ToolTrace => ({id:randomIdentity('view'),toolName,kind,title:toolName,subtitle:'已完成',status:'completed',...extra});
const longPath = '/Users/demo/projects/pulsara-agent/frontend/components/workbench/tool-result-display/very-long-example-component.tsx';
const imageRef=randomIdentity('image');
const processTarget={kind:'PROCESS',process_id:randomIdentity('process')};
const cellTarget={kind:'CELL',cell_id:randomIdentity('cell')};
const agentTarget={kind:'AGENT',agent_id:randomIdentity('session')};
const mcpTarget={kind:'MCP_TOOL_REF',tool_ref:randomIdentity('mcpref')};
const groups: Record<string, ToolTrace[]> = {
  '文件与命令': [
    trace('terminal','exec_process','terminal',{command:'npm test -- --run tool-results',businessValue:business([{type:'text',value:'✓ tool result display (8)\n✓ process output (4)\n\nTest Files  2 passed\nTests       12 passed'}],{target:processTarget,meta:{status:'SUCCESS',process_status:'EXITED',exit_code:0,cwd:'/project'}})}),
    trace('read','read_file','read',{argumentsJson:JSON.stringify({target:{kind:'PATH',path:longPath}}),businessValue:business([{type:'text',value:'export function ToolResult() {\n  return <section>Readable result</section>;\n}'}],{target:{kind:'PATH',path:longPath},meta:{status:'SUCCESS',total_lines:3}})}),
    trace('edit','edit_file','edit',{argumentsJson:JSON.stringify({target:{kind:'PATH',path:longPath}}),businessValue:business([{type:'text',value:'--- a/frontend/tool-result.tsx\n+++ b/frontend/tool-result.tsx\n@@ -1,3 +1,3 @@\n- return null;\n+ return <ToolResult value={value} />;'}],{target:{kind:'PATH',path:longPath}})}),
    trace('search','search_file_content','search',{argumentsJson:JSON.stringify({pattern:'ToolResult',target:{kind:'PATH',path:'/project/frontend'}}),businessValue:business([{type:'object',value:{target:{kind:'PATH',path:'frontend/components/workbench-view.tsx'},line:533,text:'const result = trace.resultText;'}}])}),
    trace('write','write_file','edit',{argumentsJson:JSON.stringify({target:{kind:'PATH',path:'/project/notes/result.md'}}),businessValue:business([{type:'text',value:'已创建文件。'}],{target:{kind:'PATH',path:'/project/notes/result.md'},meta:{status:'SUCCESS',bytes_written:256}})})
  ],
  '图片与媒体': [
    trace('image','view_image','read',{argumentsJson:JSON.stringify({target:{kind:'PATH',path:'/project/assets/architecture.png'}}),businessValue:business([{type:'image',value:{kind:'IMAGE_REF',image_ref:imageRef}}],{target:{kind:'PATH',path:'/project/assets/architecture.png'}})}),
    trace('video','read_video_frames','read',{businessValue:business([{type:'object',value:{frames:[{n:1,t:12.125},{n:2,t:24.5}]}},{type:'image',value:{kind:'IMAGE_REF',image_ref:imageRef}}],{target:{kind:'PATH',path:'/project/demo.mp4'}})}),
    trace('transcript','read_transcript','read',{businessValue:business([{type:'object',value:{from:0,to:3.2,text:'欢迎使用 Pulsara。'}},{type:'object',value:{from:3.2,to:7.1,text:'这里展示工具的执行结果。'}}],{target:{kind:'PATH',path:'/project/demo.mp4'},meta:{status:'SUCCESS',source:'subtitle'}})}),
  ],
  'MCP 与能力': [
    trace('mcp-text','call_mcp_tool','mcp',{argumentsJson:JSON.stringify({target:mcpTarget,arguments:{city:'Shanghai'}}),businessValue:business([{type:'text',value:'今天晴，气温 15–23°C。\n明天多云，有小雨。'}])}),
    trace('mcp-structured','call_mcp_tool','mcp',{argumentsJson:JSON.stringify({target:mcpTarget,arguments:{city:'Shanghai'}}),businessValue:business([],{structuredContent:{location:'Shanghai',forecast:'sunny',days:[{high:23,low:15},{high:21,low:16}]}})}),
    trace('mcp-resource','read_mcp_resource','mcp',{argumentsJson:JSON.stringify({target:{kind:'MCP_RESOURCE',server_id:'docs',uri:'file:///guide'}}),businessValue:business([{type:'text',value:'快速开始：配置连接，然后调用天气预报工具。'}])}),
    trace('mcp-list','read_mcp_resource','mcp',{argumentsJson:JSON.stringify({target:{kind:'MCP_CONNECTION',config_path:'/project/.pulsara/mcp.yaml',server_id:'docs'}}),businessValue:business([{type:'text',value:'没有其他资源。'}])}),
    trace('capabilities','query_capabilities','tool',{argumentsJson:JSON.stringify({kind:'MCP',query:'weather'}),businessValue:business([{type:'object',value:{target:{kind:'MCP_TOOL',server_id:'weather',remote_tool_name:'forecast'},description:'查询天气预报。',input_schema:{type:'object',properties:{city:{type:'string'}}},invocation:{mode:'META',target:mcpTarget}}}])}),
    trace('manage-capability','manage_capability','tool',{businessValue:business([{type:'text',value:'已更新 MCP 连接。'}])}),
    trace('reload','reload_capabilities','tool',{businessValue:business([{type:'text',value:'已刷新扩展能力。'}])}),
  ],
  '进程与任务': [
    trace('processes','query_processes','tool',{businessValue:business([{type:'object',value:{target:processTarget,process_status:'RUNNING',exit_code:null,command:'npm test',cwd:'/project',stdin_closed:false,duration_seconds:1}}])}),
    trace('manage-process','manage_process','terminal',{argumentsJson:JSON.stringify({action:'wait',target:processTarget}),businessValue:business([{type:'text',value:'building frontend...\n✓ 42 modules transformed'}],{target:processTarget,meta:{status:'SUCCESS',process_status:'RUNNING',exit_code:null,cwd:'/project'}})}),
    trace('agents','query_agents','tool',{businessValue:business([{type:'object',value:{target:agentTarget,current_work:null,current_work_title:null,queued_work_count:0,name:'audit'}}])}),
    trace('cells','query_cells','tool',{businessValue:business([{type:'object',value:{target:cellTarget,state:'completed',pending_calls:0}}])}),
    trace('manage-cell','manage_cell','tool',{argumentsJson:JSON.stringify({action:'wait',target:cellTarget}),businessValue:business([],{target:cellTarget,meta:{status:'SUCCESS',cell_status:'completed',pending_calls:0}})}),
    trace('exec-cell','exec_cell','tool',{attemptId:randomIdentity('attempt'),businessValue:business([{type:'text',value:'已接纳脚本执行。'}],{target:cellTarget}),programCalls:[trace('child-read','read_file','read',{businessValue:business([{type:'text',value:'脚本读取的文件内容。'}],{target:{kind:'PATH',path:'/project/data.txt'}})})]}),
  ],
  '状态与记忆': [
    trace('todo','todo','tool',{businessValue:business([{type:'text',value:'已更新工作清单。'}])}),
    trace('schedule','query_schedules','tool',{businessValue:business([{type:'text',value:'没有符合条件的定时计划。'}])}),
    trace('manage-schedule','manage_schedule','tool',{businessValue:business([{type:'text',value:'已更新定时计划。'}])}),
    trace('memory','query_memory','tool',{argumentsJson:JSON.stringify({query:'用户偏好'}),businessValue:business([{type:'text',value:'没有找到相关记忆。'}])}),
    trace('remember','remember','tool',{businessValue:business([{type:'text',value:'已保存记忆。'}])}),
    trace('failed','query_agents','tool',{status:'failed',resultState:'ERROR',businessValue:{meta:{status:'ERROR'},content:[{type:'text',value:'任务状态暂时不可用，请稍后重试。'}]}}),
  ],
};
const workspaceId=randomIdentity('workspace'), sessionIds=Object.fromEntries(Object.keys(groups).map(group=>[group,randomIdentity('session')]));
const messageIds=Object.fromEntries(Object.keys(groups).map(group=>[group,randomIdentity('view')]));
const turnId=randomIdentity('turn');
function Demo() {
  const [group,setGroup]=useState(Object.keys(groups)[0]);
  const [show,setShow]=useState(false);
  const [dark,setDark]=useState(false);
  useEffect(()=>{document.documentElement.dataset.theme=dark?'dark':'light';},[dark]);
  const store=useMemo(()=>new PromptDraftStore(),[]);
  useEffect(()=>()=>store.destroy(),[store]);
  const props: Props={
    workspace:{id:workspaceId,name:'工具气泡画廊',path:'/local-fixture',kind:'project'},
    session:{id:sessionIds[group],title:group,subtitle:'隔离演示',status:'running',updatedAt:'刚刚',live:true,modelCallBinding:{connection_id:'demo',reasoning:null}},
    messages:[{id:messageIds[group],role:'assistant',assistantKind:'live',turnId,time:'',status:'running',body:'',traces:groups[group]}],
    history:{atLatest:true,hasOlder:false,loading:false,windows:[{id:'tail',hasOlder:false,hasNewer:false,loading:false}]},
    activeTurnId:turnId,activePlanMode:false,isRunning:true,inspectorOpen:false,
    queuedCount:0,queuedPrompts:[],localSubmissions:[],queueActions:[],onQueueAction:async()=>{},onQueueActionHandled:noop,
    runtimeStatus:'online',modelConfigurations:[{id:'demo',source:'models_dev',route_id:'demo',route_name:'本地演示数据',wire_api:'openai_responses',model_id:'demo',display_name:'本地演示数据',base_url:'https://example.invalid/v1',status:'ready',authentication:'none',credential_configured:true,reasoning:{kind:'provider_default'},default_reasoning:null}],modelCallBinding:{connection_id:'demo',reasoning:null},
    canControl:true,isObserver:false,permission:'read-only',skills:[],onFork:async()=>{},onReconnect:noop,onTakeControl:noop,onToggleInspector:noop,onOpenModelSettings:noop,
    onModelCallBindingChange:async()=>{},onSend:async()=>true,onStop:noop,onCompact:async()=>{},onRenameSession:noop,onReopenRuntime:noop,onCloseSession:noop,runtimeReopenBusy:false,
    onReadInteraction:async()=>{throw Error('本地演示没有交互卡片');},onResolveInteraction:async()=>false,
    outputOwnerKey:'gallery',onReadToolOutput:async(resultEntryId,position)=>({resultEntryId,position,text:'完整保留的文件内容\nexport function ToolResult() {\n  return <section>Readable result</section>;\n}'}),
    onReadPromptImage:async()=>new Uint8Array(),promptDraftStore:store,onNotify:noop,onPermissionChange:noop,
    onReadLatestHistory:async()=>{},onReadProcessEnd:async()=>{},onLoadHistory:async()=>{},onRetainHistory:noop,
  };
  return <><aside className="gallery-controls"><strong>工具气泡 · 本地演示</strong>
    {Object.keys(groups).map(name=><button key={name} aria-pressed={group===name} onClick={()=>setGroup(name)}>{name}</button>)}
    <button onClick={()=>setDark(value=>!value)}>{dark?'浅色':'深色'}</button>
    <label><input type="checkbox" checked={show} onChange={event=>setShow(event.target.checked)}/>显示内置工具结果详情</label>
    <button onClick={()=>document.querySelectorAll<HTMLButtonElement>('.trace-card__summary[aria-expanded="false"]').forEach(button=>button.click())}>展开可展开项</button>
  </aside><FileLinkContext.Provider value={{imageOwnerKey:'gallery-images',open:noop,readImageReference:async()=>({bytes:new Uint8Array(await (await fetch('/assets/pulsara-icon.png')).arrayBuffer()),mediaType:'image/png',width:128,height:128})}}><ToolResultDisplayContext.Provider value={{showBuiltinToolResults:show,onChange:setShow}}><WorkbenchView {...props}/></ToolResultDisplayContext.Provider></FileLinkContext.Provider></>;
}
initializeUiPreferences({});
const style=document.createElement('style');style.textContent=`#root{height:100dvh;display:grid;grid-template-rows:auto minmax(0,1fr)}.gallery-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 16px;background:var(--paper-solid);font:13px var(--sans);border-bottom:1px solid var(--line)}.gallery-controls button{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--paper-raised)}.gallery-controls button[aria-pressed=true]{background:var(--line)}.gallery-controls label{display:flex;align-items:center;gap:5px}`;document.head.append(style);
const root=import.meta.hot?.data.reactRoot ?? createRoot(document.getElementById('root')!);
if(import.meta.hot)import.meta.hot.data.reactRoot=root;
root.render(<StrictMode><Demo/></StrictMode>);
