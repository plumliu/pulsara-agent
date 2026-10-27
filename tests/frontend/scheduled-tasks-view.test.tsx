import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ScheduledTasksView } from '../../frontend/components/scheduled-tasks-view';
import { LocalScheduledTasksApi, ScheduledApiError, type ScheduledTask } from '../../frontend/lib/scheduled-tasks-api';
import type { ModelConfigurationSummary } from '../../frontend/lib/runtime-adapter';
import type { SessionSummary } from '../../frontend/lib/pulsara-types';
afterEach(() => { cleanup(); vi.restoreAllMocks(); notify.mockClear(); });
const task: ScheduledTask = { id:'scheduled:a',session_id:'session:a',name:'每日报告',prompt:'检查进展',status:'ACTIVE',revision:1,next_run_at:'2026-10-06T00:00:00Z',timezone:'Asia/Shanghai',permission_mode:'ask-permissions',schedule:{contract:'scheduled-rule:v1',kind:'once',run_at_utc:'2026-10-06T00:00:13Z'} };
const model:ModelConfigurationSummary={id:'model:a',source:'models_dev',route_id:'test',route_name:'测试供应商',wire_api:'openai_responses',model_id:'test-model',status:'ready',base_url:'http://localhost',authentication:'none',credential_configured:false,reasoning:{kind:'selectable',effort:{values:['high','max']}},default_reasoning:{kind:'effort',value:'high'}};
function setup() {
 const api=new LocalScheduledTasksApi();
 vi.spyOn(api,'listSessions').mockResolvedValue({sessions:[{id:'session:a',title:'任务会话'}],next_cursor:null});
 vi.spyOn(api,'list').mockResolvedValue({tasks:[task],next_cursor:null});
 vi.spyOn(api,'preview').mockResolvedValue({next_run_at:task.next_run_at!,local_time_fold:null});
 vi.spyOn(api,'create').mockResolvedValue({...task,status:'PAUSED',next_run_at:null});
 vi.spyOn(api,'get').mockResolvedValue(task);
 vi.spyOn(api,'update').mockResolvedValue(task);
 vi.spyOn(api,'action').mockResolvedValue({task:null});
 vi.spyOn(api,'runNow').mockResolvedValue({queue_item_id:'queue:a',status:'PENDING',coalesced:false});
 return api;
}
const notify = vi.fn();
function show(api:LocalScheduledTasksApi,onOpenSession=vi.fn(),onCreateSession=vi.fn(async()=> 'session:new')) { return render(<ScheduledTasksView onNotify={notify} modelConfigurations={[model]} onCreateSession={onCreateSession} api={api} ready sessions={[{id:'session:a',title:'普通会话'} as SessionSummary]} onOpenSession={onOpenSession} onOpenSettings={vi.fn()} />); }
it('retains the complete run action after unknown ACK and task revision refresh',async()=>{
 const api=setup();vi.mocked(api.runNow).mockRejectedValueOnce(new Error('network lost'));
 show(api);fireEvent.click(await screen.findByRole('button',{name:'运行一次'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('提交结果尚未确认','network lost','warning'));const original=vi.mocked(api.runNow).mock.calls[0][0];
 vi.mocked(api.list).mockResolvedValue({tasks:[{...task,revision:2,prompt:'新提示词'}],next_cursor:null});
 fireEvent.click(screen.getByRole('button',{name:'刷新定时任务'}));await screen.findByText('新提示词');
 fireEvent.click(screen.getByRole('button',{name:'运行一次'}));await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(2));
 expect(vi.mocked(api.runNow).mock.calls[1][0]).toEqual(original);expect(original.expected_revision).toBe(1);
});
it('rejects definite conflicting run and uses a new identity after reload',async()=>{
 const api=setup();vi.mocked(api.runNow).mockRejectedValueOnce(new ScheduledApiError('任务已修改',409));
 vi.mocked(api.list).mockResolvedValueOnce({tasks:[task],next_cursor:null}).mockResolvedValue({tasks:[{...task,revision:2}],next_cursor:null});
 show(api);fireEvent.click(await screen.findByRole('button',{name:'运行一次'}));await waitFor(()=>expect(notify).toHaveBeenCalledWith('未能运行任务','任务已修改','warning'));await waitFor(()=>expect(api.list).toHaveBeenCalledTimes(2));
 fireEvent.click(screen.getByRole('button',{name:'运行一次'}));await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(2));
 const [a,b]=vi.mocked(api.runNow).mock.calls.map(c=>c[0]);expect(b.expected_revision).toBe(2);expect(b.client_command_id).not.toBe(a.client_command_id);
});
it('confirms an unknown submission through the toolbar after the once row disappears',async()=>{
 const api=setup();vi.mocked(api.runNow).mockRejectedValueOnce(new Error('network lost'));
 show(api);await screen.findByRole('button',{name:'运行一次'});
 vi.mocked(api.list).mockResolvedValue({tasks:[],next_cursor:null});
 fireEvent.click(screen.getByRole('button',{name:'运行一次'}));
 await waitFor(()=>expect(screen.queryByRole('heading',{name:task.name})).toBeNull());
 await waitFor(()=>expect(screen.getByRole('button',{name:'确认上次提交'}).hasAttribute('disabled')).toBe(false));
 expect(screen.queryByText('network lost')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'确认上次提交'}));
 await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(2));
 const [original,retry]=vi.mocked(api.runNow).mock.calls.map(call=>call[0]);
 expect(retry).toEqual(original);
 await waitFor(()=>expect(screen.queryByRole('button',{name:'确认上次提交'})).toBeNull());
});
it('editing preserves the saved timezone, permission and exact once UTC including seconds',async()=>{
 const api=setup();const local=vi.spyOn(api,'localOnce');show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 expect(screen.queryByRole('combobox',{name:'时区'})).toBeNull();
 expect(screen.queryByRole('combobox',{name:'权限'})).toBeNull();
 expect(screen.queryByRole('combobox',{name:'会话'})).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));await waitFor(()=>expect(api.update).toHaveBeenCalled());
 const saved=vi.mocked(api.update).mock.calls[0][1];expect(saved.schedule).toEqual(task.schedule);expect(saved.timezone).toBe(task.timezone);expect(saved.permission_mode).toBe(task.permission_mode);expect(local).not.toHaveBeenCalled();
});
it('reloads all editor fields on CAS conflict rather than resubmitting stale values',async()=>{
 const api=setup();vi.mocked(api.update).mockRejectedValueOnce(new ScheduledApiError('配置已变更',409));
 vi.mocked(api.get).mockResolvedValue({...task,revision:2,name:'另一个窗口的配置',schedule:{contract:'scheduled-rule:v1',kind:'monthly',start_date:'2026-10-01',time:'09:30',day:31}});
 show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 await waitFor(()=>expect((screen.getByRole('textbox',{name:'名称'}) as HTMLInputElement).value).toBe('另一个窗口的配置'));
 expect((screen.getByRole('combobox',{name:'时间规则'}) as HTMLSelectElement).value).toBe('monthly');
 expect((screen.getByRole('spinbutton',{name:'每月几号'}) as HTMLInputElement).value).toBe('31');
 await waitFor(()=>expect(screen.getByRole('button',{name:'保存任务'}).hasAttribute('disabled')).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 await waitFor(()=>expect(api.update).toHaveBeenCalledTimes(2));
 expect(vi.mocked(api.update).mock.calls[1][0].revision).toBe(2);
 expect(vi.mocked(api.update).mock.calls[1][1].name).toBe('另一个窗口的配置');
});
it('keeps the session link and expands only the prompt in the compact paused card',async()=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[{...task,status:'PAUSED',next_run_at:null,last_turn_status:'INTERRUPTED',last_entry_id:'entry:user'}],next_cursor:null});const open=vi.fn();show(api,open);
 fireEvent.click(await screen.findByRole('button',{name:'普通会话'}));expect(open).toHaveBeenCalledWith('session:a');
 expect(screen.queryByText(/查看记录|恢复后继续按计划运行/)).toBeNull();
 const toggle=screen.getByRole('button',{name:'展开每日报告'});
 fireEvent.click(toggle);
 const prompt=document.getElementById(toggle.getAttribute('aria-controls')!);
 expect(prompt?.textContent).toBe(task.prompt);expect(prompt?.classList.contains('is-full')).toBe(true);
 expect(screen.queryByText('时区')).toBeNull();expect(screen.queryByText('权限')).toBeNull();
 expect(prompt?.closest('article')?.querySelector('.scheduled-row-meta')).toBeNull();
});
it('removes an accepted once plan and shows submission feedback',async()=>{
 const api=setup();show(api);
 await screen.findByRole('heading',{name:'每日报告'});
 vi.mocked(api.list).mockResolvedValue({tasks:[],next_cursor:null});
 fireEvent.click(screen.getByRole('button',{name:'运行一次'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('已提交','可在会话中查看','success'));
 expect(screen.queryByText('已提交，可在会话中查看')).toBeNull();
 await waitFor(()=>expect(screen.queryByRole('heading',{name:'每日报告'})).toBeNull());
 fireEvent.change(screen.getByRole('combobox',{name:'任务状态'}),{target:{value:'ACTIVE'}});
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith('ACTIVE',undefined,expect.any(AbortSignal),undefined));
});
it('switches atomically when enabling another plan without a confirmation dialog',async()=>{
 const api=setup();const target={...task,id:'scheduled:b',name:'每周整理',status:'PAUSED' as const,next_run_at:null};
 vi.mocked(api.list).mockImplementation(async(status)=>({tasks:status==='ACTIVE'?[task]:[task,target],next_cursor:null}));
 vi.spyOn(api,'switch').mockResolvedValue({paused:{...task,status:'PAUSED'},active:{...target,status:'ACTIVE'}});
 show(api);fireEvent.click(await screen.findByRole('switch',{name:'启用每周整理'}));
 await waitFor(()=>expect(api.switch).toHaveBeenCalledWith(task,{kind:'existing',target:{kind:'SCHEDULE',task_id:target.id},expected_revision:target.revision}));
 expect(api.action).not.toHaveBeenCalled();expect(screen.queryByRole('dialog')).toBeNull();
});
it('does not replace a newer filter result with a late older page',async()=>{
 const api=setup();let finish!:(p:{tasks:ScheduledTask[];next_cursor:null})=>void;vi.mocked(api.list).mockReturnValueOnce(new Promise(resolve=>{finish=resolve;})).mockResolvedValue({tasks:[{...task,id:'scheduled:b',name:'暂停任务',status:'PAUSED'}],next_cursor:null});
 show(api);fireEvent.change(screen.getByRole('combobox',{name:'任务状态'}),{target:{value:'PAUSED'}});await screen.findByText('暂停任务');finish({tasks:[task],next_cursor:null});await new Promise(resolve=>setTimeout(resolve,0));expect(screen.queryByText('每日报告')).toBeNull();
});
it('new task creation is lazy and Escape restores focus',async()=>{
 const api=setup();show(api);const button=await screen.findByRole('button',{name:'新建任务'});button.focus();fireEvent.click(button);
 const dialog=await screen.findByRole('dialog',{name:'新建定时任务'});
 expect(screen.queryByRole('combobox',{name:'权限'})).toBeNull();
 expect(screen.queryByRole('combobox',{name:'时区'})).toBeNull();
 expect(screen.queryByRole('combobox',{name:'会话'})).toBeNull();
 expect(api.create).not.toHaveBeenCalled();
 fireEvent.keyDown(dialog,{key:'Escape'});expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(button);
});
it.each([
 {fold:0 as const,instant:'2026-11-01T05:30:00Z',label:'第一次',offset:'GMT-04:00'},
 {fold:1 as const,instant:'2026-11-01T06:30:00Z',label:'第二次',offset:'GMT-05:00'},
])('ambiguous local preview states the actual occurrence $label and UTC offset',async({fold,instant,label,offset})=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[{...task,timezone:'America/New_York',schedule:{contract:'scheduled-rule:v1',kind:'once',run_at_utc:instant}}],next_cursor:null});
 vi.mocked(api.preview).mockResolvedValue({next_run_at:instant,local_time_fold:fold});
 show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 const preview=await screen.findByText(new RegExp('重复时间的'+label));expect(preview.textContent).toContain(offset);
});

