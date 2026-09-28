import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderMermaid, type MermaidImage } from '../lib/mermaid-renderer';
import { MarkdownBody } from './markdown-body';
import { MermaidBlock } from './mermaid-block';

vi.mock('../lib/mermaid-renderer', () => ({ renderMermaid: vi.fn() }));
const renderDiagram = vi.mocked(renderMermaid);
const source = 'flowchart TD\n  A[开始] --> B[完成]';
const diagram = { svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 100"/>', width: 300, height: 100 };
const notify = vi.fn();

beforeEach(() => {
  renderDiagram.mockReset();
  renderDiagram.mockResolvedValue(diagram);
  notify.mockClear();
  document.documentElement.removeAttribute('data-theme');
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Markdown Mermaid diagrams', () => {
  it('renders fenced diagrams in order while preserving normal and inline code and math', async () => {
    const body = ['`mermaid`', '```mermaid\n' + source + '\n```', '```js\nconst x = 1;\n```', '$x^2$', '```mermaid\nsequenceDiagram\nA->>B: 你好\n```'].join('\n\n');
    const { container } = render(<MarkdownBody body={body} onNotify={notify} />);
    await waitFor(() => expect(screen.getAllByRole('img', { name: 'Mermaid 图表' })).toHaveLength(2));
    expect(renderDiagram.mock.calls.map(call => call[0])).toEqual([source, 'sequenceDiagram\nA->>B: 你好']);
    expect(container.querySelector('.code-card[aria-label="js 代码块"]')?.textContent).toContain('const x = 1;');
    expect(container.querySelector('p code')?.textContent).toBe('mermaid');
    expect(container.querySelector('.katex')).toBeTruthy();
    expect(container.querySelector('pre .mermaid-block')).toBeNull();
  });

  it('keeps streaming source without rendering, then renders the completed message', async () => {
    const body = `\`\`\`mermaid\n${source}\n\`\`\``;
    const { rerender } = render(<MarkdownBody body={body} streaming onNotify={notify} />);
    expect(screen.getByRole('status').textContent).toBe('图表生成中…');
    expect(renderDiagram).not.toHaveBeenCalled();
    rerender(<MarkdownBody body={body} onNotify={notify} />);
    await screen.findByRole('img', { name: 'Mermaid 图表' });
    expect(renderDiagram).toHaveBeenCalledTimes(1);
    // Unrelated prose changing must not remount / redraw a completed diagram.
    rerender(<MarkdownBody body={body + '\n\n后续文字'} onNotify={notify} />);
    expect(renderDiagram).toHaveBeenCalledTimes(1);
  });

  it('can show and copy exact source and open the existing image viewer', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { container } = render(<MermaidBlock source={source} streaming={false} onNotify={notify} />);
    await screen.findByRole('img', { name: 'Mermaid 图表' });
    fireEvent.click(screen.getByRole('button', { name: '查看源码' }));
    expect(container.querySelector('pre code')?.textContent).toBe(source);
    fireEvent.click(screen.getByRole('button', { name: '复制 Mermaid 源码' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(source));
    fireEvent.click(screen.getByRole('button', { name: '显示图表' }));
    fireEvent.click(screen.getByRole('button', { name: '放大图表' }));
    expect(screen.getByRole('dialog', { name: 'Lightbox' })).toBeTruthy();
  });

  it('retains invalid source and allows retry without poisoning another diagram', async () => {
    renderDiagram.mockRejectedValueOnce(new Error('syntax')).mockResolvedValue(diagram);
    const { container } = render(<MarkdownBody body={'```mermaid\nbroken\n```\n\n```mermaid\n' + source + '\n```'} onNotify={notify} />);
    await screen.findByText('暂时无法绘制，请查看源码。');
    expect(container.querySelector('pre code')?.textContent).toBe('broken');
    expect(screen.getAllByRole('img', { name: 'Mermaid 图表' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getAllByRole('img', { name: 'Mermaid 图表' })).toHaveLength(2));
    expect(notify).not.toHaveBeenCalled();
  });

  it('discards late results and releases image URLs on theme change and unmount', async () => {
    let finishOld!: (value: MermaidImage) => void;
    renderDiagram.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValueOnce('blob:current').mockReturnValueOnce('blob:dark');
    const revoke = vi.spyOn(URL, 'revokeObjectURL');
    const { rerender, unmount } = render(<MermaidBlock source="old" streaming={false} onNotify={notify} />);
    const oldSignal = renderDiagram.mock.calls[0][2];
    rerender(<MermaidBlock source={source} streaming={false} onNotify={notify} />);
    expect(oldSignal.aborted).toBe(true);
    await screen.findByRole('img', { name: 'Mermaid 图表' });
    await act(async () => { finishOld(diagram); });
    expect(create).toHaveBeenCalledTimes(1);
    act(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await waitFor(() => expect(renderDiagram).toHaveBeenLastCalledWith(source, true, expect.any(AbortSignal)));
    await waitFor(() => expect(screen.getByRole('img').getAttribute('src')).toBe('blob:dark'));
    expect(revoke).toHaveBeenCalledWith('blob:current');
    unmount();
    expect(revoke).toHaveBeenCalledWith('blob:dark');
  });
});
