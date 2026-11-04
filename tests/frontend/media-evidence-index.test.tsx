import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaEvidenceIndex } from '../../frontend/components/media-evidence-index';
import { FileMediaContext } from '../../frontend/components/file-link-context';
import type { FilePreviewApi } from '../../frontend/lib/file-preview';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';

const page = {target:{kind:'PATH',path:'/project/canonical video.mp4'},media_version:{modified_ns:'1823467890123456789',size:100},crop:[.1,.2,.8,.9],frames:[{n:1,image:1,t:3.123456789},{n:2,image:1,t:9.75}]};
const trace: ToolTrace = {id:'trace',kind:'read',toolName:'read_video_frames',title:'读取画面',subtitle:'已完成',status:'completed',argumentsJson:JSON.stringify({target:{kind:'PATH',path:'wrong.mp4'},from:0,to:999}),resultText:JSON.stringify(page)};
const api: FilePreviewApi = {open:vi.fn(),snapshot:vi.fn(),close:vi.fn(),page:vi.fn(),action:vi.fn(),annotations:vi.fn()};
function mount(value: ToolTrace, open=vi.fn()) {
  return {open,...render(<FileMediaContext.Provider value={{api,ownerKey:'test',sessionId:'test',open}}><MediaEvidenceIndex trace={value}/></FileMediaContext.Provider>)};
}
afterEach(() => {cleanup();vi.clearAllMocks();});
it('uses exact canonical source, time, crop and file version with no media load until clicked', () => {
  const {open} = mount({...trace,resultText:undefined,resultContent:{parts:[{type:'text',text:JSON.stringify(page)}]}});
  expect(api.open).not.toHaveBeenCalled();
  expect(open).not.toHaveBeenCalled();
  const button=screen.getByRole('button',{name:'定位第 1 帧到 3.123456789 秒'});
  expect(button.getAttribute('title')).toContain('crop=[0.1,0.2,0.8,0.9]');
  fireEvent.click(button);
  expect(open).toHaveBeenCalledExactlyOnceWith('/project/canonical video.mp4',3.123456789,{modified_ns:'1823467890123456789',size:100});
});
it('uses transcript absolute times without reading next or inferring finer timestamps', () => {
  const {open}=mount({...trace,toolName:'read_transcript',resultText:JSON.stringify({...page,frames:undefined,crop:undefined,entries:[{from:130.75,to:180,text:'原文片段',position:{index:2,line:1,char_offset:20}}],next:{tool:'read_transcript',arguments:{continuation:{position:'unread'}}}})});
  fireEvent.click(screen.getByRole('button',{name:'定位原文 1 到 130.75 秒'}));
  expect(open).toHaveBeenCalledExactlyOnceWith(page.target.path,130.75,page.media_version);
  expect(api.open).not.toHaveBeenCalled();
});
it.each([
  {...page,media_version:undefined}, {...page,frames:[{n:1,image:1,t:-1}]}, {...page,crop:[0,0,0,1]},
])('does not invent navigable evidence from incomplete or invalid pages', value => {
  mount({...trace,resultText:JSON.stringify(value)});
  expect(screen.queryByRole('navigation')).toBeNull();
});
it('does not show generated parameters or a running tool as delivered evidence', () => {
  mount({...trace,status:'running'});
  expect(screen.queryByRole('navigation')).toBeNull();
});
