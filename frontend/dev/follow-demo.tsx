import { StrictMode, useEffect, useMemo, useState, type ComponentProps } from 'react';
import { createRoot } from 'react-dom/client';
import { WorkbenchView } from '../components/workbench-view';
import { PromptDraftStore } from '../lib/prompt-draft';
import { promptContentTextProjection } from '../lib/prompt-content';
import { initializeUiPreferences } from '../lib/ui-preferences';
import type { Message } from '../lib/pulsara-types';
import type { QueuedPrompt } from '../lib/runtime-adapter';
import '../app/globals.css';
import '../app/styles/base.css';
import '../app/styles/shell.css';
import '../app/styles/workbench.css';
import '../app/styles/overlays.css';
import '../app/styles/responsive.css';
import '../app/styles/desktop.css';
import 'katex/dist/katex.min.css';
import 'yet-another-react-lightbox/styles.css';

// Isolated fixture: production components, no runtime connection or saved data.
type Props = ComponentProps<typeof WorkbenchView>;
const noop = () => {};
const model: Props['modelConfigurations'][number] = {
  id: 'demo', source: 'models_dev', route_id: 'demo', route_name: '本地演示数据', wire_api: 'openai_responses', model_id: 'demo', display_name: '本地演示数据', base_url: 'https://example.invalid/v1',
  status: 'ready', authentication: 'none', credential_configured: true, reasoning: {kind: 'provider_default'}, default_reasoning: null,
};
const paragraph = (index: number) => `第 ${index} 段思考：先检查连续阅读区间，再核对语义锚点和用户操作的归属。内容继续生成时，展开末尾思考应持续跟随；向上阅读后应保留正在看的位置。`;
const longThought = Array.from({length: 55}, (_, index) => paragraph(index + 1)).join('\n\n');
const oldMessages: Message[] = [1, 2, 3].flatMap(index => [
  {id:`user-${index}`,role:'user',time:'',body:`历史问题 ${index}：请检查长思考和工具详情。`},
  {id:`process-${index}`,role:'assistant',assistantKind:'tool-request',turnId:`old-${index}`,time:'',body:'检查历史输出。',
    reasoning:[{id:`thought-${index}`,kind:'full',body:longThought}],
    traces:[{id:`tool-${index}`,kind:'mcp',toolName:'mcp__demo__read',title:'读取演示文本',subtitle:'已完成',status:'completed',resultText:longThought}]},
  {id:`answer-${index}`,role:'assistant',assistantKind:'terminal',turnId:`old-${index}`,rootFinal:true,time:'',body:`历史回答 ${index}。\n\n${paragraph(index)}`},
]);
function delayedRead(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const abort = () => {clearTimeout(timer); reject(new DOMException('读取已取消', 'AbortError'));};
    const timer = setTimeout(() => {signal.removeEventListener('abort',abort);resolve();}, 500);
    signal.addEventListener('abort',abort,{once:true}); if(signal.aborted)abort();
  });
}
function Demo() {
  const [version,setVersion]=useState(1),[running,setRunning]=useState(true),[streaming,setStreaming]=useState(false);
  const [body,setBody]=useState(longThought),[oldBand,setOldBand]=useState(false),[failReads,setFailReads]=useState(false);
  const [queue,setQueue]=useState<QueuedPrompt[]>([]),[notice,setNotice]=useState('先展开末尾思考；启动流式输出后，试着向上阅读。');
  const store=useMemo(()=>new PromptDraftStore(),[]);
  const [readerStatus,setReaderStatus]=useState('');
  useEffect(()=>()=>store.destroy(),[store]);
  useEffect(()=>{
    if(!streaming||!running)return;
    const interval=setInterval(()=>setBody(previous=>`${previous}\n\n${paragraph(previous.split('\n\n').length+1)}`),250);
    return()=>clearInterval(interval);
  },[streaming,running]);
  const current:Message={id:'current-process',role:'assistant',assistantKind:'live',turnId:'current',time:'',status:running?'running':'completed',body:'',
    reasoning:[{id:'current-thought',kind:'full',active:running,body}],
    traces:[{id:'current-tool',kind:'mcp',toolName:'mcp__demo__read',title:'读取演示文本',subtitle:'已完成',status:'completed',resultText:longThought}]};
  const [sent,setSent]=useState<Message[]>([]);
  const messages=oldBand?oldMessages.slice(0,3):[...oldMessages,current,...(!running?[{id:'current-final',role:'assistant',assistantKind:'terminal',turnId:'current',rootFinal:true,time:'',body:'思考已完成。手动打开的过程应继续保持展开；主动阅读和文字选区也应受到保护。'} as Message]:[]),...sent];
  const read=async(signal:AbortSignal)=>{await delayedRead(signal);if(failReads)throw Error('演示读取失败，请关闭失败开关后重试。');};
  const reset=()=>{setVersion(value=>value+1);setBody(longThought);setRunning(true);setStreaming(false);setOldBand(false);setQueue([]);setSent([]);};
  const props:Props={
    workspace:{id:'demo-workspace',name:'跟随最新体验',path:'/local-fixture',kind:'project'},
    session:{id:`follow-demo-${version}`,title:'跟随最新 · 生产组件体验',subtitle:'隔离演示',status:running?'running':'completed',updatedAt:'刚刚',live:true,modelCallBinding:{connection_id:'demo',reasoning:null}},
    messages,history:{atLatest:!oldBand,hasOlder:false,loading:false,windows:[{id:oldBand?'old':'tail',hasOlder:false,hasNewer:oldBand,loading:false}]},
    activeTurnId:'current',activePlanMode:false,isRunning:running,inspectorOpen:false,
    queuedCount:queue.length,queuedPrompts:queue,localSubmissions:[],queueActions:[],
    onQueueAction:async(item,kind)=>{setQueue(previous=>previous.filter(value=>value.queueItemId!==item.queueItemId));if(kind==='edit')store.restoreIfEmpty(`follow-demo-${version}`,{parts:[{type:'text',text:promptContentTextProjection(item.content)}]});},
    onQueueActionHandled:noop,runtimeStatus:'online',canCreateSession:false,modelConfigurations:[model],modelCallBinding:{connection_id:'demo',reasoning:null},
    canControl:true,isObserver:false,permission:'read-only',skills:[],
    onFork:async()=>{},onReconnect:noop,onTakeControl:noop,onNewSession:reset,onToggleInspector:noop,onOpenModelSettings:noop,
    onModelCallBindingChange:async()=>{},onSend:async(content)=>{
      if(running)setQueue(previous=>[...previous,{sourceKind:'command',queueItemId:crypto.randomUUID(),commandId:crypto.randomUUID(),sequence:String(previous.length+1),status:'pending',deliveryMode:'new-turn',content:{parts:[{type:'text',text:promptContentTextProjection(content)}]},permission:'read-only'}]);
      else setSent(previous=>[...previous,{id:crypto.randomUUID(),role:'user',time:'',body:promptContentTextProjection(content)}]);
      return true;
    },onStop:()=>{setRunning(false);setStreaming(false);},onCompact:async()=>{},onRenameSession:noop,onReopenRuntime:reset,runtimeReopenBusy:false,
    onReadInteraction:async()=>{throw Error('此演示没有交互卡片');},onResolveInteraction:async()=>false,
    artifactOwnerKey:`demo-${version}`,onReadToolArtifact:async()=>{throw Error('此演示使用本地工具正文');},onReadPromptImage:async()=>new Uint8Array(),promptDraftStore:store,
    onNotify:(title,message)=>setNotice(`${title}：${message}`),onPermissionChange:noop,
    onReadLatestHistory:async signal=>{await read(signal);setOldBand(false);},
    onReadProcessEnd:async(_entry,signal)=>{await read(signal);},
    onLoadHistory:async()=>setOldBand(false),
    onRetainHistory:interest=>setReaderStatus(interest.followLatest?'允许跟随':'保护阅读'),
    todo:{id:'demo-todo',items:[{id:'one',label:'展开长思考',status:'completed'},{id:'two',label:'向上阅读并选择文字',status:'in-progress'},{id:'three',label:'回到最新并完成执行',status:'pending'}]},
  };
  return <><aside className="follow-demo-controls">
    <strong>本地体验 · {readerStatus}</strong>
    <button onClick={()=>setStreaming(value=>!value)} disabled={!running}>{streaming?'暂停输出':'启动流式输出'}</button>
    <button onClick={()=>{setRunning(false);setStreaming(false);}}>完成执行</button>
    <button onClick={()=>{setOldBand(true);setVersion(value=>value+1);}}>切到历史区间</button>
    <button onClick={()=>setQueue(previous=>previous.length?[]:[{sourceKind:'command',queueItemId:'demo-queue',commandId:'demo-command',sequence:'1',status:'pending',deliveryMode:'new-turn',content:{parts:[{type:'text',text:longThought}]},permission:'read-only'}])}>增删长队列</button>
    <label><input type="checkbox" checked={failReads} onChange={event=>setFailReads(event.target.checked)}/>读取失败</label>
    <button onClick={reset}>重置／切换会话</button>
    <span role="status">{notice}</span>
  </aside><WorkbenchView {...props}/></>;
}
initializeUiPreferences({});
const style=document.createElement('style');style.textContent=`#root{height:100dvh;display:grid;grid-template-rows:auto minmax(0,1fr)}.follow-demo-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 16px;background:var(--paper-solid);font:13px var(--sans);border-bottom:1px solid var(--line)}.follow-demo-controls button{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--paper-raised)}.follow-demo-controls span{flex-basis:100%;color:var(--muted)}`;document.head.append(style);
createRoot(document.getElementById('root')!).render(<StrictMode><Demo/></StrictMode>);
