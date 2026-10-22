import { afterEach, describe, expect, it, vi } from 'vitest';
import { captureHistoryAnchor, collectHistoryResidence, hasHistorySelection, restoreHistoryAnchor, waitForHistoryEntry } from '../../frontend/lib/history-dom';
import { registerAnnotationBody, waitForAnnotationBody } from '../../frontend/lib/response-selection';

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('rendered history selections', () => {
  afterEach(() => document.getSelection()?.removeAllRanges());
  it.each(['text', 'whitespace', 'hidden', 'inert', 'unrendered', 'outside', 'outside-and-empty'] as const)('recognizes only visible conversation text: %s', mode => {
    document.body.innerHTML = '<p>外部文字</p><main><span>正文</span></main>';
    const root = document.querySelector('main')!;
    const text = root.querySelector('span')!;
    if (mode === 'whitespace' || mode === 'outside-and-empty') text.textContent = ' \n\t ';
    if (mode === 'hidden' || mode === 'inert') text.setAttribute(mode, '');
    vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue((mode === 'unrendered' ? [] : [new DOMRect(0, 0, 100, 20)]) as unknown as DOMRectList);
    const range = document.createRange();
    range.selectNodeContents(mode === 'outside' ? document.querySelector('p')! : root);
    if (mode === 'hidden' || mode === 'inert') range.selectNodeContents(text);
    if (mode === 'outside-and-empty') range.setStartBefore(document.querySelector('p')!);
    document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
    expect(hasHistorySelection(root)).toBe(mode === 'text');
  });
});

it('retains viewport preparation, selection aliases and focus without pinning every expanded disclosure', () => {
 const root=document.createElement('div');document.body.append(root);
 root.innerHTML='<div data-source-entry="near" data-source-entries="near result"><article data-result-entry="result">visible</article></div><div data-source-entry="selected">selected text</div><div data-source-entry="focused"><button>focus</button></div><div data-source-entry="expanded" data-process-expanded="true">far expanded</div><section inert><div data-source-entry="hidden">closed</div></section>';
 vi.spyOn(root,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,500));
 for(const node of root.querySelectorAll<HTMLElement>('[data-source-entry], [data-result-entry]')) vi.spyOn(node,'getBoundingClientRect').mockReturnValue(new DOMRect(0,node.dataset.sourceEntry==='near'||node.dataset.resultEntry?550:2000,600,40));
 root.querySelector('button')!.focus();
 const range=document.createRange();range.selectNodeContents(root.querySelector('[data-source-entry="selected"]')!);document.getSelection()!.removeAllRanges();document.getSelection()!.addRange(range);
 expect(collectHistoryResidence(root)).toEqual({visibleEntryIds:['near','result'],pinnedEntryIds:['selected','focused']});
 document.getSelection()!.removeAllRanges();
 expect(collectHistoryResidence(root).pinnedEntryIds).toEqual(['focused']);
 root.hidden=true;expect(collectHistoryResidence(root)).toEqual({visibleEntryIds:[],pinnedEntryIds:[]});
});

it('anchors a collapsed fragment through prepend without treating its header as a loaded body or pinning all aliases',async()=>{
 const root=document.createElement('div');document.body.append(root);
 root.innerHTML='<button data-history-entry="old" data-history-entry-aliases="old hidden:1 hidden:2">collapsed process</button><button data-history-entry="other" data-history-entry-aliases="other">other fragment</button>';
 vi.spyOn(root,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,500));
 const header=root.querySelector('button')!;
 const rect=vi.spyOn(header,'getBoundingClientRect').mockReturnValue(new DOMRect(0,30,600,30));
 expect(collectHistoryResidence(root).visibleEntryIds).toEqual(['old']);
 const anchor=captureHistoryAnchor(root)!;expect(anchor).toMatchObject({entryId:'old',header:true,top:30});
 let located=false;const abort=new AbortController();const location=waitForHistoryEntry(root,'old',abort.signal).then(()=>{located=true;},()=>{});
 await Promise.resolve();expect(located).toBe(false);
 header.dataset.historyEntry='prepended';header.dataset.historyEntryAliases='prepended old hidden:1 hidden:2';
 rect.mockReturnValue(new DOMRect(0,180,600,30));
 restoreHistoryAnchor(root,anchor);expect(root.scrollTop).toBe(150);
 expect(collectHistoryResidence(root).visibleEntryIds).toEqual(['prepended']);
 abort.abort();await location;
});

