import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ToolBusinessContent} from '../../frontend/components/tool-business-content';
import {FileLinkContext,FileMediaContext} from '../../frontend/components/file-link-context';
import type {ToolBusinessValue} from '../../frontend/lib/tool-business-value';

afterEach(cleanup);
it('renders file-search lines and counts with actionable paths and exact text',()=>{
 const open=vi.fn();
 const snippet='  foo\tfoo\n';
 const value:ToolBusinessValue={target:{kind:'PATH',path:'src'},meta:{status:'SUCCESS'},content:[
  {type:'object',value:{target:{kind:'PATH',path:'src/a.py'},line:7,text:snippet}},
  {type:'object',value:{target:{kind:'PATH',path:'src/b.py'},count:3}},
 ]};
 const {container}=render(<FileLinkContext.Provider value={{open}}><ToolBusinessContent value={value} toolName="search_file_content"/></FileLinkContext.Provider>);
 expect(container.querySelector('.tool-search-result pre')?.textContent).toBe(snippet);
 expect(screen.getByText('第 7 行')).toBeTruthy();
 expect(screen.getByText('3 次匹配')).toBeTruthy();
 const link=screen.getByRole('link',{name:'src/a.py'});
 fireEvent.click(link);
 expect(open).toHaveBeenCalledExactlyOnceWith('src/a.py',link);
 expect(screen.queryByText('查看结构化结果')).toBeNull();
});
it('shows original transcript text and actionable times, without dumping business metadata',()=>{
 const open=vi.fn();
 const value:ToolBusinessValue={target:{kind:'PATH',path:'audio.wav'},meta:{status:'SUCCESS',source:'subtitle',language:'zh'},internal_extra:'not useful to readers',content:[{type:'text',value:'请求范围说明\n'},{type:'object',value:{from:1,to:2,text:'原文\n不改写',visible_range:{from:1.5,to:2}}}]};
 render(<FileMediaContext.Provider value={{api:{} as never,ownerKey:'read',sessionId:'session',open}}><ToolBusinessContent value={value} toolName="read_transcript"/></FileMediaContext.Provider>);
 expect(screen.getByText('字幕原文')).toBeTruthy();
 expect(screen.getByText('原文 不改写')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'0:01.000–0:02.000'}));
 expect(open).toHaveBeenCalledExactlyOnceWith('audio.wav',1);
 expect(screen.queryByText(/internal_extra|visible_range|SUCCESS|"meta"/)).toBeNull();
});
it('keeps general structured details closed until explicitly requested',()=>{
 render(<ToolBusinessContent value={{meta:{status:'SUCCESS'},content:[{type:'object',value:{answer:42}}]}}/>);
 const summary=screen.getByText('查看结构化结果');
 expect(summary.closest('details')?.open).toBe(false);
 expect(screen.queryByText(/"status"/)).toBeNull();
});
it('retains non-path targets as inspectable structured content',()=>{
 const target={kind:'SKILL',name:'example-skill'};
 render(<ToolBusinessContent value={{meta:{status:'SUCCESS'},content:[{type:'target',value:target}]}}/>);
 const details=screen.getByText('查看结构化结果').closest('details')!;
 expect(details.open).toBe(false);
 expect(details.querySelector('pre')?.textContent).toBe(JSON.stringify(target,null,2));
});
it('explains an empty MCP result only when there is no other displayed content',()=>{
 const value:ToolBusinessValue={meta:{status:'SUCCESS'},content:[]};
 const {rerender}=render(<ToolBusinessContent toolName="call_mcp_tool" value={value}/>);
 expect(screen.getByText('本次调用未返回内容。')).toBeTruthy();
 rerender(<ToolBusinessContent toolName="call_mcp_tool" value={{...value,structuredContent:null}}/>);
 expect(screen.queryByText('本次调用未返回内容。')).toBeNull();
 rerender(<ToolBusinessContent toolName="call_mcp_tool" value={{...value,target:{kind:'PATH',path:'src/a.py'}}}/>);
 expect(screen.queryByText('本次调用未返回内容。')).toBeNull();
 rerender(<ToolBusinessContent toolName="call_mcp_tool" value={value} inspectFields/>);
 expect(screen.queryByText('本次调用未返回内容。')).toBeNull();
});
it('shows top-level result fields only when requested, without duplicating content',()=>{
 const value:ToolBusinessValue={target:{kind:'PLUGIN',name:'demo'},meta:{status:'SUCCESS'},extra:'execution fact',content:[{type:'text',value:'business content'}],structuredContent:{answer:42}};
 const {rerender}=render(<ToolBusinessContent value={value}/>);
 expect(screen.queryByText('查看结果信息')).toBeNull();
 rerender(<ToolBusinessContent value={value} inspectFields/>);
 const details=screen.getByText('查看结果信息').closest('details')!;
 expect(details.open).toBe(false);
 expect(JSON.parse(details.querySelector('pre')!.textContent!)).toEqual({target:value.target,meta:value.meta,extra:'execution fact'});
 expect(screen.getByText('business content')).toBeTruthy();
 expect(screen.getByText('查看结构化结果')).toBeTruthy();
});

it.each([null,false,0,'',[],{forecast:'sunny',nested:{status:'business-data'}}])('keeps actual MCP structured value inspectable and copyable: %j', structuredContent=>{
 const copy=vi.fn();
 const encoded=JSON.stringify(structuredContent,null,2);
 const {container}=render(<ToolBusinessContent toolName="call_mcp_tool" onCopy={copy} value={{meta:{status:'SUCCESS'},content:[],structuredContent}}/>);
 const details=screen.getByText('查看结构化结果').closest('details')!;
 expect(details.open).toBe(false);
 details.open=true;
 expect(details.querySelector('pre')?.textContent).toBe(encoded);
 fireEvent.click(screen.getByRole('button',{name:'复制结构化结果'}));
 expect(copy).toHaveBeenCalledExactlyOnceWith(encoded);
 expect(container.textContent).not.toContain('SUCCESS');
});

it('retains formal MCP content beside structuredContent and omits absent structured details',()=>{
 const {rerender}=render(<ToolBusinessContent toolName="call_mcp_tool" value={{meta:{status:'ERROR'},content:[{type:'text',value:'actual tool diagnostic'}],structuredContent:{completed:2}}}/>);
 expect(screen.getByText('actual tool diagnostic')).toBeTruthy();
 expect(screen.getByText('查看结构化结果')).toBeTruthy();
 rerender(<ToolBusinessContent toolName="call_mcp_tool" value={{meta:{status:'SUCCESS'},content:[{type:'text',value:'formal text only'}]}}/>);
 expect(screen.getByText('formal text only')).toBeTruthy();
 expect(screen.queryByText('查看结构化结果')).toBeNull();
});
