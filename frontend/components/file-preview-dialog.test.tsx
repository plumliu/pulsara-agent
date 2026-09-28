import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { FilePreviewProvider } from './file-preview-dialog';
import { MarkdownBody } from './markdown-body';
import type { FilePreview, FilePreviewApi } from '../lib/file-preview';
import { classifyFileLink, markdownImageUrl } from '../lib/file-preview';
import { htmlPreviewShell } from './sandboxed-html-preview';

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  HTMLElement.prototype.scrollTo = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const notify = vi.fn();
const file = (name: string): FilePreview => ({ read_token: name, name, path: `/project/${name}`, kind: 'text', size: 5, notice: null, content_url: `/api/file-previews/${name}/content`, document_url: null, images_url: null, can_open: false });
function api(): FilePreviewApi {
  return { open: vi.fn(async path => file(path)), close: vi.fn(async () => undefined), action: vi.fn(async () => undefined), page: vi.fn(async () => ({ mode: 'text' as const, text: 'hello', cursor: 0, next_cursor: null })) };
}
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }

it('classifies local links without granting scripts or foreign file hosts', () => {
  for (const value of ['a/b.csv', '/tmp/a.md', '~/notes.md', 'file:///tmp/a%20b.html']) expect(classifyFileLink(value)).toBe('local');
  for (const value of ['javascript:x', 'data:text/html,hi', 'sandbox:/a', 'file://evil/a', '%00', '%zz', 'a\\b']) expect(classifyFileLink(value)).toBe('invalid');
  expect(classifyFileLink('https://example.com/a')).toBe('external');
  expect(markdownImageUrl('../secret.png','/images/')).toBeUndefined();
  expect(markdownImageUrl('./plots/中文.png','/images/')).toBe('/images/plots/%E4%B8%AD%E6%96%87.png');
});

it('intercepts file links and renders the original text while keeping external links', async () => {
  const reader = api();
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[报告](report.txt) [官网](https://example.com) [坏地址](javascript:alert)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '报告' }), { ctrlKey: true });
  await screen.findByText('hello');
  expect(reader.open).toHaveBeenCalledWith('report.txt', undefined);
  expect(screen.getByRole('link', { name: '官网' }).getAttribute('target')).toBe('_blank');
  expect(screen.getByRole('link', { name: '坏地址' }).getAttribute('href')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '关闭预览' }));
  await waitFor(() => expect(reader.close).toHaveBeenCalledWith('report.txt'));
});

it('settles a stale open before the latest intent and binds close to its token', async () => {
  const reader = api();
  const first = deferred<FilePreview>();
  vi.mocked(reader.open).mockImplementationOnce(() => first.promise);
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[甲](a.txt) [乙](b.txt)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '甲' }));
  await waitFor(() => expect(reader.open).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('link', { name: '乙' }));
  expect(reader.open).toHaveBeenCalledTimes(1);
  await act(async () => first.resolve(file('a.txt')));
  await screen.findByText('hello');
  expect(reader.close).toHaveBeenCalledWith('a.txt');
  expect(reader.open).toHaveBeenLastCalledWith('b.txt', undefined);
  expect(screen.getByRole('dialog').textContent).toContain('b.txt');
});

it('switches owners without remounting conversation children or accepting old results', async () => {
  const a = api(), b = api(), pending = deferred<FilePreview>();
  vi.mocked(a.open).mockReturnValue(pending.promise);
  function Child() { return <input defaultValue="draft" aria-label="unchanged composer" />; }
  const body = <><Child /><MarkdownBody body="[报告](a.txt)" onNotify={notify} /></>;
  const view = render(<FilePreviewProvider api={a} ownerKey="one" onNotify={notify}>{body}</FilePreviewProvider>);
  const editor = screen.getByLabelText('unchanged composer');
  fireEvent.click(screen.getByRole('link', { name: '报告' }));
  await waitFor(() => expect(a.open).toHaveBeenCalled());
  view.rerender(<FilePreviewProvider api={b} ownerKey="two" onNotify={notify}>{body}</FilePreviewProvider>);
  await act(async () => pending.resolve(file('a.txt')));
  expect(screen.getByLabelText('unchanged composer')).toBe(editor);
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(a.close).toHaveBeenCalledWith('a.txt');
  expect(b.close).not.toHaveBeenCalled();
});

it('shares a restricted HTML shell without letting content break into its trusted script', () => {
  const shell = htmlPreviewShell({ html: '</script><script>parent.escaped=true</script>', measure: true });
  const parsed = new DOMParser().parseFromString(shell, 'text/html');
  expect(parsed.querySelectorAll('script')).toHaveLength(1);
  expect(parsed.querySelector('iframe')?.getAttribute('sandbox')).toBe('allow-scripts');
  expect(parsed.querySelector('meta')?.getAttribute('content')).toContain("frame-src 'none'");
  expect(() => htmlPreviewShell({ url: 'https://evil.example/a' })).toThrow();
});

