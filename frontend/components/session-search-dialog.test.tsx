import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SessionSearchDialog } from './session-search-dialog';
import type { RuntimeAdapter, SessionSearchPage } from '../lib/runtime-adapter';
import type { SessionSummary } from '../lib/pulsara-types';

const session = (id: string, lifecycle: 'OPEN' | 'ARCHIVED' = 'OPEN'): SessionSummary => ({
  id, title: `标题 ${id}`, lifecycle, subtitle: '', status: 'completed', updatedAt: '今天', live: false,
});
const page = (...sessions: SessionSummary[]): SessionSearchPage => ({ items: sessions.map(session => ({ session, matchKind: 'assistant', snippet: '包含中文 %_.* <script> 的中间解释' })), nextCursor: null });
function setup(search = vi.fn().mockResolvedValue(page(session('one')))) {
  const read = vi.fn(async (id: string) => session(id));
  const restore = vi.fn(async (id: string) => ({ session_id: id, status: 'OPEN' }));
  const adapter = { searchSessions: search, readSession: read, unarchiveSession: restore } as unknown as RuntimeAdapter;
  const onOpen = vi.fn(), onClose = vi.fn();
  const result = render(<SessionSearchDialog adapter={adapter} available onOpen={onOpen} onClose={onClose} />);
  return { ...result, search, read, restore, onOpen, onClose, input: screen.getByLabelText('搜索关键词') };
}
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('selects with arrows, prevents IME Enter and revalidates before opening', async () => {
  const { input, read, onOpen, onClose } = setup(vi.fn().mockResolvedValue(page(session('one'), session('two'))));
  expect(document.activeElement).toBe(input);
  await screen.findByRole('button', { name: /标题 two/ });
  expect(document.querySelector('.session-search-result.is-selected')).toBeNull();
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(read).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: 'Enter' });
  expect(read).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input); fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(onOpen).toHaveBeenCalledExactlyOnceWith(session('two')));
  expect(read).toHaveBeenCalledExactlyOnceWith('two'); expect(onClose).toHaveBeenCalledOnce();
});

it('requires explicit restore and reports deleted results without navigating', async () => {
  const { restore, read, onOpen } = setup(vi.fn().mockResolvedValue(page(session('archived', 'ARCHIVED'))));
  fireEvent.click(await screen.findByRole('button', { name: /标题 archived/ }));
  expect(restore).not.toHaveBeenCalled(); expect(read).not.toHaveBeenCalled();
  read.mockRejectedValueOnce(new Error('会话已删除'));
  fireEvent.click(screen.getByRole('button', { name: '取消归档并打开' }));
  await screen.findByRole('alert');
  expect(restore).toHaveBeenCalledExactlyOnceWith('archived');
  expect(onOpen).not.toHaveBeenCalled(); expect(screen.getByRole('alert').textContent).toContain('已删除');
});

it('ignores stale queries and aborts requests on unmount', async () => {
  let resolveOld!: (value: SessionSearchPage) => void;
  const search = vi.fn().mockImplementationOnce(() => new Promise<SessionSearchPage>(resolve => { resolveOld = resolve; }))
    .mockResolvedValue(page(session('new')));
  const { input, unmount } = setup(search);
  await waitFor(() => expect(search).toHaveBeenCalledOnce());
  const oldSignal = search.mock.calls[0][3] as AbortSignal;
  fireEvent.change(input, { target: { value: 'new' } });
  expect(oldSignal.aborted).toBe(true);
  await screen.findByRole('button', { name: /标题 new/ });
  await act(async () => resolveOld(page(session('old'))));
  expect(screen.queryByRole('button', { name: /标题 old/ })).toBeNull();
  const newSignal = search.mock.calls[1][3] as AbortSignal;
  unmount(); expect(newSignal.aborted).toBe(true);
});

it('appends pages once per session and retries errors without hiding existing results', async () => {
  const search = vi.fn().mockResolvedValueOnce({ ...page(session('one')), nextCursor: 'next' })
    .mockRejectedValueOnce(new Error('暂时失败')).mockResolvedValueOnce(page(session('one'), session('two')));
  const { input } = setup(search);
  fireEvent.change(input, { target: { value: 'needle' } });
  fireEvent.click(await screen.findByRole('button', { name: '加载更多' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: /标题 one/ })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await screen.findByRole('button', { name: /标题 two/ });
  expect(screen.getAllByRole('button', { name: /标题 one/ })).toHaveLength(1);
  expect(search.mock.calls[2].slice(0, 3)).toEqual(['needle', 'ALL', 'next']);
});

it('searches literal punctuation, highlights text safely and filters archived sessions', async () => {
  const { input, search } = setup();
  await screen.findByRole('button', { name: /标题 one/ });
  fireEvent.change(input, { target: { value: '%_.*' } });
  await waitFor(() => expect(document.querySelector('mark')?.textContent).toBe('%_.*'));
  expect(document.querySelector('script')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '会话搜索范围' }));
  fireEvent.click(screen.getByRole('menuitemradio', { name: '已归档' }));
  await waitFor(() => expect(search.mock.lastCall?.slice(0, 2)).toEqual(['%_.*', 'ARCHIVED']));
});

