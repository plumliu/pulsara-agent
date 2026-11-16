import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaEvidenceIndex } from '../../frontend/components/media-evidence-index';
import { FileMediaContext } from '../../frontend/components/file-link-context';
import type { FilePreviewApi } from '../../frontend/lib/file-preview';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';
import type { ToolBusinessValue } from '../../frontend/lib/tool-business-value';

const value: ToolBusinessValue = {target:{kind:'PATH',path:'videos/canonical video.mp4'},meta:{status:'SUCCESS'},content:[
  {type:'text',value:'detail：请求 [3, 10) 秒。\n'},
  {type:'object',value:{t:3.123456789,visible_range:[3.123456789,3.2]}},
  {type:'object',value:{t:9.75,visible_range:[9.75,10]}},
]};
const trace: ToolTrace = {id:'trace',kind:'read',toolName:'read_video_frames',title:'读取画面',subtitle:'已完成',status:'completed',argumentsJson:JSON.stringify({target:{kind:'PATH',path:'wrong.mp4'},from:0,to:999}),businessValue:value};
const api: FilePreviewApi = {open:vi.fn(),snapshot:vi.fn(),close:vi.fn(),page:vi.fn(),action:vi.fn(),annotations:vi.fn()};
function mount(item: ToolTrace, open=vi.fn()) {
  return {open,...render(<FileMediaContext.Provider value={{api,ownerKey:'test',sessionId:'test',open}}><MediaEvidenceIndex trace={item}/></FileMediaContext.Provider>)};
}
afterEach(() => {cleanup();vi.clearAllMocks();});
it('uses frozen business times and the public target; opens current media only on a click', () => {
  const {open} = mount(trace);
  expect(api.open).not.toHaveBeenCalled();
  expect(open).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'定位第 1 帧到 3.123456789 秒'}));
  expect(open).toHaveBeenCalledExactlyOnceWith('videos/canonical video.mp4',3.123456789);
});
it('uses complete transcript segment times and keeps original wording', () => {
  const {open}=mount({...trace,toolName:'read_transcript',businessValue:{...value,meta:{status:'SUCCESS',source:'subtitle'},content:[{type:'object',value:{from:130.75,to:180,text:'原文片段',visible_range:{from:130.75,to:150}}}]}});
  const button=screen.getByRole('button',{name:'定位原文 1 到 130.75 秒'});
  expect(button.getAttribute('title')).toBe('原文片段');
  fireEvent.click(button);
  expect(open).toHaveBeenCalledExactlyOnceWith('videos/canonical video.mp4',130.75);
  expect(api.open).not.toHaveBeenCalled();
});
it('flattens successive collage indices in their original content order', () => {
  const {open}=mount({...trace,businessValue:{...value,content:[
    {type:'object',value:{frames:[{n:1,t:3,cell_rect:[0,0,.5,1]},{n:2,t:4,cell_rect:[.5,0,1,1]}]}},
    {type:'text',value:'后续拼图'},
    {type:'object',value:{frames:[{n:1,t:5,cell_rect:[0,0,1,1]}]}},
  ]}});
  fireEvent.click(screen.getByRole('button',{name:'定位第 3 帧到 5 秒'}));
  expect(open).toHaveBeenCalledExactlyOnceWith('videos/canonical video.mp4',5);
});
it('does not manufacture navigation from display text or arguments', () => {
  mount({...trace,businessValue:undefined,resultText:JSON.stringify(value)});
  expect(screen.queryByRole('navigation')).toBeNull();
});
it('rejects invalid time indices and a running result', () => {
  const {unmount}=mount({...trace,businessValue:{...value,content:[{type:'object',value:{t:-1}}]}});
  expect(screen.queryByRole('navigation')).toBeNull();
  unmount();
  mount({...trace,status:'running'});
  expect(screen.queryByRole('navigation')).toBeNull();
});
