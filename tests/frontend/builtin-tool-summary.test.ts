import {describe,expect,it} from 'vitest';
import type {ToolTrace} from '../../frontend/lib/pulsara-types';
import type {ToolBusinessValue} from '../../frontend/lib/tool-business-value';
import {builtinToolSummary} from '../../frontend/lib/builtin-tool-summary';
function trace(name:string,value?:ToolBusinessValue,args:unknown={},extra:Partial<ToolTrace>={}):ToolTrace {
 return {id:'call',kind:'tool',toolName:name,title:name,subtitle:'',status:'completed',resultState:'SUCCESS',
  argumentsJson:JSON.stringify(args),businessValue:value,...extra};
}
const value=(meta:Record<string,unknown>={},content:ToolBusinessValue['content']=[],fields:Record<string,unknown>={}):ToolBusinessValue=>({...fields,meta:{status:'SUCCESS',...meta},content});
describe('builtin summaries from saved typed business values',()=>{
 it.each([
  ['read_file',value({total_lines:42},[{type:'text',value:'RAW FILE'}],{target:{kind:'PATH',path:'src/main.py'}}),{},'读取文件','共 42 行'],
  ['view_image',value(),{target:{kind:'IMAGE_REF',image_ref:'private-id'}},'查看图片','对话中已保存的图片'],
  ['search_content',value({},[{type:'object',value:{line:1}},{type:'object',value:{line:2}}]),{pattern:'TODO'},'搜索内容','本次找到 2 行匹配'],
  ['find_files',value({},[{type:'target',value:{kind:'PATH',path:'a.py'}}]),{glob:'*.py'},'查找文件','本次找到 1 个文件'],
  ['search_content',value({},[{type:'object',value:{target:{kind:'PATH',path:'a.py'},count:3}}]),{output_mode:'count'},'搜索内容','本次列出 1 个文件的匹配行数'],
  ['search_content',value({},[{type:'target',value:{kind:'PATH',path:'a.py'}}]),{output_mode:'files_only'},'搜索内容','本次找到 1 个文件'],
  ['write_file',value({bytes_written:123}),{},'写入文件','已写入 123 字节'],
  ['read_file',value(),{target:{kind:'NEXT',ref:'private-id'}},'读取内容','已读取文件'],
  ['edit_file',value({},[{type:'text',value:'RAW DIFF'}]),{},'修改文件','已修改文件'],
  ['query_processes',value({},[{type:'object',value:{target:{kind:'PROCESS',process_id:'private-id'}}}]),{},'查看命令','本次列出 1 个命令进程'],
  ['enter_plan',value(),{},'开始规划','已进入规划模式'],
  ['ask_plan_question',value(),{},'确认方案细节','已记录你的选择'],
  ['exit_plan',value(),{},'提交方案','等待你的确认'],
  ['search_memory',value({},[{type:'object',value:{statement:'RAW MEMORY'}}]),{},'搜索记忆','本次找到 1 条相关记忆'],
 ])('%s shows necessary facts without dumping metadata',(name,business,args,title,detail)=>{
  const summary=builtinToolSummary(trace(name as string,business as ToolBusinessValue,args));
  expect(summary?.title).toBe(title);expect(summary?.subtitle).toContain(detail);
  expect(summary?.subtitle).not.toMatch(/RAW |private-id|process_status|SUCCESS/);
 });
 it.each([
  ['exec_process','', 'RUNNING',null,'命令仍在后台运行'],
  ['exec_process','', 'EXITED',0,'命令执行完成'],
  ['exec_process','', 'EXITED',7,'命令未成功，退出码 7'],
  ['manage_process','poll','RUNNING',null,'命令仍在运行'],
  ['manage_process','wait','RUNNING',null,'等待结束，命令仍在运行'],
  ['manage_process','write','RUNNING',null,'已发送输入 · 命令仍在运行'],
  ['manage_process','wait','EXITED',0,'命令执行完成'],
  ['manage_process','poll','EXITED',7,'命令未成功，退出码 7'],
  ['manage_process','kill','EXITED',-15,'命令未成功，退出码 -15'],
 ])('describes actual %s/%s process status',(name,action,status,code,expected)=>{
  const item=trace(name,value({process_status:status,exit_code:code,cwd:'/internal'},[{type:'text',value:'RAW OUTPUT'}],{target:{kind:'PROCESS',process_id:'private-id'}}),{action});
  // A command printing JSON must not alter the actual saved process facts.
  item.resultText='{"status":"running","exit_code":-1}';
  expect(builtinToolSummary(item)?.subtitle).toBe(expected);
 });
 it('does not infer lifecycle metadata from display text or private result facts',()=>{
  const item=trace('exec_process',value({process_status:'EXITED',exit_code:2}),{},
   {resultText:'{"status":"success","exit_code":0}',resultFacts:{status:'running',exit_code:-1}});
  expect(builtinToolSummary(item)?.subtitle).toBe('命令未成功，退出码 2');
 });
 it('keeps cancellation and missing confirmed results distinct',()=>{
  expect(builtinToolSummary(trace('write_file',undefined,{}, {status:'cancelled',resultState:undefined}))?.subtitle).toBe('未记录到操作结果');
  expect(builtinToolSummary(trace('write_file',value({status:'CANCELLED'}),{}, {status:'cancelled',resultState:'CANCELLED'}))?.subtitle).toBe('操作已取消');
  expect(builtinToolSummary(trace('write_file',value({status:'ERROR'}),{}, {status:'failed',resultState:'APPLICATION_ERROR'}))?.subtitle).toBe('操作失败');
 });
 it('uses only known presentation facts for the display request summary',()=>{
  expect(builtinToolSummary(trace('open_in_pulsara',value(),{}, {resultFacts:{presentation_targets:[{target:{kind:'PATH',path:'/one'}}]}}))?.subtitle).toBe('已请求展示 1 个目标');
 });
 it.each(['mcp__server__read_file','use_new_mcp_tool','read_mcp_resource'])('leaves %s to external presentation',name=>{
  expect(builtinToolSummary(trace(name,value()))).toBeUndefined();
 });
});
