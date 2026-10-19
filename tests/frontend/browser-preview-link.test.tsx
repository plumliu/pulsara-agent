import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SandboxedHtmlPreview } from '../../frontend/components/sandboxed-html-preview';
afterEach(cleanup);
it('requires a trusted click for a document-forged external navigation without browser activation', () => {
  const navigate=vi.fn();
  const view=render(<SandboxedHtmlPreview source={{url:'/api/file-previews/token/resources/index.html'}} title="Preview" onNavigation={navigate}/>);
  const iframe=view.container.querySelector('iframe')!;
  act(()=>window.dispatchEvent(new MessageEvent('message',{source:iframe.contentWindow,data:{type:'pulsara-preview-navigation',token:'token',href:'https://example.com/',userGesture:true,isTrusted:true,newTab:true}})));
  expect(navigate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'打开网页'}));
  expect(navigate).toHaveBeenCalledWith('https://example.com/',true);
});
it('does not accept another frame or change local document navigation', () => {
  const navigate=vi.fn();
  const view=render(<SandboxedHtmlPreview source={{url:'/api/file-previews/token/resources/index.html'}} title="Preview" onNavigation={navigate}/>);
  act(()=>window.dispatchEvent(new MessageEvent('message',{source:window,data:{type:'pulsara-preview-navigation',token:'token',href:'https://example.com/'}})));
  expect(screen.queryByRole('button',{name:'打开网页'})).toBeNull();
  act(()=>window.dispatchEvent(new MessageEvent('message',{source:view.container.querySelector('iframe')!.contentWindow,data:{type:'pulsara-preview-navigation',token:'token',href:'guide.md'}})));
  expect(navigate).toHaveBeenCalledWith('guide.md',false);
});