it('creates a new quick session with chosen model/reasoning and Shanghai/bypass defaults',async()=>{
 const api=setup();const create=vi.fn(async()=> 'session:new');show(api,vi.fn(),create);
 fireEvent.click(await screen.findByRole('button',{name:'新建任务'}));
 expect((screen.getByRole('button',{name:'保存任务'}) as HTMLButtonElement).disabled).toBe(true);
 expect(create).not.toHaveBeenCalled();
 fireEvent.change(screen.getByRole('combobox',{name:'供应商 / 模型'}),{target:{value:model.id}});
 fireEvent.change(screen.getByRole('combobox',{name:'推理强度'}),{target:{value:JSON.stringify({kind:'effort',value:'max'})}});
 fireEvent.change(screen.getByRole('textbox',{name:'名称'}),{target:{value:'每日检查'}});
 fireEvent.change(screen.getByRole('textbox',{name:'提示词'}),{target:{value:'检查进展'}});
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 await waitFor(()=>expect(api.create).toHaveBeenCalled());
 expect(create).toHaveBeenCalledExactlyOnceWith({connection_id:model.id,reasoning:{kind:'effort',value:'max'}});
 expect(vi.mocked(api.create).mock.calls[0]).toEqual(['session:new',expect.objectContaining({name:'每日检查',prompt:'检查进展',timezone:'Asia/Shanghai',permission_mode:'bypass-permissions'})]);
});
it('retries a rejected task save in the already-created session',async()=>{
 const api=setup();vi.mocked(api.create).mockRejectedValueOnce(new ScheduledApiError('保存失败',400));const create=vi.fn(async()=> 'session:new');show(api,vi.fn(),create);
 fireEvent.click(await screen.findByRole('button',{name:'新建任务'}));
 fireEvent.change(screen.getByRole('combobox',{name:'供应商 / 模型'}),{target:{value:model.id}});
 fireEvent.change(screen.getByRole('textbox',{name:'名称'}),{target:{value:'每日检查'}});
 fireEvent.change(screen.getByRole('textbox',{name:'提示词'}),{target:{value:'检查进展'}});
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));await waitFor(()=>expect(notify).toHaveBeenCalledWith('任务未保存','保存失败','warning'));
 expect(screen.queryByRole('alert')).toBeNull();
 expect((screen.getByRole('combobox',{name:'供应商 / 模型'}) as HTMLSelectElement).disabled).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));await waitFor(()=>expect(api.create).toHaveBeenCalledTimes(2));
 expect(create).toHaveBeenCalledTimes(1);expect(vi.mocked(api.create).mock.calls.map(c=>c[0])).toEqual(['session:new','session:new']);
});


