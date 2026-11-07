// Local visual fixture using production readers and workspace components only.
import { useEffect, useState } from 'react';
import { AgentList, CellGraph } from '../components/agent-cell-workspace';
import { AgentConversationPane } from '../components/agent-conversation-pane';
import { DesktopWorkspace, DesktopWorkspaceProvider, inspectorTab, useDesktopWorkspace, type WorkspaceTab } from '../components/desktop-workspace';
import { agentCellApi, agentLabel, type AgentRow, type WorkFact } from '../lib/agent-cell-api';
import type { AgentConversationReader, RuntimeProjection, QueuedPrompt } from '../lib/runtime-adapter';
import { randomIdentity } from '../lib/identity';

const root = randomIdentity('session');
const agents = [randomIdentity('session'), randomIdentity('session')];
const works = [randomIdentity('queue'), randomIdentity('queue'), randomIdentity('queue')];
const host = randomIdentity('host');
const cell = randomIdentity('cell');
const attempt = randomIdentity('attempt');
const entries = [randomIdentity('entry'), randomIdentity('entry'), randomIdentity('entry')];
const turns = [randomIdentity('turn'), randomIdentity('turn')];
let continued = false;
const titles = ['修复停止链路', '复审派发与等待接口', '补充接口回归测试'];
const facts = (): WorkFact[] => works.map((work_id, index) => ({agent_id:agents[0], work_id, work_title:titles[index], status:index === 0 || (continued && index === 1) ? 'completed' : index === (continued ? 2 : 1) ? 'running' : 'queued'}));
const row = (id: string): AgentRow => ({agent_id:id,title:null,host_session_id:null,
  current_work:id === agents[0] ? {work_id:works[continued ? 2 : 1],work_title:titles[continued ? 2 : 1],status:'running'} : null,
  queued_work_count:id === agents[0] && !continued ? 1 : 0,has_running_turn:true});
const queue = (): QueuedPrompt[] => continued ? [] : [{queueItemId:works[2],sequence:'3',status:'pending',sourceKind:'agent',inputOrigin:'AGENT_INSTRUCTION',workTitle:titles[2],deliveryMode:'new-turn',content:{parts:[{type:'text',text:'补充同名任务、排队不覆盖当前任务和历史定位的接口回归。'}]}}];
const projection = (): RuntimeProjection => ({messages:[
  {id:entries[0],turnId:turns[0],role:'user',inputOrigin:'AGENT_INSTRUCTION',inputSource:{identityKind:'queue',queueItemId:works[0],sourceKind:'agent',deliveryMode:'new-turn',workTitle:titles[0]},time:'',body:'检查停止链路，核对每次请求的目标归属和最终结果。'},
  {id:entries[1],turnId:turns[0],role:'assistant',assistantKind:'terminal',rootFinal:true,status:'completed',time:'',body:'已完成停止链路复核。\n\n请求始终保留原会话和目标；切换会话后，已收到的结果仍归属原任务。'},
  {id:entries[2],turnId:turns[1],role:'user',inputOrigin:'AGENT_INSTRUCTION',inputSource:{identityKind:'queue',queueItemId:works[1],sourceKind:'agent',deliveryMode:'new-turn',workTitle:titles[1]},time:'',body:'复核数组派发与等待接口，保留历史任务的标题与准确定位。'},
],canonicalInputs:[],queuedPrompts:[],queuedCount:queue().length,isRunning:false} as unknown as RuntimeProjection);
const unavailable = async () => { throw new Error('此演示没有该类附件。'); };
const reader = (): AgentConversationReader => ({view:'history',sessionId:agents[0],current:projection,snapshot:async()=>projection(),observe:async()=>projection(),subscribe:()=>()=>{},readHistory:async()=>projection(),hydrateEntries:async()=>projection(),readLatestHistory:async()=>projection(),readProcessEnd:async()=>projection(),locateEntry:async()=>projection(),locateAnnotationSource:async()=>projection(),retainHistory:()=>undefined,
  readQueuePage:async()=>({items:queue(),nextCursor:null}),readQueueStatus:async()=>null,readQueueItem:async id=>queue().find(item=>item.queueItemId===id)??null,
  readToolOutput:unavailable,readPromptImage:unavailable,readImageReference:unavailable,readVisualizationThumbnail:unavailable,readVisualizationContent:unavailable,close:async()=>{}});
const adapter={openAgentReader:async()=>reader(),createAgentContentReader:()=>reader()};
export function installAgentWorkDemo() {
  agentCellApi.agents=async()=>({agents:agents.map(row),next_cursor:null});
  agentCellApi.agent=async(_root,id)=>({agent:row(id)});
  agentCellApi.graph=async(_root,_host,_cell,section)=>({cell:{target:{kind:'CELL',cell_id:cell},state:'running',has_unread_output:false,pending_calls:1,source_exec_attempt_id:attempt},participation_coverage:'complete',section,items:section==='works'?facts():[],next_cursor:null});
  agentCellApi.work=async(_root,id)=>({work:facts().find(work=>work.work_id===id)!,inputs:id===works[2]?[{kind:'dependency',source_work_id:works[1],ordinal:0}]:[]});
}
function Workspace() {
  const workspace=useDesktopWorkspace()!;
  const {open,activate}=workspace;
  const [,redraw]=useState(false);
  useEffect(()=>{for(const id of agents) open(inspectorTab(root,'agent',agentLabel(row(id)),id)); activate(inspectorTab(root,'agent','',agents[0]).id);},[open,activate]);
  const renderTab=(tab:WorkspaceTab)=>{
    if(tab.kind==='cell-graph') return <CellGraph root={root} host={host} cell={cell} onAgent={id=>workspace.open(inspectorTab(root,'agent',agentLabel(row(id)),id))} onWork={work=>workspace.open({...inspectorTab(root,'agent',agentLabel(row(work.agent_id)),work.agent_id),workLocation:{workId:work.work_id,requestId:1}})} />;
    if(tab.kind!=='agent') return null;
    tab.view ??={top:0,left:0}; tab.view.agentReadingState ??={choices:new Map()};
    return <AgentConversationPane key={tab.id} rootSessionId={root} agentSessionId={tab.entityId} agentName={tab.title} metadata={tab.agentMetadata??{}} adapter={adapter} readingState={tab.view.agentReadingState} onNotify={()=>{}} workLocation={tab.workLocation} />;
  };
  return <><aside className="follow-demo-controls"><strong>子代理任务展示</strong><button onClick={()=>{continued=!continued;redraw(continued);}}>切换到下一项工作</button><button onClick={()=>workspace.open(inspectorTab(root,'cell-graph','工作流图',cell,host))}>查看工作流</button><span>本地演示使用生产组件。工作切换后，当前任务会在下一次刷新中更新。</span></aside><main className="agent-work-demo"><section aria-label="子代理列表"><AgentList root={root} onOpen={agent=>workspace.open(inspectorTab(root,'agent',agentLabel(agent),agent.agent_id))}/></section><DesktopWorkspace isOpen onClose={()=>{}} toolbar={null} renderTab={renderTab}/></main></>;
}
export function AgentWorkDemo() {
  return <DesktopWorkspaceProvider sessionId={root} onShow={()=>{}}><Workspace/></DesktopWorkspaceProvider>;
}
