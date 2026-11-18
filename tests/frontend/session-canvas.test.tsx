import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SessionCanvas, SessionCanvasTitleActions, SessionCanvasTitleIdentity, useSessionCanvas, type SessionCanvasPlacement,
} from '../../frontend/components/session-canvas';
import {
  applyDockResize, dockDividers, emptyLayout, fillFreeDock, focusWindow, guideDockResize, minimizeWindow, placeWindow,
  recallWindow, releaseDockSide, reorderTabs, resizeDockLine,
} from '../../frontend/components/session-canvas-layout';

const view = { w: 1200, h: 800 };
const full = { type: 'maximized' } as const;
const left = { x: 0, y: 0, w: .5, h: 1 };
const right = { x: .5, y: 0, w: .5, h: 1 };
const topLeft = { x: 0, y: 0, w: .5, h: .5 };
const bottomLeft = { x: 0, y: .5, w: .5, h: .5 };
const topRight = { x: .5, y: 0, w: .5, h: .5 };
const bottomRight = { x: .5, y: .5, w: .5, h: .5 };
const floating = { type: 'floating', rect: { x: 700, y: 200, w: 480, h: 420 } } as const;
const docked = (rect: typeof left): SessionCanvasPlacement => ({ type: 'docked', rect });

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('session canvas layout', () => {
  it('uses admission order after dock conflicts and keeps it when focusing or reordering', () => {
    let layout = placeWindow(emptyLayout, 'a', view, docked(topLeft));
    layout = placeWindow(layout, 'b', view, docked(bottomLeft));
    layout = placeWindow(layout, 'c', view, docked(topRight));
    layout = placeWindow(layout, 'd', view, docked(bottomRight));
    layout = focusWindow(layout, 'a');
    layout = reorderTabs(layout, 'd', 0);
    expect(layout.tabs).toEqual(['d', 'a', 'b', 'c']);
    expect(layout.admitted).toEqual(['a', 'b', 'c', 'd']);
    expect(layout.focusedId).toBe('a');

    const floatingAdmission = placeWindow(layout, 'e', view, floating);
    expect(floatingAdmission.admitted).toEqual(['b', 'c', 'd', 'e']);
    const dockedAdmission = placeWindow(layout, 'e', view, docked(left));
    expect(dockedAdmission.admitted).toEqual(['c', 'd', 'e']);
    expect(dockedAdmission.windows.get('a')).toMatchObject({ mode: 'minimized', dock: topLeft });
    expect(dockedAdmission.windows.get('b')).toMatchObject({ mode: 'minimized', dock: bottomLeft });

    const recalled = recallWindow(dockedAdmission, 'a', view);
    expect(recalled.windows.get('a')?.mode).toBe('floating');
    expect(recalled.windows.get('e')?.dock).toEqual(left);
  });

  it('restores the maximized window and recalls only the chosen tab', () => {
    let layout = placeWindow(emptyLayout, 'a', view, floating);
    layout = placeWindow(layout, 'b', view, docked(right));
    layout = placeWindow(layout, 'c', view, full);
    expect(layout.admitted).toEqual(['c']);
    const recalled = recallWindow(layout, 'b', view);
    expect(recalled.admitted).toEqual(['c', 'b']);
    expect(recalled.windows.get('c')?.mode).toBe('floating');
    expect(recalled.windows.get('b')?.dock).toEqual(right);
    expect(recalled.windows.get('a')?.mode).toBe('minimized');
    expect(minimizeWindow(recalled, 'b').focusedId).toBe('c');
    expect(minimizeWindow(layout, 'c').focusedId).toBeNull();
  });

  it('links the full-height divider, retains resized proportions and expands across a collapsed side', () => {
    let layout = placeWindow(emptyLayout, 'a', view, docked(left));
    layout = placeWindow(layout, 'b', view, docked(topRight));
    layout = placeWindow(layout, 'c', view, docked(bottomRight));
    const divider = dockDividers(layout).find(item => item.participants.length === 3)!;
    expect(divider.participants.map(part => part.id)).toEqual(['a', 'b', 'c']);
    const { low, high } = guideDockResize(layout, divider, view);
    expect(low).toBeCloseTo(260 / view.w);
    expect(high).toBeCloseTo(1 - 260 / view.w);
    const resized = applyDockResize(layout, resizeDockLine(divider, .375));
    expect(resized.windows.get('a')?.dock).toEqual({ ...left, w: .375 });
    expect(resized.windows.get('b')?.dock).toEqual({ x: .375, y: 0, w: .625, h: .5 });
    expect(resized.windows.get('c')?.dock).toEqual({ x: .375, y: .5, w: .625, h: .5 });

    const onLeft = releaseDockSide(layout, divider, true, view);
    expect(onLeft.admitted).toEqual(['b', 'c']);
    expect(onLeft.windows.get('a')).toMatchObject({ mode: 'minimized', dock: left });
    expect(onLeft.windows.get('b')?.dock).toEqual({ x: 0, y: 0, w: 1, h: .5 });
    expect(onLeft.windows.get('c')?.dock).toEqual({ x: 0, y: .5, w: 1, h: .5 });
    const onRight = releaseDockSide(layout, divider, false, view);
    expect(onRight.admitted).toEqual(['a']);
    expect(onRight.windows.get('a')?.mode).toBe('maximized');
  });

  it('fills the nearest open half and then the largest usable gap', () => {
    let layout = placeWindow(emptyLayout, 'a', view, docked(topLeft));
    layout = placeWindow(layout, 'b', view, floating);
    expect(fillFreeDock(layout, 'b', view).windows.get('b')?.dock).toEqual(right);
    layout = placeWindow(layout, 'c', view, docked(bottomLeft));
    layout = placeWindow(layout, 'd', view, docked(topRight));
    const gap = fillFreeDock(layout, 'b', view);
    expect(gap.windows.get('b')?.dock).toEqual(bottomRight);
    expect(gap.admitted).toEqual(layout.admitted);
    expect(fillFreeDock(layout, 'b', { w: 400, h: 800 })).toBe(layout);
  });
});

