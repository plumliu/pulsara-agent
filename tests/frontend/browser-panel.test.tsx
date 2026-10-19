import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { BrowserPanel } from '../../frontend/components/browser-panel';
import { BrowserTabs, type BrowserTabRecord } from '../../frontend/lib/browser-tabs';
import type { BrowserState } from '../../frontend/lib/desktop-host';
afterEach(cleanup);
const state=(record:BrowserTabRecord,url:string):BrowserState=>({sessionId:record.sessionId,tabId:record.id,instanceId:'native',url,pendingUrl:null,title:'Page',loading:false,canGoBack:false,canGoForward:false,error:null,crashed:false});
const tabs=()=>new BrowserTabs(undefined,{visibleSession:()=>'',activeId:()=>undefined,insert:vi.fn(),changed:vi.fn(),activate:vi.fn(),shortcut:vi.fn()});
it('does not focus a temporary blank field for a popup or supplied URL; shows the committed URL',()=>{
 const record:BrowserTabRecord={id:'popup',sessionId:'a',pendingNavigation:'https://example.com/'};
 const manager=tabs(); const view=render(<BrowserPanel record={record} tabs={manager} isOpen/>);
 const input=screen.getByRole('textbox',{name:'网页地址'});
 expect(document.activeElement).not.toBe(input);
 record.pendingNavigation=undefined;record.state=state(record,'https://example.com/');
 view.rerender(<BrowserPanel record={record} tabs={manager} isOpen/>);
 expect((input as HTMLInputElement).value).toBe('https://example.com/');
});
it('focuses a deliberate empty tab, preserves editing against observations, and uses actual URL after blur',()=>{
 const record:BrowserTabRecord={id:'blank',sessionId:'a',focusAddressOnOpen:true};const manager=tabs();
 const view=render(<BrowserPanel record={record} tabs={manager} isOpen/>);
 const input=screen.getByRole('textbox',{name:'网页地址'}) as HTMLInputElement;
 expect(document.activeElement).toBe(input);
 fireEvent.change(input,{target:{value:'https://draft.test/'}});
 record.state=state(record,'https://actual.test/');
 view.rerender(<BrowserPanel record={record} tabs={manager} isOpen/>);
 expect(input.value).toBe('https://draft.test/');
 act(()=>input.blur());expect(input.value).toBe('https://actual.test/');
});
it('submits a scheme-free address with no placeholder',()=>{
 const record:BrowserTabRecord={id:'blank',sessionId:'a',focusAddressOnOpen:true}; const manager=tabs();
 const navigate=vi.spyOn(manager,'navigate').mockResolvedValue(undefined);
 render(<BrowserPanel record={record} tabs={manager} isOpen/>);
 const input=screen.getByRole('textbox',{name:'网页地址'});
 expect(input.hasAttribute('placeholder')).toBe(false);
 fireEvent.change(input,{target:{value:'example.com'}});
 fireEvent.submit(input.closest('form')!);
 expect(navigate).toHaveBeenCalledWith(record,'https://example.com/');
});
