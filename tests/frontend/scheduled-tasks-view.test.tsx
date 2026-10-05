import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ScheduledTasksView } from '../../frontend/components/scheduled-tasks-view';
import { LocalScheduledTasksApi, ScheduledApiError, type ScheduledTask } from '../../frontend/lib/scheduled-tasks-api';
import type { ModelConfigurationSummary } from '../../frontend/lib/runtime-adapter';
import type { SessionSummary } from '../../frontend/lib/pulsara-types';
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const task: ScheduledTask = { id:'scheduled:a',session_id:'session:a',name:'每日报告',prompt:'检查进展',status:'ACTIVE',revision:1,next_run_at:'2026-10-06T00:00:00Z',timezone:'Asia/Shanghai',permission_mode:'ask-permissions',schedule:{contract:'scheduled-rule:v1',kind:'once',run_at_utc:'2026-10-06T00:00:13Z'} };
const model:ModelConfigurationSummary={id:'model:a',source:'models_dev',route_id:'test',route_name:'测试供应商',wire_api:'openai_responses',model_id:'test-model',status:'ready',base_url:'http://localhost',authentication:'none',credential_configured:false,reasoning:{kind:'selectable',effort:{values:['high','max']}},default_reasoning:{kind:'effort',value:'high'}};
function setup() {
 const api=new LocalScheduledTasksApi();
 vi.spyOn(api,'list').mockResolvedValue({tasks:[task],next_cursor:null});
 vi.spyOn(api,'preview').mockResolvedValue({next_run_at:task.next_run_at!,local_time_fold:null});
 vi.spyOn(api,'create').mockResolvedValue(task);
 vi.spyOn(api,'get').mockResolvedValue(task);
 vi.spyOn(api,'update').mockResolvedValue(task);
 vi.spyOn(api,'action').mockResolvedValue({task:null});
 vi.spyOn(api,'runNow').mockResolvedValue({queue_item_id:'queue:a',status:'PENDING'});
 return api;
}
function show(api:LocalScheduledTasksApi,onOpenSession=vi.fn(),onCreateSession=vi.fn(async()=> 'session:new')) { return render(<ScheduledTasksView modelConfigurations={[model]} onCreateSession={onCreateSession} api={api} ready sessions={[{id:'session:a',title:'普通会话'} as SessionSummary]} onOpenSession={onOpenSession} onOpenSettings={vi.fn()} />); }
it('retains the complete run action after unknown ACK and task revision refresh',async()=>{
 const api=setup();vi.mocked(api.runNow).mockRejectedValueOnce(new Error('network lost'));
 show(api);fireEvent.click(await screen.findByRole('button',{name:'立即运行'}));
 await screen.findByRole('alert');const original=vi.mocked(api.runNow).mock.calls[0][0];
 vi.mocked(api.list).mockResolvedValue({tasks:[{...task,revision:2,prompt:'新提示词'}],next_cursor:null});
 fireEvent.click(screen.getByRole('button',{name:'刷新定时任务'}));await screen.findByText('新提示词');
 fireEvent.click(screen.getByRole('button',{name:'立即运行'}));await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(2));
 expect(vi.mocked(api.runNow).mock.calls[1][0]).toEqual(original);expect(original.expected_revision).toBe(1);
});
it('rejects definite conflicting run and uses a new identity after reload',async()=>{
 const api=setup();vi.mocked(api.runNow).mockRejectedValueOnce(new ScheduledApiError('任务已修改',409));
 vi.mocked(api.list).mockResolvedValueOnce({tasks:[task],next_cursor:null}).mockResolvedValue({tasks:[{...task,revision:2}],next_cursor:null});
 show(api);fireEvent.click(await screen.findByRole('button',{name:'立即运行'}));await screen.findByRole('alert');await waitFor(()=>expect(api.list).toHaveBeenCalledTimes(2));
 fireEvent.click(screen.getByRole('button',{name:'立即运行'}));await waitFor(()=>expect(api.runNow).toHaveBeenCalledTimes(2));
 const [a,b]=vi.mocked(api.runNow).mock.calls.map(c=>c[0]);expect(b.expected_revision).toBe(2);expect(b.client_command_id).not.toBe(a.client_command_id);
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
});
it('links interrupted scheduled execution to its retained ordinary history entry',async()=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[{...task,status:'COMPLETED',last_turn_status:'INTERRUPTED',last_entry_id:'entry:user'}],next_cursor:null});const open=vi.fn();show(api,open);
 fireEvent.click(await screen.findByRole('button',{name:'已中断 · 查看记录'}));expect(open).toHaveBeenCalledWith('session:a','entry:user');
});
it('groups a dispatched once task under enabled without presenting a resume or pause action',async()=>{
 const api=setup();vi.mocked(api.list).mockResolvedValue({tasks:[task,{...task,id:'scheduled:once',name:'一次性检查',status:'COMPLETED',next_run_at:null}],next_cursor:null});show(api);
 const heading=await screen.findByRole('heading',{name:'一次性检查'});const row=heading.closest('article')!;
 expect(within(row).getByText('已启用')).toBeTruthy();
 expect(within(row).getByText('一次性计划已派发')).toBeTruthy();
 expect(within(row).queryByRole('button',{name:'恢复'})).toBeNull();
 expect(within(row).queryByRole('button',{name:'暂停'})).toBeNull();
 const filter=screen.getByRole('combobox',{name:'任务状态'});
 expect(within(filter).queryByRole('option',{name:'已派发'})).toBeNull();
 fireEvent.change(filter,{target:{value:'ENABLED'}});
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith('ENABLED',undefined,expect.any(AbortSignal)));
 expect(screen.getByRole('heading',{name:'一次性检查'})).toBeTruthy();
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
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));await screen.findByText('保存失败');
 expect((screen.getByRole('combobox',{name:'供应商 / 模型'}) as HTMLSelectElement).disabled).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'保存任务'}));await waitFor(()=>expect(api.create).toHaveBeenCalledTimes(2));
 expect(create).toHaveBeenCalledTimes(1);expect(vi.mocked(api.create).mock.calls.map(c=>c[0])).toEqual(['session:new','session:new']);
});