function Conversation({ title }: { title: string }) {
  const [draft, setDraft] = useState('');
  return <>
    <SessionCanvasTitleIdentity><span>{title} 状态</span></SessionCanvasTitleIdentity>
    <SessionCanvasTitleActions><button type="button">{title} 工作区</button></SessionCanvasTitleActions>
    <input aria-label={`${title} 草稿`} value={draft} onChange={event => setDraft(event.target.value)} />
  </>;
}

function CanvasHarness({ load }: { load?: (id: string, placement: SessionCanvasPlacement) => Promise<boolean> }) {
  const canvas = useSessionCanvas();
  const [visible, setVisible] = useState(true);
  const [tabHost, setTabHost] = useState<HTMLDivElement | null>(null);
  const sessions = ['甲', '乙', '丙'].map(title => ({ id: title, title, content: <Conversation title={title} />, pendingApproval: title === '乙' }));
  return <>
    <header className="desktop-titlebar"><div ref={setTabHost} data-testid="titlebar-host" /></header>
    {sessions.map(session => <button key={session.id} type="button" data-testid={`open-${session.id}`}
      onPointerDown={event => canvas.beginSidebarDrag(event, session)}
      onClick={() => canvas.place(session.id)}>{session.title}</button>)}
    <button type="button" onClick={() => setVisible(shown => !shown)}>切换页面</button>
    <SessionCanvas controller={canvas} sessions={sessions} visible={visible} tabHost={tabHost}
      onCloseSession={id => canvas.close(id)}
      onOpenSession={async (id, placement) => {
        if (await load?.(id, placement)) canvas.place(id, placement);
      }} />
  </>;
}

