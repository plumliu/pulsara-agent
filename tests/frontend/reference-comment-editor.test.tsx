import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { ReferenceCommentEditor } from '../../frontend/components/reference-comment-editor';
import type { SkillCapability } from '../../frontend/lib/pulsara-types';
import { formatWorkspaceReference } from '../../frontend/lib/file-reference';
const skill={name:'pdf',enabled:true,effective:true} as SkillCapability;
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it('uses the same skill and workspace candidates in a comment and sends their exact text',async()=>{
 const file={kind:'file' as const,name:'a.pdf',path:'/workspace/a.pdf',relative_path:'a.pdf'};
 const complete=vi.fn(async()=>({directory:'/workspace',items:[file],next_cursor:null}));
 const changed=vi.fn();
 function Comment(){const [value,setValue]=useState('');return <ReferenceCommentEditor value={value} onChange={text=>{setValue(text);changed(text);}} skills={[skill]} completePaths={complete}/>;}
 render(<Comment/>);const user=userEvent.setup();const box=screen.getByRole('textbox',{name:'批注内容'});
 await user.type(box,'/pd');
 fireEvent.click(await screen.findByRole('button',{name:'pdf'}));
 await waitFor(()=>expect(changed).toHaveBeenLastCalledWith('/pdf '));
 expect(screen.getByLabelText('技能：pdf')).toBeTruthy();
 await user.type(box,'@a');
 fireEvent.click(await screen.findByRole('button',{name:'a.pdf'}));
 await waitFor(()=>expect(changed).toHaveBeenLastCalledWith(`/pdf ${formatWorkspaceReference(file)} `));
 expect(complete).toHaveBeenCalledWith('a',null,expect.any(AbortSignal));
});
it.each(['`/pd','```js\n/pd','> /pd','https://example.com/pd'])('does not offer explicit skill activation inside %s',async value=>{
 render(<ReferenceCommentEditor value={value} onChange={()=>{}} skills={[skill]}/>);
 await new Promise(resolve=>setTimeout(resolve,30));
 expect(screen.queryByRole('option')).toBeNull();
 expect(screen.getByRole('textbox').textContent).toContain('/pd');
});