it('locates and flashes an exact task beyond the first page, then deduplicates ordinary pagination', async () => {
 const api=setup();const target={...task,id:'scheduled:z',name:'定位任务'};
 vi.mocked(api.list).mockResolvedValueOnce({tasks:[task],next_cursor:task.id}).mockResolvedValue({tasks:[target],next_cursor:null});
 vi.mocked(api.get).mockResolvedValue(target);
 const scroll=vi.fn();
 Object.defineProperty(HTMLElement.prototype,'scrollIntoView',{configurable:true,value:scroll});
 render(<ScheduledTasksView onNotify={notify} focusTaskId={target.id} modelConfigurations={[model]} onCreateSession={vi.fn()} api={api} ready sessions={[]} onOpenSession={vi.fn()} onOpenSettings={vi.fn()} />);
 const row=(await screen.findByRole('heading',{name:target.name})).closest('article')!;
 expect(api.get).toHaveBeenCalledWith(target.id,expect.any(AbortSignal));
 await waitFor(()=>expect(document.activeElement).toBe(row));
 expect(scroll).toHaveBeenCalledWith({block:'center'});
 expect(row.classList.contains('is-highlighted')).toBe(true);
 // React selects the WebKit event in jsdom, which has no AnimationEvent.
 const animationEnd='AnimationEvent' in window?'animationend':'webkitAnimationEnd';
 const entrance=new Event(animationEnd,{bubbles:true});Object.defineProperty(entrance,'animationName',{value:'scheduled-content-in'});fireEvent(row,entrance);
 expect(row.classList.contains('is-highlighted')).toBe(true);
 const highlight=new Event(animationEnd,{bubbles:true});Object.defineProperty(highlight,'animationName',{value:'home-setting-highlight'});fireEvent(row,highlight);
 expect(row.classList.contains('is-highlighted')).toBe(false);
 fireEvent.click(screen.getByRole('button',{name:'加载更多任务'}));
 await waitFor(()=>expect(screen.queryByRole('button',{name:'加载更多任务'})).toBeNull());
 expect(screen.getAllByRole('heading',{name:target.name})).toHaveLength(1);
 Reflect.deleteProperty(HTMLElement.prototype,'scrollIntoView');
});