describe('session canvas windows', () => {
  it('retains the mounted form, title actions and approval signal through minimize and navigation', () => {
    render(<CanvasHarness />);
    fireEvent.click(screen.getByTestId('open-甲'));
    const first = screen.getByRole('region', { name: '甲' });
    const draft = within(first).getByRole('textbox', { name: '甲 草稿' }) as HTMLInputElement;
    fireEvent.change(draft, { target: { value: '继续写这段草稿' } });
    expect(within(first).getByText('甲 状态')).toBeTruthy();
    expect(within(first).getByRole('button', { name: '甲 工作区' })).toBeTruthy();

    fireEvent.click(screen.getByTestId('open-乙'));
    expect(first.hidden).toBe(true);
    expect(within(screen.getByRole('tab', { name: /乙/ })).getByLabelText('等待审批')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '甲' }));
    expect(first.hidden).toBe(false);
    expect(screen.getByRole('textbox', { name: '甲 草稿' })).toBe(draft);
    expect(draft.value).toBe('继续写这段草稿');
    expect(first.getAttribute('data-mode')).toBe('floating');

    fireEvent.click(within(first).getByRole('button', { name: '收起会话窗口' }));
    expect(first.hidden).toBe(true);
    expect(screen.getByRole('tab', { name: /乙/ }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: '切换页面' }));
    expect(screen.queryByRole('tab', { name: '甲' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '切换页面' }));
    fireEvent.click(screen.getByRole('tab', { name: '甲' }));
    expect(screen.getByRole('textbox', { name: '甲 草稿' })).toBe(draft);
  });

  it('defers a dropped session until loading succeeds and preserves the previous layout and focus on failure', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('session-canvas__surface')) return new DOMRect(225, 70, 1000, 680);
      if (this.classList.contains('session-canvas__tab-strip')) return new DOMRect(225, 35, 1000, 35);
      return new DOMRect(10, 115, 175, 35);
    });
    let settle!: (loaded: boolean) => void;
    const load = vi.fn(() => new Promise<boolean>(resolve => { settle = resolve; }));
    const { container } = render(<CanvasHarness load={load} />);
    fireEvent.click(screen.getByTestId('open-甲'));
    const first = screen.getByRole('region', { name: '甲' });
    const draft = within(first).getByRole('textbox');
    draft.focus();
    const other = screen.getByTestId('open-乙');
    const moveIntoCanvas = async () => {
      fireEvent.pointerDown(other, { button: 0, clientX: 80, clientY: 130, pointerId: 1 });
      fireEvent.pointerMove(document, { clientX: 1210, clientY: 300, pointerId: 1 });
      await act(async () => { await new Promise(requestAnimationFrame); });
      expect(container.querySelector('.session-canvas__preview[data-snap]')).toBeTruthy();
    };
    const release = () => fireEvent.pointerUp(document, { clientX: 1210, clientY: 300, pointerId: 1 });
    await moveIntoCanvas();
    fireEvent.keyDown(document, { key: 'Escape' });
    release();
    expect(load).not.toHaveBeenCalled();
    expect(first.getAttribute('data-mode')).toBe('maximized');
    expect(document.activeElement).toBe(draft);

    await moveIntoCanvas();
    release();
    expect(load).toHaveBeenCalledWith('乙', docked(right));
    expect(first.getAttribute('data-mode')).toBe('maximized');
    expect(screen.queryByRole('tab', { name: /乙/ })).toBeNull();
    await act(async () => { settle(false); });
    expect(document.activeElement).toBe(draft);

    await moveIntoCanvas();
    release();
    await act(async () => { settle(true); });
    expect(first.getAttribute('data-mode')).toBe('floating');
    expect(screen.getByRole('region', { name: '乙' }).getAttribute('data-mode')).toBe('docked');
  });

  it('uses the hosted titlebar for tab insertion and keeps the canvas free of the old row', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('session-canvas__surface')) return new DOMRect(225, 40, 1000, 710);
      if (this.classList.contains('session-canvas__tab-strip')) return new DOMRect(225, 0, 1000, 40);
      if (this.classList.contains('session-canvas__tab')) return new DOMRect(260, 5, 160, 30);
      return new DOMRect(10, 115, 175, 35);
    });
    const load = vi.fn(async () => true);
    const { container } = render(<CanvasHarness load={load} />);
    const host = screen.getByTestId('titlebar-host');
    vi.spyOn(document, 'elementFromPoint').mockReturnValue(host);
    fireEvent.click(screen.getByTestId('open-甲'));
    expect(within(host).getByRole('tab', { name: '甲' })).toBeTruthy();
    expect(container.querySelector('.session-canvas > .session-canvas__tabs')).toBeNull();

    fireEvent.pointerDown(screen.getByTestId('open-乙'), { button: 0, clientX: 80, clientY: 130, pointerId: 1 });
    fireEvent.pointerMove(document, { clientX: 240, clientY: 20, pointerId: 1 });
    await act(async () => { await new Promise(requestAnimationFrame); });
    expect(host.querySelector('.session-canvas__insertion')).toBeTruthy();
    await act(async () => { fireEvent.pointerUp(document, { clientX: 240, clientY: 20, pointerId: 1 }); });
    expect(load).toHaveBeenCalledWith('乙', { type: 'maximized', tabIndex: 0 });
    expect(within(host).getAllByRole('tab').map(tab => tab.getAttribute('title'))).toEqual(['乙', '甲']);
    const second = screen.getByRole('region', { name: '乙' });
    expect(second.getAttribute('data-mode')).toBe('maximized');

    fireEvent.pointerDown(within(host).getByRole('tab', { name: /乙/ }), { button: 0, clientX: 280, clientY: 20, pointerId: 2 });
    fireEvent.pointerMove(document, { clientX: 1210, clientY: 300, pointerId: 2 });
    await act(async () => { await new Promise(requestAnimationFrame); });
    expect(container.querySelector('.session-canvas__preview[data-snap]')).toBeTruthy();
    fireEvent.pointerUp(document, { clientX: 1210, clientY: 300, pointerId: 2 });
    expect(screen.getByRole('region', { name: '乙' })).toBe(second);
    expect(second.getAttribute('data-mode')).toBe('docked');
    expect(load).toHaveBeenCalledTimes(1);

    const title = within(second).getByText('乙 状态');
    fireEvent.doubleClick(within(second).getByRole('button', { name: '乙 工作区' }));
    expect(second.getAttribute('data-mode')).toBe('docked');
    fireEvent.doubleClick(title);
    expect(second.getAttribute('data-mode')).toBe('maximized');
    fireEvent.pointerDown(title, { button: 0, clientX: 400, clientY: 60, pointerId: 3 });
    fireEvent.pointerMove(document, { clientX: 525, clientY: 250, pointerId: 3 });
    await act(async () => { await new Promise(requestAnimationFrame); });
    fireEvent.pointerUp(document, { clientX: 525, clientY: 250, pointerId: 3 });
    expect(second.getAttribute('data-mode')).toBe('floating');

    fireEvent.click(screen.getByRole('button', { name: '切换页面' }));
    expect(within(host).queryByRole('tablist')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '切换页面' }));
    expect(within(host).getAllByRole('tab').map(tab => tab.getAttribute('title'))).toEqual(['乙', '甲']);
  });
});