it('switches Markdown source without losing the loaded page', async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file('report.md'), kind: 'markdown' });
  vi.mocked(reader.page).mockResolvedValue({ mode: 'text', text: '# Heading', cursor: 0, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[报告](report.md)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '报告' }));
  await screen.findByRole('heading', { name: 'Heading' });
  fireEvent.click(screen.getByRole('button', { name: '查看源码' }));
  await screen.findByText('# Heading');
  fireEvent.click(screen.getByRole('button', { name: '显示预览' }));
  await screen.findByRole('heading', { name: 'Heading' });
  expect(reader.page).toHaveBeenCalledTimes(1);
});

it('shows HTML source as text while preserving the preview frame', async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file('plot.html'), kind: 'html', document_url: '/api/file-previews/preview/resources/plot.html' });
  const html = '<h1>Raw heading</h1><script>window.injected=true</script>';
  vi.mocked(reader.page).mockResolvedValue({ mode: 'text', text: html, cursor: 0, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[图表](plot.html)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '图表' }));
  const toggle = await screen.findByRole('button', { name: '查看源码' });
  expect(toggle.closest('nav')?.getAttribute('aria-label')).toBe('文件操作');
  const frame = screen.getByTitle('plot.html');
  fireEvent.click(toggle);
  await screen.findByText(html);
  expect(screen.queryByRole('heading', { name: 'Raw heading' })).toBeNull();
  expect(frame.parentElement?.hidden).toBe(true);
  expect(screen.queryByLabelText('文件分页')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '显示预览' }));
  expect(screen.getByTitle('plot.html')).toBe(frame);
  expect(frame.parentElement?.hidden).toBe(false);
});

it('keeps CSV pagination available and restarts at row zero after a source toggle', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file('rows.csv'), kind: 'table' });
  vi.mocked(reader.page).mockImplementation(async (_, cursor, mode) => mode === 'table'
    ? { mode, rows: [[cursor === 0 ? 'name' : 'next record']], cursor, next_cursor: cursor === 0 ? 1 : null }
    : { mode, text: 'name\nnext record', cursor, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[表格](rows.csv)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '表格' }));
  await screen.findByRole('columnheader', { name: 'name' });
  fireEvent.click(screen.getByRole('button', { name: '下一页 →' }));
  await screen.findByRole('cell', { name: 'next record' });
  expect(screen.getByLabelText('文件分页').tagName).toBe('FOOTER');
  fireEvent.click(screen.getByRole('button', { name: '查看源码' }));
  await screen.findByText('name next record');
  expect(screen.queryByLabelText('文件分页')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '显示预览' }));
  await screen.findByRole('columnheader', { name: 'name' });
  expect(vi.mocked(reader.page).mock.calls.map(([, cursor, mode]) => [cursor, mode])).toEqual([[0, 'table'], [1, 'table'], [0, 'text'], [0, 'table']]);
});

it('retries a missing Markdown sibling using its original resolved target', async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValueOnce({ ...file('report.md'), path: '/project/reports/report.md', kind: 'markdown' }).mockRejectedValueOnce(new Error('文件不存在')).mockResolvedValueOnce(file('new.txt'));
  vi.mocked(reader.page).mockResolvedValueOnce({ mode: 'text', text: '[下一份](sub/new.txt)', cursor: 0, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[报告](report.md)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '报告' }));
  fireEvent.click(await screen.findByRole('link', { name: '下一份' }));
  await screen.findByText('文件不存在');
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await screen.findByText('hello');
  expect(reader.open).toHaveBeenLastCalledWith('file:///project/reports/sub/new.txt', undefined);
});

it('keeps the final columns reachable without an oversized scroll surface', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file('wide.csv'), kind: 'table' });
  const row = Array.from({ length: 200001 }, (_, index) => index === 199680 ? 'final group' : '');
  vi.mocked(reader.page).mockResolvedValue({ mode: 'table', rows: [row], cursor: 0, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body="[表格](wide.csv)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '表格' }));
  fireEvent.change(await screen.findByLabelText('列分组'), { target: { value: '196' } });
  await screen.findByText('final group');
  expect(screen.getByRole('table').querySelectorAll('[role=columnheader]').length).toBeLessThan(20);
  vi.unstubAllGlobals();
});

it('keeps the conversation mounted during consecutive table scroll events', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file('wide.csv'), kind: 'table' });
  const rows = Array.from({ length: 80 }, (_, r) => Array.from({ length: 40 }, (_, c) => `r${r}c${c}`));
  vi.mocked(reader.page).mockResolvedValue({ mode: 'table', rows, cursor: 0, next_cursor: null });
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><p>当前会话</p><MarkdownBody body="[表格](wide.csv)" onNotify={notify} /></FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '表格' }));
  const table = await screen.findByRole('table');
  // Scroll updates may be batched; React clears currentTarget after dispatch.
  act(() => {
    fireEvent.scroll(table, { target: { scrollLeft: 180, scrollTop: 34 } });
    fireEvent.scroll(table, { target: { scrollLeft: 1800, scrollTop: 340 } });
  });
  expect(await screen.findByText('r10c10')).toBeTruthy();
  expect(screen.queryByText('r0c0')).toBeNull();
  expect(screen.getByText('当前会话')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '关闭预览' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByText('当前会话')).toBeTruthy();
});