it('reports a removed direct target while keeping the ordinary task list usable', async () => {
 const api=setup();vi.mocked(api.get).mockRejectedValue(new ScheduledApiError('任务不存在',404));
 render(<ScheduledTasksView onNotify={notify} focusTaskId="scheduled:deleted" modelConfigurations={[model]} onCreateSession={vi.fn()} api={api} ready sessions={[]} onOpenSession={vi.fn()} onOpenSettings={vi.fn()} />);
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('无法定位定时任务','任务不存在','warning'));
 expect(screen.queryByRole('alert')).toBeNull();
 expect(await screen.findByRole('heading',{name:task.name})).toBeTruthy();
});

it('keeps all task sessions available while combining session and status filters',async()=>{
 const api=setup();show(api);
 const filters=screen.getByRole('navigation',{name:'按会话筛选'});
 fireEvent.click(await within(filters).findByRole('button',{name:'任务会话'}));
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith('',undefined,expect.any(AbortSignal),'session:a'));
 fireEvent.change(screen.getByRole('combobox',{name:'任务状态'}),{target:{value:'PAUSED'}});
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith('PAUSED',undefined,expect.any(AbortSignal),'session:a'));
 expect(within(filters).getByRole('button',{name:'任务会话'}).getAttribute('aria-pressed')).toBe('true');
 fireEvent.change(screen.getByRole('searchbox',{name:'搜索任务会话'}),{target:{value:'另一会话'}});
 await waitFor(()=>expect(api.listSessions).toHaveBeenLastCalledWith('另一会话',undefined,expect.any(AbortSignal)));
 fireEvent.click(within(filters).getByRole('button',{name:'全部会话'}));
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith('PAUSED',undefined,expect.any(AbortSignal),undefined));
});
it('enables a paused task directly if its session has no active plan',async()=>{
 const api=setup();const paused={...task,status:'PAUSED' as const,next_run_at:null};
 vi.mocked(api.list).mockImplementation(async(status)=>({tasks:status==='ACTIVE'?[]:[paused],next_cursor:null}));
 const atomic=vi.spyOn(api,'switch');show(api);
 fireEvent.click(await screen.findByRole('switch',{name:'启用每日报告'}));
 await waitFor(()=>expect(api.action).toHaveBeenCalledWith(paused,'resume'));
 expect(atomic).not.toHaveBeenCalled();
});
it('keeps the previous active plan visible after an atomic switch conflict',async()=>{
 const api=setup();const paused={...task,id:'scheduled:b',name:'另一个计划',status:'PAUSED' as const,next_run_at:null};
 vi.mocked(api.list).mockImplementation(async(status)=>({tasks:status==='ACTIVE'?[task]:[task,paused],next_cursor:null}));
 vi.spyOn(api,'switch').mockRejectedValue(new ScheduledApiError('计划已变更',409));show(api);
 fireEvent.click(await screen.findByRole('switch',{name:'启用另一个计划'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('未能启用任务','计划已变更','warning'));
 expect(screen.queryByRole('alert')).toBeNull();
 await waitFor(()=>expect(screen.getByRole('switch',{name:'启用另一个计划'}).hasAttribute('disabled')).toBe(false));
 expect(screen.getByRole('switch',{name:'启用每日报告'}).getAttribute('aria-checked')).toBe('true');
 expect(screen.getByRole('switch',{name:'启用另一个计划'}).getAttribute('aria-checked')).toBe('false');
 expect(api.action).not.toHaveBeenCalled();
});

it.each([
 {pending_queue_item_id:'queue:a',last_turn_status:null,label:'已排队'},
 {pending_queue_item_id:null,last_turn_status:'RUNNING',label:'运行中'},
])('blocks another run while $label, then allows a new run after settlement',async(state)=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[{...task,...state}],next_cursor:null});
 show(api);
 const button=await screen.findByRole('button',{name:state.label});
 expect(button.hasAttribute('disabled')).toBe(true);
 fireEvent.click(button);expect(api.runNow).not.toHaveBeenCalled();
 vi.mocked(api.get).mockResolvedValue({...task,pending_queue_item_id:null,last_turn_status:'COMPLETED'});
 fireEvent.focus(window);
 const run=await screen.findByRole('button',{name:'运行一次'});
 expect(run.hasAttribute('disabled')).toBe(false);
 fireEvent.click(run);await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(1));
});

