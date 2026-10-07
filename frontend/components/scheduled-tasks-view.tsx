import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { CalendarClock, ChevronDown, ChevronRight, CircleAlert, Clock3, ExternalLink, LoaderCircle, MessageSquare, Pause, Pencil, Play, Plus, RefreshCw, SlidersHorizontal, Trash2, X } from 'lucide-react';
import type { SessionSummary, PermissionMode } from '../lib/pulsara-types';
import type { ModelCallBindingPayload, ModelConfigurationSummary, ReasoningSelectionPayload } from '../lib/runtime-adapter';
import { LocalScheduledTasksApi, ScheduledApiError, scheduleLabel, type ScheduledTask, type ScheduledValues, type ScheduleRule, type ScheduledRunRequest } from '../lib/scheduled-tasks-api';

// Use the same portal/focus boundary as existing session management dialogs.
function ScheduledDialog({label,busy,onClose,children}: {label:string;busy:boolean;onClose:()=>void;children:ReactNode}) {
  const dialog=useRef<HTMLElement>(null);
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const app=document.querySelector<HTMLElement>('main.pulsara-shell');const wasInert=app?.inert??false;
    if(app)app.inert=true;
    dialog.current?.focus();
    return()=>{if(app)app.inert=wasInert;if(previous?.isConnected)previous.focus();};
  },[]);
  return createPortal(<div className="scheduled-overlay"><section ref={dialog} className="scheduled-dialog" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} onKeyDown={event=>{
    event.stopPropagation();
    if(event.key==='Escape'&&!event.nativeEvent.isComposing){event.preventDefault();if(!busy)onClose();}
    if(event.key!=='Tab')return;
    const controls=[...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')??[])];const first=controls[0],last=controls.at(-1);
    if(!first||!last){event.preventDefault();return;}
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog.current)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog.current)){event.preventDefault();first.focus();}
  }}>{children}</section></div>,document.body);
}

