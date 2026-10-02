import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SessionRenameDialog } from '../../frontend/components/session-rename-dialog';
import type { SessionSummary } from '../../frontend/lib/pulsara-types';

const session = { id: 'one', title: '原始标题' } as SessionSummary;
afterEach(cleanup);

it('selects the current title, preserves input on failure and submits trimmed text once', async () => {
  let reject!: (error: Error) => void;
  const save = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  const close = vi.fn();
  render(<SessionRenameDialog session={session} onSave={save} onClose={close} />);
  const input = screen.getByLabelText('会话标题') as HTMLInputElement;
  expect(document.activeElement).toBe(input);
  expect(input.selectionStart).toBe(0); expect(input.selectionEnd).toBe(session.title.length);
  fireEvent.change(input, { target: { value: '  新标题  ' } });
  fireEvent.submit(input.closest('form')!); fireEvent.submit(input.closest('form')!);
  expect(save).toHaveBeenCalledExactlyOnceWith('新标题');
  fireEvent.keyDown(input, { key: 'Escape' }); expect(close).not.toHaveBeenCalled();
  await act(async () => { reject(new Error('保存未确认')); });
  expect((await screen.findByRole('alert')).textContent).toContain('保存未确认');
  expect(input.value).toBe('  新标题  ');
  fireEvent.keyDown(input, { key: 'Escape' }); expect(close).toHaveBeenCalledOnce();
});

it('does not submit during IME composition and closes after confirmed save', async () => {
  const save = vi.fn(async () => {}); const close = vi.fn();
  render(<SessionRenameDialog session={session} onSave={save} onClose={close} />);
  const input = screen.getByLabelText('会话标题');
  fireEvent.compositionStart(input); fireEvent.submit(input.closest('form')!);
  expect(save).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input); fireEvent.submit(input.closest('form')!);
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
});