it('prevents rapid repeated submission while the request is unresolved',async()=>{
 const api=setup();let resolve!: (value:{queue_item_id:string;status:string;coalesced:boolean})=>void;
 vi.mocked(api.runNow).mockImplementation(()=>new Promise(done=>{resolve=done;}));
 show(api);const run=await screen.findByRole('button',{name:'运行一次'});
 fireEvent.click(run);fireEvent.click(run);fireEvent.click(run);
 expect(api.runNow).toHaveBeenCalledTimes(1);
 vi.mocked(api.list).mockResolvedValue({tasks:[{...task,last_turn_status:'RUNNING'}],next_cursor:null});
 resolve({queue_item_id:'queue:a',status:'RUNNING',coalesced:false});
 expect((await screen.findByRole('button',{name:'运行中'})).hasAttribute('disabled')).toBe(true);
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('任务正在运行','可在会话中查看','success'));
 await waitFor(()=>expect(screen.queryByRole('status')).toBeNull());
});

it('uses the existing refresh action after a list failure without claiming the collection is empty',async()=>{
 const api=setup();vi.mocked(api.list).mockRejectedValueOnce(new Error('读取失败'));
 show(api);
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('无法读取定时任务','读取失败','warning'));
 expect(screen.queryByRole('alert')).toBeNull();expect(screen.queryByText('读取失败')).toBeNull();
 expect(screen.queryByText('还没有定时任务')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'刷新定时任务'}));
 expect(await screen.findByRole('heading',{name:task.name})).toBeTruthy();
});

