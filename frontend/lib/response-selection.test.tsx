import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { MarkdownBody, normalizeMathMarkdown } from '../components/markdown-body';
import { FileLinkContext } from '../components/file-link-context';
import { AnnotationBody } from '../components/response-annotations';
import { mapResponseSelection } from './response-selection';
import { PromptDraftStore } from './prompt-draft';

vi.mock('../components/mermaid-block', () => ({ MermaidBlock: () => <section><div data-source-decoration=""><button>源码</button></div><img alt="图" /></section> }));
afterEach(() => { cleanup(); window.getSelection()?.removeAllRanges(); });
function select(start: Node, from: number, end = start, to = start.textContent?.length ?? 0) {
  const range = document.createRange(); range.setStart(start, from); range.setEnd(end, to);
  const selection = window.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  return mapResponseSelection(selection)?.annotation;
}
function mount(body: string) {
  const view = render(<AnnotationBody entryId="entry:a" body={body}><MarkdownBody annotationSource body={body} onNotify={() => {}} /></AnnotationBody>);
  return { ...view, leaves: [...view.container.querySelectorAll<HTMLElement>('[data-source-text]')] };
}

describe('canonical response selection', () => {
  it('maps decoded entities, escaped text and UTF16 inside a text node', () => {
    const body = '甲 &amp; 乙😀 \\*重复 &#x1F600; e\u0301';
    const { leaves } = mount(body); const text = leaves[0].firstChild!;
    const value = text.textContent!;
    const from = value.indexOf('乙'); const to = value.indexOf('重复') + 2;
    expect(select(text, from, text, to)).toEqual({ type: 'annotation', quote: '乙😀 \\*重复', source: { entry_id: 'entry:a', start: body.indexOf('乙'), end: body.indexOf('重复') + 2 } });
    const emoji = value.lastIndexOf('😀');
    expect(select(text, emoji, text, emoji + 1)?.quote).toBe('&#x1F600;');
  });
  it('uses exact positions for repeated text and normalized inline code whitespace', () => {
    const body = '重复 **重复** ` a\nb ` 重复'; const { leaves } = mount(body);
    const code = leaves.find(leaf => leaf.textContent === 'a b')!.firstChild!;
    expect(select(code, 1, code, 3)?.quote).toBe('\nb');
    const last = leaves.at(-1)!.firstChild!;
    expect(select(last, 1, last, 3)?.source?.start).toBe(body.lastIndexOf('重复'));
  });
  it('preserves math normalization offsets and atomic formula source', () => {
    const body = '前面\n\n$$ x+y $$\n\n后面 &amp; 尾'; const { container, leaves } = mount(body);
    expect(normalizeMathMarkdown(body)).toContain('$$\nx+y\n$$');
    const last = leaves.at(-1)!.firstChild!;
    expect(select(last, 0, last, 2)?.source?.start).toBe(body.indexOf('后面'));
    const atom = container.querySelector<HTMLElement>('[data-source-atom]')!;
    const parent = atom.parentNode!; const index = [...parent.childNodes].indexOf(atom as unknown as ChildNode);
    expect(select(parent, index, parent, index + 1)?.quote).toBe('$$ x+y $$');
  });
  it.each(['```js\nSECRET\n```', '    SECRET', '```\n\nSECRET\r\n\n```', '    A\n    SECRET', '  ```\n\tSECRET\n  ```'])('maps ordinary code with parser whitespace: %s', code => {
    const body = `${code}\n\nafter`;
    const { container, leaves } = mount(body);
    const text = [...container.querySelectorAll<HTMLElement>('pre code [data-source-text]')]
      .find(node => node.textContent?.includes('SECRET'))!.firstChild!;
    const index = text.textContent!.indexOf('SECRET');
    expect(select(text, index, text, index + 6)).toEqual({ type: 'annotation', quote: 'SECRET', source: { entry_id: 'entry:a', start: body.indexOf('SECRET'), end: body.indexOf('SECRET') + 6 } });
    expect(select(text, index, leaves.at(-1)!.firstChild!, 5)?.quote).toBe(body.slice(body.indexOf('SECRET')));
  });
  it.each(['\r\n', '\r', '\n'])('preserves original line endings %j', ending => {
    const body = `a${ending}b`;
    const { leaves } = mount(body); const text = leaves[0].firstChild!;
    expect(select(text, text.textContent!.length - 1)?.quote).toBe('b');
    expect(select(text, 0)?.quote).toBe(body);
  });
  it.each(['jsx', 'mermaid-example'])('keeps ordinary %s fences selectable as code', language => {
    const { container } = mount(`\`\`\`${language}\n<svg><text>hello</text></svg>\n\`\`\``);
    const text = [...container.querySelectorAll<HTMLElement>('pre code [data-source-text]')]
      .find(node => node.textContent?.includes('hello'))!.firstChild!;
    const from = text.textContent!.indexOf('hello');
    expect(select(text, from, text, from + 5)?.quote).toBe('hello');
  });
  it('maps colored code tokens to the canonical reply and ignores the gutter and toolbar', async () => {
    const body = '前言\n\n```python\ndef fibonacci(n):\n    return "值😀"\n```\n\n结尾';
    const { container } = mount(body);
    const card = container.querySelector('.code-card')!;
    await waitFor(() => expect(card.querySelector('[class*="code-card__token--"]')).not.toBeNull());
    const leaves = [...card.querySelectorAll<HTMLElement>('code [data-source-text]')];
    const first = leaves.find(leaf => leaf.textContent?.includes('def'))!;
    const last = leaves.find(leaf => leaf.textContent?.includes('fibonacci'))!;
    expect(select(first.firstChild!, 0, last.firstChild!, last.textContent!.length)?.quote).toBe('def fibonacci');
    fireEvent.click(card.querySelector('button[aria-label="按宽度换行"]')!);
    expect(select(first.firstChild!, 0, last.firstChild!, last.textContent!.length)?.quote).toBe('def fibonacci');
    const secondLine = [...card.querySelectorAll<HTMLElement>('code [data-source-text]')]
      .find(leaf => leaf.textContent?.includes('return'))!.firstChild!;
    expect(select(first.firstChild!, 0, secondLine, secondLine.textContent!.length)?.quote).toBe('def fibonacci(n):\n    return');
    const ending = [...container.querySelectorAll<HTMLElement>('[data-source-text]')].at(-1)!.firstChild!;
    expect(select(first.firstChild!, 0, ending, 2)?.quote).toBe(body.slice(body.indexOf('def')));
    expect([...card.querySelectorAll('.code-card__line')].map(line => line.getAttribute('data-line'))).toEqual(['1', '2']);
    const header = card.querySelector('.code-card__toolbar > span')!.lastChild!;
    expect(select(header, 0)).toBeUndefined();
  });
  it('clips element endpoints against text contents, excluding boundary-only contact', () => {
    const { container, leaves } = mount('first\n\nsecond');
    const paragraphs = container.querySelectorAll('p');
    expect(select(paragraphs[0], 1, leaves[1].firstChild!, 3)?.quote).toBe('sec');
    expect(select(leaves[0].firstChild!, 2, paragraphs[1], 0)?.quote).toBe('rst');
  });
  it('preserves table structure and column alignment while mapping selections across cells and rows', () => {
    const body = [
      '表格之前', '',
      '| 购买者群体 | 人数 | 频次中位数 | 客户平均客单价中位数 | 品类数中位数 | 退款额/支付额 | 有效退款客户占比 |',
      '| :--- | ---: | :---: | ---: | ---: | ---: | ---: |',
      '| 高频多品类 | 604 | 1.42 | ¥208.36 | 5 | 7.65% | 65.9% |',
      '| 低频轻量 | 746 | 0.33 | ¥137.94 | 3 | 6.24% | 19.7% |',
      '| 高客单价 | 530 | 0.44 | ¥520.24 | 3 | 6.88% | 36.6% |',
      '', '表格之后',
    ].join('\n');
    const { container, leaves } = mount(body);
    const table = container.querySelector('table')!;
    // Non-cell elements here create anonymous table boxes and make the header
    // and body lay out separate column widths, even with otherwise valid data.
    expect([...table.children].map(node => node.tagName)).toEqual(['THEAD', 'TBODY']);
    for (const group of table.children) expect([...group.children].every(node => node.tagName === 'TR')).toBe(true);
    for (const row of table.rows) {
      expect(row.children).toHaveLength(7);
      expect([...row.children].every(node => node.tagName === (row.parentElement!.tagName === 'THEAD' ? 'TH' : 'TD'))).toBe(true);
    }
    expect(table.rows[0].cells[1].style.textAlign).toBe('right');
    expect(table.rows[1].cells[2].style.textAlign).toBe('center');
    expect(table.querySelectorAll('[data-source-invalid]')).toHaveLength(0);
    const firstText = (node: Node) => document.createTreeWalker(node, NodeFilter.SHOW_TEXT).nextNode()!;
    const start = firstText(table.rows[0].cells[0]);
    const end = firstText(table.rows[3].cells[6]);
    expect(select(start, 0, end, 5)?.quote).toBe(body.slice(body.indexOf('购买者群体'), body.lastIndexOf('36.6%') + 5));
    expect(select(leaves[0].firstChild!, 0, leaves.at(-1)!.firstChild!, 4)?.quote).toBe(body);
  });
  it('covers rendered HTML literals and rejects any unknown selected text', () => {
    const body = 'before <b>raw</b> after'; const { container } = mount(body);
    const root = container.querySelector('[data-annotation-entry]')!;
    expect(select(root, 0, root, root.childNodes.length)?.quote).toBe(body);
    root.appendChild(document.createTextNode('unmapped'));
    expect(select(root, 0, root, root.childNodes.length)).toBeUndefined();
  });
  it('quotes image-only empty-text selections and whole diagrams through UI decoration', () => {
    const body = '前\n\n![说明](a.png)\n\n```mermaid\ngraph TD\nA-->B\n```\n\n后';
    const { container, leaves } = mount(body);
    const image = container.querySelector<HTMLElement>('[data-source-atom]')!; const parent = image.parentNode!;
    const index = [...parent.childNodes].indexOf(image as unknown as ChildNode);
    expect(select(parent, index, parent, index + 1)?.quote).toBe('![说明](a.png)');
    expect(select(leaves[0].firstChild!, 0, leaves.at(-1)!.firstChild!, 1)?.quote).toBe(body);
    const toolbar = container.querySelector('button')!.firstChild!;
    expect(select(toolbar, 0)).toBeUndefined();
  });
  it('rejects independent messages and reordered footnotes', () => {
    const view = render(<><AnnotationBody entryId="a" body="first"><MarkdownBody annotationSource body="first" onNotify={() => {}} /></AnnotationBody><div>工具</div><AnnotationBody entryId="b" body="second"><MarkdownBody annotationSource body="second" onNotify={() => {}} /></AnnotationBody></>);
    const leaves = view.container.querySelectorAll('[data-source-text]');
    expect(select(leaves[0].firstChild!, 0, leaves[1].firstChild!, 3)).toBeUndefined();
    cleanup();
    const footnotes = mount('正文[^a]\n\n[^a]: 注释');
    const root = footnotes.container.querySelector('[data-annotation-entry]')!;
    expect(select(root, 0, root, root.childNodes.length)).toBeUndefined();
  });
  it('selects a linked image without opening its link, while preserving ordinary clicks', () => {
    const body = '[![image](https://example.com/image.png)](report.pdf)';
    const open = vi.fn();
    const { container } = render(<FileLinkContext.Provider value={{ open }}><AnnotationBody entryId="entry:a" body={body}><MarkdownBody annotationSource body={body} onNotify={() => {}} /></AnnotationBody></FileLinkContext.Provider>);
    const image = container.querySelector('img')!;
    const pointer = (type: string, x: number) => fireEvent(image, new MouseEvent(type, { bubbles: true, button: 0, clientX: x, clientY: 10 }));
    pointer('pointerdown', 10); pointer('pointermove', 40); pointer('pointerup', 40);
    expect(window.getSelection()!.isCollapsed).toBe(false);
    expect(mapResponseSelection(window.getSelection())?.annotation.quote).toBe('![image](https://example.com/image.png)');
    fireEvent.click(image);
    expect(open).not.toHaveBeenCalled();
    window.getSelection()!.removeAllRanges();
    pointer('pointerdown', 10); pointer('pointerup', 10); fireEvent.click(image);
    expect(open).toHaveBeenCalledOnce();
    expect(open.mock.calls[0][0]).toBe('report.pdf');
  });
});

describe('annotation drafts', () => {
  it('keeps annotation-first order, undo, restoration and snapshot ownership', async () => {
    const store = new PromptDraftStore(); const editor = store.getEditor('s');
    store.insertText('s', '正文');
    const annotation = { type: 'annotation' as const, quote: '$skill ![a](a.png)', source: { entry_id: 'entry:a', start: 0, end: 18 }, comment: '解释' };
    store.addAnnotation('s', annotation);
    const captured = await store.capture('s');
    expect(captured.content.parts).toEqual([annotation, { type: 'text', text: '正文' }]);
    editor.commands.undo(); expect((await store.capture('s')).content.parts).toEqual([{ type: 'text', text: '正文' }]);
    editor.commands.redo();
    store.insertText('s', '开始', true);
    expect(store.clearIfSnapshot('s', captured)).toBe(false);
    expect((await store.capture('s')).content.parts[1]).toEqual({ type: 'text', text: '开始正文' });
    expect(store.restoreIfEmpty('other', captured.content)).toBe(true);
    expect((await store.capture('other')).content).toEqual(captured.content);
    store.destroy();
  });
});