it('brings a late keyword into the compact preview with surrounding text and case-insensitive highlighting', async () => {
  const result = page(session('one'));
  result.items[0].snippet = '这段较长的开场说明不应占满摘要。'.repeat(5) + '查看 OpenAI DevDay 的直播安排和时间';
  const { input } = setup(vi.fn().mockResolvedValue(result));
  await screen.findByRole('button', { name: /标题 one/ });
  fireEvent.change(input, { target: { value: 'dev' } });
  await waitFor(() => expect(document.querySelector('.session-search-result p mark')?.textContent).toBe('Dev'));
  const preview = document.querySelector('.session-search-result p')!;
  expect(preview.textContent).toContain('OpenAI DevDay 的直播安排');
  expect(preview.textContent).not.toContain('这段较长的开场说明');
  expect(preview.textContent).toContain('…');
});


it('shows only five recent sessions without preselection, while keyword search can show more', async () => {
  const result = { ...page(...Array.from({ length: 7 }, (_, index) => session(String(index)))), nextCursor: 'next' };
  const { input } = setup(vi.fn().mockResolvedValue(result));
  await screen.findByRole('button', { name: /标题 4/ });
  expect(screen.getByText('最近会话')).toBeTruthy();
  expect(document.querySelectorAll('.session-search-result')).toHaveLength(5);
  expect(document.querySelector('.session-search-result.is-selected')).toBeNull();
  expect(screen.queryByRole('button', { name: '加载更多' })).toBeNull();
  fireEvent.change(input, { target: { value: 'needle' } });
  await screen.findByRole('button', { name: /标题 6/ });
  expect(screen.queryByText('最近会话')).toBeNull();
  expect(document.querySelector('.session-search-result.is-selected')).toBeNull();
  expect(screen.getByRole('button', { name: '加载更多' })).toBeTruthy();
});


it('supports scope-menu arrows, Escape and outside dismissal without closing search', async () => {
  const { input, search, onClose } = setup();
  await screen.findByRole('button', { name: /标题 one/ });
  const trigger = screen.getByRole('button', { name: '会话搜索范围' });
  fireEvent.click(trigger);
  expect(document.activeElement).toBe(screen.getByRole('menuitemradio', { name: '全部' }));
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  expect(document.activeElement).toBe(screen.getByRole('menuitemradio', { name: '活跃会话' }));
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  expect(screen.queryByRole('menu')).toBeNull();
  expect(document.activeElement).toBe(trigger); expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole('menuitemradio', { name: '活跃会话' }));
  await waitFor(() => expect(search.mock.lastCall?.slice(0, 2)).toEqual(['', 'OPEN']));
  expect(trigger.textContent).toContain('活跃会话');
  fireEvent.click(trigger); fireEvent.pointerDown(input);
  expect(screen.queryByRole('menu')).toBeNull(); expect(onClose).not.toHaveBeenCalled();
});


it('refreshes the recent five within each chosen scope', async () => {
  const search = vi.fn(async (_query: string, scope: string) => page(
    ...Array.from({ length: 5 }, (_, index) => session(`${scope}-${index}`, scope === 'ARCHIVED' ? 'ARCHIVED' : 'OPEN')),
  ));
  setup(search);
  await screen.findByRole('button', { name: /标题 ALL-4/ });
  for (const [label, scope] of [['活跃会话', 'OPEN'], ['已归档', 'ARCHIVED'], ['全部', 'ALL']]) {
    fireEvent.click(screen.getByRole('button', { name: '会话搜索范围' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: label }));
    await screen.findByRole('button', { name: new RegExp(`标题 ${scope}-4`) });
    expect(search.mock.lastCall?.slice(0, 2)).toEqual(['', scope]);
    expect(document.querySelectorAll('.session-search-result')).toHaveLength(5);
    expect([...document.querySelectorAll('.session-search-result')].every(row => row.textContent?.includes(`标题 ${scope}-`))).toBe(true);
    expect(document.querySelector('.session-search-result.is-selected')).toBeNull();
  }
});