it('keeps loaded tasks and pagination available after a failed next page',async()=>{
 const api=setup();vi.mocked(api.list).mockResolvedValueOnce({tasks:[task],next_cursor:'next'}).mockRejectedValueOnce(new Error('下一页失败')).mockResolvedValue({tasks:[],next_cursor:null});
 show(api);fireEvent.click(await screen.findByRole('button',{name:'加载更多任务'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('无法加载更多任务','下一页失败','warning'));
 expect(screen.getByRole('heading',{name:task.name})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'加载更多任务'}));
 await waitFor(()=>expect(screen.queryByRole('button',{name:'加载更多任务'})).toBeNull());
 expect(screen.getByRole('heading',{name:task.name})).toBeTruthy();
});

it('toasts a session-filter read failure and preserves its existing retry action',async()=>{
 const api=setup();vi.mocked(api.listSessions).mockRejectedValueOnce(new Error('会话读取失败'));
 show(api);
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('无法读取任务会话','会话读取失败','warning'));
 expect(screen.queryByText('会话读取失败')).toBeNull();expect(screen.queryByText('暂无会话')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'重试'}));
 expect(await screen.findByRole('button',{name:'任务会话'})).toBeTruthy();
 expect(screen.queryByRole('button',{name:'重试'})).toBeNull();
});