describe('canonical history DOM completion and anchors', () => {
  it('keeps source registration pending beyond DOM insertion until the verified body is registered', async () => {
    const controller = new AbortController();
    let completed = false;
    const ready = waitForAnnotationBody('old', controller.signal).then(node => { completed = true; return node; });
    const root = document.createElement('div'); root.dataset.annotationEntry = 'old'; document.body.append(root);
    await Promise.resolve();
    expect(completed).toBe(false);
    const cleanup = registerAnnotationBody(root, '中文原文');
    expect(await ready).toBe(root);
    cleanup();
  });

  it('cancels a registration wait before a late old source is mounted', async () => {
    const controller = new AbortController();
    const result = waitForAnnotationBody('old', controller.signal).catch(error => error);
    controller.abort();
    const root = document.createElement('div'); root.dataset.annotationEntry = 'old'; document.body.append(root);
    const cleanup = registerAnnotationBody(root, 'late');
    expect(await result).toMatchObject({ name: 'AbortError' });
    cleanup();
  });

  it('does not declare a loading shell to be a located entry', async () => {
    const root = document.createElement('div'); document.body.append(root);
    root.innerHTML = '<div data-source-entry="old" data-content-state="loading"><p>正文读取中</p></div>';
    let completed = false;
    const ready = waitForHistoryEntry(root, 'old', new AbortController().signal).then(node => { completed = true; return node; });
    await Promise.resolve(); expect(completed).toBe(false);
    root.firstElementChild!.setAttribute('data-content-state', 'ready');
    expect(await ready).toBe(root.querySelector('p'));
  });

  it('restores the same tool result after a fallback entry merges into its real assistant trace', () => {
    const root = document.createElement('div'); document.body.append(root);
    root.innerHTML = '<div data-source-entry="result"><article>独立结果</article></div>';
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 600, 500));
    vi.spyOn(root.querySelector('article')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 35, 600, 60));
    const anchor = captureHistoryAnchor(root)!;
    expect(anchor).toMatchObject({ entryId: 'result', top: 35 });
    root.innerHTML = '<div data-source-entry="assistant"><article><div data-result-entry="result">真实工具结果</div></article></div>';
    vi.spyOn(root.querySelector('[data-result-entry]')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 235, 600, 60));
    root.scrollTop = 100;
    restoreHistoryAnchor(root, anchor);
    expect(root.scrollTop).toBe(300);
  });

  it('preserves a source-text subanchor when preceding Markdown changes height', () => {
    const root = document.createElement('div'); document.body.append(root);
    root.innerHTML = '<div data-source-entry="answer"><article><span data-source-text="[[10,11],[11,12]]">原句</span></article></div>';
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 600, 500));
    const rect = vi.spyOn(root.querySelector('span')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 20, 60, 20));
    const anchor = captureHistoryAnchor(root)!;
    expect(anchor).toMatchObject({ entryId: 'answer', sourceOffset: 10, top: 20 });
    rect.mockReturnValue(new DOMRect(0, 140, 60, 20));
    restoreHistoryAnchor(root, anchor);
    expect(root.scrollTop).toBe(120);
  });
});

it('prefers an exact final-answer host over its earlier reasoning-process alias', async () => {
  const root = document.createElement('div'); document.body.append(root);
  root.innerHTML = '<div data-source-entry="answer:process" data-source-entries="answer" data-content-state="loading"><p>思考读取中</p></div><div data-source-entry="answer" data-content-state="ready"><article>正文已可读</article></div>';
  const target = await waitForHistoryEntry(root, 'answer', new AbortController().signal);
  expect(target.textContent).toBe('正文已可读');
  expect(target.closest('[data-source-entry]')?.getAttribute('data-source-entry')).toBe('answer');
});

it('does not anchor or locate a closed disclosure while its exit animation retains the DOM', async () => {
  const root=document.createElement('div');document.body.append(root);
  root.innerHTML='<section inert><div data-source-entry="closed" data-content-state="ready"><p>hidden body</p></div></section>';
  vi.spyOn(root,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,500));
  vi.spyOn(root.querySelector('p')!,'getBoundingClientRect').mockReturnValue(new DOMRect(0,20,600,100));
  expect(captureHistoryAnchor(root)).toBeUndefined();
  let located=false;
  const ready=waitForHistoryEntry(root,'closed',new AbortController().signal).then(node=>{located=true;return node;});
  await Promise.resolve();expect(located).toBe(false);
  root.querySelector('section')!.removeAttribute('inert');
  expect((await ready).textContent).toBe('hidden body');
});

it('restores the bottom process control without confusing it with the top control of the same canonical entry',()=>{
  const root=document.createElement('div');document.body.append(root);
  root.innerHTML='<button data-history-entry="answer" data-process-edge="top">顶部</button><button data-history-entry="answer" data-process-edge="bottom">底部</button>';
  vi.spyOn(root,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,500));
  const [top,bottom]=root.querySelectorAll('button');
  vi.spyOn(top,'getBoundingClientRect').mockReturnValue(new DOMRect(0,-1000,100,24));
  const position=vi.spyOn(bottom,'getBoundingClientRect').mockReturnValue(new DOMRect(0,100,100,24));
  const anchor=captureHistoryAnchor(root)!;
  expect(anchor).toMatchObject({entryId:'answer',processEdge:'bottom',top:100});
  position.mockReturnValue(new DOMRect(0,180,100,24));
  restoreHistoryAnchor(root,anchor);
  expect(root.scrollTop).toBe(80);
});