const statuses = { ACTIVE: '已启用', PAUSED: '已暂停', COMPLETED: '已启用' };
const statusFilters = { ENABLED: '已启用', PAUSED: '已暂停' };
const turns: Record<string, string> = { RUNNING: '正在执行', COMPLETED: '执行完成', INTERRUPTED: '已中断' };
const permissions: Record<PermissionMode, string> = { 'read-only': '只读', 'ask-permissions': '询问权限', 'accept-edits': '允许编辑', 'bypass-permissions': '允许全部操作' };
function dateTime(value: string | null, zone: string, withOffset = true) { return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', ...(withOffset ? {timeZoneName:'longOffset' as const} : {}) }).format(new Date(value)) : '—'; }
function localInput(value: string, zone: string) { const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(value)); const get = (name: string) => parts.find(p => p.type === name)?.value; return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`; }
interface Props { modelConfigurations: ModelConfigurationSummary[]; onCreateSession: (binding: ModelCallBindingPayload) => Promise<string>; api: LocalScheduledTasksApi; sessions: SessionSummary[]; ready: boolean; onOpenSession: (id: string, entryId?: string) => void; onOpenSettings: () => void }
export function ScheduledTasksView({ modelConfigurations, onCreateSession, api, sessions, ready, onOpenSession, onOpenSettings }: Props) {
  const [items, setItems] = useState<ScheduledTask[]>([]); const [status, setStatus] = useState(''); const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [editor, setEditor] = useState<{task?: ScheduledTask} | null>(null); const [confirmDelete, setConfirmDelete] = useState<ScheduledTask | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const revision = useRef(0); const runRequests = useRef(new Map<string, ScheduledRunRequest>());
  // Invalidate every outstanding list request, including manual reloads, on cleanup.
  const invalidateListRequests = useCallback(() => { revision.current++; }, []);
  const reload = async (more = false) => {
    const captured = ++revision.current; setLoading(true);
    try { const page = await api.list(status, more ? cursor ?? undefined : undefined); if (revision.current !== captured) return; setItems(old => more ? [...old.filter(x => !page.tasks.some(y => y.id === x.id)), ...page.tasks] : page.tasks); setCursor(page.next_cursor); }
    catch (e) { if (revision.current === captured) setError((e as Error).message); }
    finally { if (revision.current === captured) setLoading(false); }
  };
  useEffect(() => {
    const captured = ++revision.current;
    const controller = new AbortController();
    if (ready) void api.list(status, undefined, controller.signal).then(page => {
      if (revision.current !== captured) return;
      setItems(page.tasks); setCursor(page.next_cursor); setLoading(false);
    }).catch(e => { if (!controller.signal.aborted && revision.current === captured) { setError((e as Error).message); setLoading(false); } });
    return () => { controller.abort(); invalidateListRequests(); };
  }, [api, ready, status, invalidateListRequests]);
  const act = async (task: ScheduledTask, action: 'pause' | 'resume' | 'delete' | 'run') => {
    setBusy(true); setError('');
    try {
      if (action === 'run') { let request = runRequests.current.get(task.id); if (!request) { request = { task_id: task.id, session_id: task.session_id, expected_revision: task.revision, client_command_id: crypto.randomUUID(), request_at_utc: new Date(Math.floor(Date.now() / 1000) * 1000).toISOString() }; runRequests.current.set(task.id, request); } await api.runNow(request); runRequests.current.delete(task.id); }
      else await api.action(task, action);
      setConfirmDelete(null); await reload();
    } catch (e) { setError((e as Error).message); if (e instanceof ScheduledApiError && e.status >= 400 && e.status < 500) { runRequests.current.delete(task.id); if (e.status === 409) await reload(); } }
    finally { setBusy(false); }
  };
  return <section className="scheduled-view">
    <header className="page-header scheduled-page-header">
      <div><span className="page-kicker">会话计划</span><h1>定时任务</h1><p>让日常工作按时开始，结果留在会话中。</p></div>
      {ready && <div className="scheduled-page-actions">
        <button className="scheduled-primary" disabled={busy} onClick={() => setEditor({})}><Plus size={15} aria-hidden="true" />新建任务</button>
      </div>}
    </header>
    <div className="scheduled-inner">
      {!ready ? <div className="scheduled-collection scheduled-empty">
        <span className="scheduled-empty-icon"><CalendarClock size={25} aria-hidden="true" /></span><h2>连接数据库后即可管理任务</h2>
        <p>定时任务和会话会保存在本地数据库中。</p><button className="secondary-action" onClick={onOpenSettings}>打开设置</button>
      </div> : <div className="scheduled-collection">
        <div className="scheduled-toolbar">
          <div className="scheduled-collection-title"><CalendarClock size={17} aria-hidden="true" /><h2>{status ? statusFilters[status as keyof typeof statusFilters] : '全部任务'}</h2></div>
          <div className="scheduled-toolbar-actions">
            <label className="scheduled-filter memory-state-select"><SlidersHorizontal size={15} aria-hidden="true" /><select aria-label="任务状态" value={status} disabled={busy} onChange={e => setStatus(e.target.value)}><option value="">全部状态</option>{Object.entries(statusFilters).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select><ChevronDown size={13} aria-hidden="true" /></label>
            <button className="scheduled-icon-button" disabled={busy || loading} onClick={() => void reload()} aria-label="刷新定时任务">{loading ? <LoaderCircle size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}</button>
          </div>
        </div>
        {error && <div role="alert" className="scheduled-feedback"><CircleAlert size={16} aria-hidden="true" /><span>{error}</span></div>}
        {loading && !items.length ? <div className="scheduled-empty" role="status"><span className="scheduled-empty-icon"><LoaderCircle size={25} aria-hidden="true" /></span><p>正在读取任务…</p></div> : !items.length ? <div className="scheduled-empty">
          <span className="scheduled-empty-icon"><CalendarClock size={25} aria-hidden="true" /></span><h2>{status ? '没有符合条件的任务' : '还没有定时任务'}</h2><p>{status ? '可以切换状态，查看其他任务。' : '每天的检查、每周的整理，都可以交给一个计划。'}</p>
          {!status && <button className="secondary-action" onClick={() => setEditor({})}><Plus size={14} aria-hidden="true" />创建第一个任务</button>}
        </div> : <div className="scheduled-list">{items.map(task => {
          const open = expanded === task.id;
          const sessionTitle = sessions.find(s => s.id === task.session_id)?.title ?? '打开会话';
          const detailId = `scheduled-detail-${task.id}`;
          return <article key={task.id} className={`scheduled-row${open ? ' is-expanded' : ''}`}>
            <div className="scheduled-row-heading">
              <span className="scheduled-row-icon"><CalendarClock size={19} aria-hidden="true" /></span>
              <button className="scheduled-row-title" aria-expanded={open} aria-controls={detailId} onClick={() => setExpanded(open ? null : task.id)}>
                <h3>{task.name}</h3><span>{scheduleLabel(task)}</span>
              </button>
              <span className={`scheduled-status is-${task.status === 'PAUSED' ? 'paused' : 'active'}`}>{statuses[task.status]}</span>
              <button className="scheduled-icon-button scheduled-expand" aria-label={`${open ? '收起' : '展开'}${task.name}`} aria-expanded={open} aria-controls={detailId} onClick={() => setExpanded(open ? null : task.id)}><ChevronRight size={16} aria-hidden="true" /></button>
            </div>
            <div className="scheduled-row-body">
              <p className={`scheduled-prompt${open ? ' is-full' : ''}`}>{task.prompt}</p>
              <div className="scheduled-row-meta"><span><Clock3 size={13} aria-hidden="true" />{task.next_run_at ? `下一次：${dateTime(task.next_run_at, task.timezone)}` : task.status === 'COMPLETED' ? '一次性计划已派发' : '恢复后继续按计划运行'}</span>{task.pending_queue_item_id && <span className="scheduled-pending">等待运行</span>}</div>
              <dl id={detailId} hidden={!open} className="scheduled-details"><div><dt>时区</dt><dd>{task.timezone}</dd></div><div><dt>权限</dt><dd>{permissions[task.permission_mode]}</dd></div></dl>
              {task.prepare_error && <p className="scheduled-task-error"><CircleAlert size={14} aria-hidden="true" />{task.prepare_error}</p>}
              <footer className="scheduled-row-footer">
                <div className="scheduled-links"><button title={sessionTitle} onClick={() => onOpenSession(task.session_id)}><MessageSquare size={13} aria-hidden="true" /><span>{sessionTitle}</span></button>{task.last_turn_status && <button onClick={() => onOpenSession(task.session_id, task.last_entry_id ?? undefined)}><ExternalLink size={13} aria-hidden="true" /><span>{turns[task.last_turn_status] ?? task.last_turn_status} · 查看记录</span></button>}</div>
                <div className="scheduled-row-actions">
                  <button disabled={busy} onClick={() => setEditor({task})}><Pencil size={13} aria-hidden="true" />编辑</button>
                  <button disabled={busy} onClick={() => void act(task,'run')}><Play size={13} aria-hidden="true" />立即运行</button>
                  {task.status !== 'COMPLETED' && <button disabled={busy} onClick={() => void act(task,task.status === 'ACTIVE' ? 'pause' : 'resume')}>{task.status === 'ACTIVE' ? <Pause size={13} aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}{task.status === 'ACTIVE' ? '暂停' : '恢复'}</button>}
                  <button className="scheduled-danger" disabled={busy} onClick={() => setConfirmDelete(task)}><Trash2 size={13} aria-hidden="true" />取消任务</button>
                </div>
              </footer>
            </div>
          </article>;
        })}</div>}
        {cursor && <div className="scheduled-pagination"><button className="secondary-action" disabled={loading || busy} onClick={() => void reload(true)}>加载更多任务</button></div>}
      </div>}
    </div>
    {editor && <TaskEditor modelConfigurations={modelConfigurations} onCreateSession={onCreateSession} onOpenSettings={onOpenSettings} key={editor.task?.id ?? 'new'} task={editor.task} api={api} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); void reload(); }} />}
    {confirmDelete && <ScheduledDialog label="取消定时任务" busy={busy} onClose={()=>setConfirmDelete(null)}>
      <header><div><span className="page-kicker">取消计划</span><h2>取消「{confirmDelete.name}」？</h2></div><button className="scheduled-icon-button" disabled={busy} aria-label="关闭取消确认" onClick={() => setConfirmDelete(null)}><X size={18} aria-hidden="true" /></button></header>
      <div className="scheduled-confirm-body"><p>待运行输入会取消，会话和历史仍然保留。正在执行的回复会继续。</p></div>
      <footer className="scheduled-dialog-actions"><button className="secondary-action" disabled={busy} onClick={() => setConfirmDelete(null)}>保留任务</button><button className="scheduled-delete-action" disabled={busy} onClick={() => void act(confirmDelete,'delete')}>取消任务</button></footer>
    </ScheduledDialog>}
  </section>;
}
function TaskEditor({modelConfigurations, onCreateSession, onOpenSettings, task, api, onClose, onSaved}: {modelConfigurations: ModelConfigurationSummary[]; onCreateSession: (binding: ModelCallBindingPayload) => Promise<string>; onOpenSettings: () => void; task?: ScheduledTask; api: LocalScheduledTasksApi; onClose: () => void; onSaved: () => void}) {
  const [createdSessionId, setCreatedSessionId] = useState('');
  const [modelId, setModelId] = useState('');
  const [reasoning, setReasoning] = useState<ReasoningSelectionPayload | null>(null);
  const model = modelConfigurations.find(item => item.id === modelId);
  const reasoningOptions: Array<{label:string; value:ReasoningSelectionPayload | null}> = [{label:'模型默认',value:null}];
  if (model?.reasoning.kind === 'selectable') {
    for (const value of model.reasoning.effort?.values ?? []) reasoningOptions.push({label: value === null || value === 'none' ? '关闭' : ({low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'最高',minimal:'最低'}[value] ?? value), value:{kind:'effort',value}});
    if (model.reasoning.toggle) reasoningOptions.push({label:'开启',value:{kind:'toggle',enabled:true}},{label:'关闭',value:{kind:'toggle',enabled:false}});
  }
  const [current, setCurrent] = useState(task);
  const timezone = current?.timezone ?? 'Asia/Shanghai';
  const timezoneLabel = timezone === 'Asia/Shanghai' ? '' : timezone;
  const permission = current?.permission_mode ?? 'bypass-permissions';
  const [name,setName] = useState(task?.name ?? ''); const [prompt,setPrompt] = useState(task?.prompt ?? '');
  const [kind,setKind] = useState<ScheduleRule['kind']>(task?.schedule.kind ?? 'daily');
  const initial = task?.schedule; const [start,setStart] = useState(initial && 'start_date' in initial ? initial.start_date : new Intl.DateTimeFormat('sv-SE', {timeZone:timezone}).format(new Date()));
  const [time,setTime] = useState(initial && 'time' in initial ? initial.time : '08:00'); const [day,setDay] = useState(initial?.kind === 'monthly' ? initial.day : 1);
  const [days,setDays] = useState(initial?.kind === 'weekly' ? initial.weekdays : [1]); const [seconds,setSeconds] = useState(initial?.kind === 'interval' ? initial.seconds : 86400);
  const [once,setOnce] = useState(() => initial?.kind === 'once' ? localInput(initial.run_at_utc,task!.timezone) : localInput(new Date(Date.now()+3600000).toISOString(),timezone));
  const [onceChanged, setOnceChanged] = useState(false);
  const [anchor,setAnchor] = useState(() => initial?.kind === 'interval' ? initial.anchor_at_utc : new Date(Math.floor(Date.now()/1000)*1000).toISOString());
  const [preview,setPreview] = useState(''); const [error,setError] = useState(''); const [busy,setBusy] = useState(false); const previewRevision = useRef(0);
  const invalidatePreviewRequests = useCallback(() => { previewRevision.current++; }, []);
  const rule = async (signal?: AbortSignal): Promise<ScheduleRule> => {
    const contract = 'scheduled-rule:v1' as const;
    if (kind === 'once') return current?.schedule.kind === 'once' && !onceChanged ? current.schedule : (await api.localOnce(once, timezone, signal)).schedule;
    if (kind === 'interval') return { contract, kind, anchor_at_utc: anchor, seconds };
    if (kind === 'weekly') return { contract,kind,start_date:start,time,weekdays:days };
    if (kind === 'monthly') return { contract,kind,start_date:start,time,day };
    return { contract,kind,start_date:start,time };
  };
  useEffect(() => { const controller = new AbortController(); const captured = ++previewRevision.current;
    void rule(controller.signal).then(schedule => api.preview(schedule, timezone,controller.signal)).then(p => { if (captured === previewRevision.current) setPreview(dateTime(p.next_run_at,timezone,timezone !== 'Asia/Shanghai') + (p.local_time_fold === 0 ? " · 重复时间的第一次" : p.local_time_fold === 1 ? " · 重复时间的第二次" : "")); }).catch(e => { if (!controller.signal.aborted && captured === previewRevision.current) setPreview((e as Error).message); });
    return () => { controller.abort(); invalidatePreviewRequests(); };
  }, [kind, start,time,day,days,seconds,once,anchor,timezone,onceChanged,current,invalidatePreviewRequests]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = async () => {
    setBusy(true); setError('');
    try {
      if (!name.trim() || !prompt.trim()) throw new Error('请填写名称和提示词。');
      const values: ScheduledValues = { name:name.trim(),prompt:prompt.trim(),timezone,permission_mode:permission,schedule:await rule() };
      if (current) await api.update(current,values);
      else {
        if (!model || model.status !== 'ready') throw new Error('请先选择可用的供应商和模型。');
        // Validate the schedule before allocating an ordinary quick workspace.
        await api.preview(values.schedule, timezone);
        const sessionId = createdSessionId || await onCreateSession({connection_id:model.id,reasoning});
        setCreatedSessionId(sessionId);
        await api.create(sessionId,values);
      }
      onSaved();
    } catch(e) {
      setError((e as Error).message);
      if (e instanceof ScheduledApiError && e.status === 409 && current) {
        try {
          const latest = await api.get(current.id); setCurrent(latest); setName(latest.name); setPrompt(latest.prompt); setKind(latest.schedule.kind);
          const r = latest.schedule; if ('start_date' in r) setStart(r.start_date); if ('time' in r) setTime(r.time); if (r.kind === 'monthly') setDay(r.day); if (r.kind === 'weekly') setDays(r.weekdays); if (r.kind === 'once') { setOnce(localInput(r.run_at_utc,latest.timezone)); setOnceChanged(false); } if (r.kind === 'interval') { setSeconds(r.seconds); setAnchor(r.anchor_at_utc); }
        } catch(refreshError) {setError((refreshError as Error).message);}
      }
    } finally { setBusy(false); }
  };
  return <ScheduledDialog label={task ? '编辑定时任务' : '新建定时任务'} busy={busy} onClose={onClose}><header><div><span className="page-kicker">会话计划</span><h2>{task ? '编辑任务' : '新建任务'}</h2></div><button className="scheduled-icon-button" disabled={busy} onClick={onClose} aria-label="关闭任务表单"><X size={18} /></button></header><form onSubmit={e => { e.preventDefault(); void save(); }}><div className="scheduled-form-body">
    {!task && <>
      <label>供应商 / 模型<select required disabled={busy || Boolean(createdSessionId)} value={modelId} onChange={e=>{const next=modelConfigurations.find(item=>item.id===e.target.value);setModelId(e.target.value);setReasoning(next?.default_reasoning ?? null);}}><option value="">选择供应商和模型</option>{modelConfigurations.map(item=><option key={item.id} value={item.id} disabled={item.status!=='ready'}>{item.route_name ?? item.route_id} · {item.display_name ?? item.model_id}</option>)}</select></label>
      {model?.reasoning.kind === 'selectable' && <label>推理强度<select disabled={busy || Boolean(createdSessionId)} value={reasoning?.kind==='budget_tokens' ? 'budget' : JSON.stringify(reasoning)} onChange={e=>setReasoning(e.target.value==='budget' ? {kind:'budget_tokens',tokens:Math.ceil(((model.reasoning.budget_tokens?.minimum ?? 0)+(model.reasoning.budget_tokens?.maximum ?? 0))/2)} : JSON.parse(e.target.value) as ReasoningSelectionPayload | null)}>{reasoningOptions.map(item=><option key={JSON.stringify(item.value)} value={JSON.stringify(item.value)}>{item.label}</option>)}{model.reasoning.budget_tokens?.minimum!=null && model.reasoning.budget_tokens.maximum!=null && <option value="budget">自定义推理预算</option>}</select></label>}
      {reasoning?.kind==='budget_tokens' && <label>推理预算<input type="number" required step="1" disabled={busy || Boolean(createdSessionId)} min={model?.reasoning.budget_tokens?.minimum ?? undefined} max={model?.reasoning.budget_tokens?.maximum ?? undefined} value={reasoning.tokens} onChange={e=>setReasoning({kind:'budget_tokens',tokens:Number(e.target.value)})}/></label>}
      {!modelConfigurations.some(item=>item.status==='ready') && <p className="scheduled-muted">还没有可用的模型。<button type="button" className="scheduled-settings-link" onClick={()=>{onClose();onOpenSettings();}}>配置模型</button></p>}
    </>}
    <label>名称<input required disabled={busy} value={name} onChange={e => setName(e.target.value)} /></label><label>提示词<textarea required disabled={busy} rows={5} value={prompt} onChange={e => setPrompt(e.target.value)} /></label>
    <label>时间规则<select disabled={busy} value={kind} onChange={e => setKind(e.target.value as ScheduleRule['kind'])}><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="interval">固定间隔</option><option value="once">只运行一次</option></select></label>
    {kind === 'once' ? <label>运行时间{timezoneLabel && `（${timezoneLabel}）`}<input type="datetime-local" required disabled={busy} value={once} onChange={e => { setOnceChanged(true); setOnce(e.target.value); }} /></label> : kind === 'interval' ? <><label>间隔（秒）<input type="number" min="1" step="1" required disabled={busy} value={seconds} onChange={e => setSeconds(Number(e.target.value))} /></label><label>固定起点（UTC）<input required disabled={busy} value={anchor} onChange={e => setAnchor(e.target.value)} /></label></> : <><div className="scheduled-fields"><label>开始日期<input type="date" required disabled={busy} value={start} onChange={e => setStart(e.target.value)} /></label><label>时间{timezoneLabel && `（${timezoneLabel}）`}<input type="time" required disabled={busy} value={time} onChange={e => setTime(e.target.value)} /></label></div>{kind === 'monthly' && <label>每月几号<input type="number" min="1" max="31" step="1" required disabled={busy} value={day} onChange={e => setDay(Number(e.target.value))} /></label>}{kind === 'weekly' && <fieldset><legend>星期</legend><div className="scheduled-weekdays">{['一','二','三','四','五','六','日'].map((d,i) => <label key={d}><input type="checkbox" disabled={busy} checked={days.includes(i+1)} onChange={e => setDays(old => e.target.checked ? [...old,i+1].sort() : old.filter(x => x!==i+1))} />{d}</label>)}</div></fieldset>}</>}

    <p className="scheduled-preview"><Clock3 size={15} aria-hidden="true" /><span>下一次：{preview || '正在计算…'}{timezoneLabel && ` · ${timezoneLabel}`}</span></p>{createdSessionId && <p className="scheduled-muted">会话已创建，重试会继续保存到该会话。</p>}{task && <p className="scheduled-muted">已入队的输入保留原内容。需要取消时，请进入会话。</p>}{error && <p role="alert" className="scheduled-error">{error}</p>}</div><footer className="scheduled-dialog-actions"><button className="secondary-action" type="button" disabled={busy} onClick={onClose}>取消</button><button className="scheduled-primary" disabled={busy || (!task && (!model || model.status !== 'ready'))}>{busy ? '保存中…' : '保存任务'}</button></footer>
  </form></ScheduledDialog>;
}