it('ignores a stale session search failure after a newer query succeeds',async()=>{
 const api=setup();let reject!:(error:Error)=>void;
 vi.mocked(api.listSessions).mockReturnValueOnce(new Promise((_,fail)=>{reject=fail;}));
 show(api);await waitFor(()=>expect(api.listSessions).toHaveBeenCalledTimes(1));
 fireEvent.change(screen.getByRole('searchbox',{name:'搜索任务会话'}),{target:{value:'新查询'}});
 await screen.findByRole('button',{name:'任务会话'});
 await act(async()=>{reject(new Error('过期会话错误'));});
 await waitFor(()=>expect(api.listSessions).toHaveBeenCalledTimes(2));
 expect(notify).not.toHaveBeenCalled();
});

it('toasts preview errors once until recovery and never displays them as a next-run time',async()=>{
 const api=setup();vi.mocked(api.preview).mockRejectedValue(new Error('规则不可用'));
 show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('无法预览下次运行时间','规则不可用','warning'));
 expect(document.querySelector('.scheduled-preview')).toBeNull();expect(screen.queryByText('规则不可用')).toBeNull();
 fireEvent.change(screen.getByRole('combobox',{name:'时间规则'}),{target:{value:'monthly'}});
 await waitFor(()=>expect(api.preview).toHaveBeenCalledTimes(2));
 expect(notify).toHaveBeenCalledTimes(1);
 vi.mocked(api.preview).mockResolvedValueOnce({next_run_at:task.next_run_at!,local_time_fold:null});
 fireEvent.change(screen.getByRole('spinbutton',{name:'每月几号'}),{target:{value:'2'}});
 await waitFor(()=>expect(document.querySelector('.scheduled-preview')?.textContent).toContain('2026'));
 fireEvent.change(screen.getByRole('spinbutton',{name:'每月几号'}),{target:{value:'3'}});
 await waitFor(()=>expect(notify).toHaveBeenCalledTimes(2));
});

it('ignores stale preview errors after schedule edits and after the editor closes',async()=>{
 const api=setup();let reject!:(error:Error)=>void;
 vi.mocked(api.preview).mockReturnValueOnce(new Promise((_,fail)=>{reject=fail;}));
 show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 await waitFor(()=>expect(api.preview).toHaveBeenCalledTimes(1));
 fireEvent.change(screen.getByRole('combobox',{name:'时间规则'}),{target:{value:'monthly'}});
 await waitFor(()=>expect(document.querySelector('.scheduled-preview')?.textContent).toContain('2026'));
 await act(async()=>{reject(new Error('过期预览错误'));});
 vi.mocked(api.preview).mockReturnValueOnce(new Promise((_,fail)=>{reject=fail;}));
 fireEvent.change(screen.getByRole('spinbutton',{name:'每月几号'}),{target:{value:'2'}});
 await waitFor(()=>expect(api.preview).toHaveBeenCalledTimes(3));
 fireEvent.click(screen.getByRole('button',{name:'取消'}));
 await act(async()=>{reject(new Error('表单已关闭的错误'));});
 expect(screen.queryByRole('dialog')).toBeNull();
 expect(notify).not.toHaveBeenCalled();
});

it('preserves an edited draft after a save failure and toasts its successful retry',async()=>{
 const api=setup();vi.mocked(api.update).mockRejectedValueOnce(new Error('保存失败'));
 show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 fireEvent.change(screen.getByRole('textbox',{name:'名称'}),{target:{value:'保留草稿'}});
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('任务未保存','保存失败','warning'));
 expect((screen.getByRole('textbox',{name:'名称'}) as HTMLInputElement).value).toBe('保留草稿');
 expect(screen.queryByRole('alert')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 await waitFor(()=>expect(notify).toHaveBeenCalledWith('任务已保存',undefined,'success'));
 expect(vi.mocked(api.update).mock.calls[1][1].name).toBe('保留草稿');
 await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
});

it('routes native form validation through toast and focuses the invalid field',async()=>{
 const api=setup();show(api);fireEvent.click(await screen.findByRole('button',{name:'编辑'}));
 const name=screen.getByRole('textbox',{name:'名称'});
 fireEvent.change(name,{target:{value:''}});
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));
 expect(notify).toHaveBeenCalledExactlyOnceWith('请检查任务配置',expect.any(String),'warning');
 expect(document.activeElement).toBe(name);expect(api.update).not.toHaveBeenCalled();
 expect(screen.queryByRole('alert')).toBeNull();
});

it('keeps a saved preparation failure as task state without repeatedly toasting it',async()=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[{...task,prepare_error:'执行前准备失败'}],next_cursor:null});
 show(api);expect(await screen.findByText('执行前准备失败')).toBeTruthy();
 expect(notify).not.toHaveBeenCalled();
});

it('does not notify after leaving while a task mutation is still pending',async()=>{
 const api=setup();let reject!:(error:Error)=>void;
 vi.mocked(api.action).mockReturnValueOnce(new Promise((_,fail)=>{reject=fail;}));
 const view=show(api);fireEvent.click(await screen.findByRole('switch',{name:`启用${task.name}`}));
 view.unmount();reject(new Error('已经离开的操作失败'));
 await new Promise(resolve=>setTimeout(resolve,0));
 expect(notify).not.toHaveBeenCalled();expect(api.list).toHaveBeenCalledTimes(1);
});

it('preserves rapid disclosure toggles through the existing title and expand controls',async()=>{
 const api=setup();show(api);
 const expand=await screen.findByRole('button',{name:`展开${task.name}`});
 const title=screen.getByRole('button',{name:new RegExp(task.name+'.*一次')});
 const prompt=document.getElementById(expand.getAttribute('aria-controls')!)!;
 // All clicks may be queued before React commits the next frame.
 await act(async()=>{expand.click();expand.click();});
 expect(expand.getAttribute('aria-expanded')).toBe('false');
 expect(prompt.classList.contains('is-full')).toBe(false);
 await act(async()=>{title.click();expand.click();title.click();});
 expect(expand.getAttribute('aria-expanded')).toBe('true');
 expect(prompt.classList.contains('is-full')).toBe(true);
 fireEvent.click(expand);
 expect(expand.getAttribute('aria-expanded')).toBe('false');
 expect(prompt.classList.contains('is-full')).toBe(false);
});


it('ignores a stale task-list failure after a newer filter response',async()=>{
 const api=setup();let reject!:(error:Error)=>void;
 vi.mocked(api.list).mockReturnValueOnce(new Promise((_,fail)=>{reject=fail;}));
 show(api);
 fireEvent.change(screen.getByRole('combobox',{name:'任务状态'}),{target:{value:'PAUSED'}});
 await screen.findByRole('heading',{name:task.name});
 await act(async()=>{reject(new Error('过期任务错误'));});
 expect(notify).not.toHaveBeenCalled();expect(screen.queryByRole('alert')).toBeNull();
 expect(screen.getByRole('heading',{name:task.name})).toBeTruthy();
});
