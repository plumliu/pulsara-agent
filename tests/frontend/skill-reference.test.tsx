import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { closeHistory } from '@tiptap/pm/history';
import { afterEach, expect, it, vi } from 'vitest';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import { skillReference, splitSkillReferences } from '../../frontend/lib/skill-reference';
import { PromptComposer } from '../../frontend/components/prompt-composer';
import { PromptContentView } from '../../frontend/components/prompt-content-view';

const stores: PromptDraftStore[] = [];
const store = () => { const value = new PromptDraftStore(); stores.push(value); return value; };
afterEach(() => { cleanup(); stores.splice(0).forEach(value => value.destroy()); });

it('uses the existing dollar skill syntax and retains all surrounding text', () => {
  const text = '使用 $huggingface-best、$pdf。\n$HOME $bad_name a$pdf $Bad $bad--name';
  const parts = splitSkillReferences(text);
  expect(parts.filter(part => typeof part !== 'string')).toEqual([
    { name: 'huggingface-best', raw: '$huggingface-best' }, { name: 'pdf', raw: '$pdf' },
  ]);
  expect(parts.map(part => typeof part === 'string' ? part : part.raw).join('')).toBe(text);
  expect(skillReference('$hf-cli')).toEqual({ name: 'hf-cli', raw: '$hf-cli' });
  expect(skillReference('$hf-cli trailing')).toBeUndefined();
});

it('keeps exact Text/Image order, file paths, and dollar markers on capture and restore', async () => {
  const value = store();
  const file = '【本地文件（只读副本，12 bytes）："/tmp/$hf-cli.pdf"】';
  const content = { parts: [
    { type: 'text' as const, text: `  $hf-cli ${file}\n` },
    { type: 'image' as const, source: 'local' as const, bytes: new Uint8Array([1, 2]), declaredMediaType: 'image/png' },
    { type: 'text' as const, text: ' $huggingface-best  ' },
  ] };
  value.restoreIfEmpty('s', content);
  expect(value.getEditor('s').getJSON().content?.[0].content?.filter(node => node.type === 'skillReference')).toHaveLength(2);
  expect(value.summary('s').text).toBe(`  $hf-cli ${file}\n $huggingface-best  `);
  const snapshot = await value.capture('s');
  expect(snapshot.content).toEqual(content);
  value.restoreIfEmpty('queue-edit', snapshot.content);
  expect((await value.capture('queue-edit')).content).toEqual(content);
});

it('deletes and undoes a selected skill atom and pastes it through the actual editor handler', async () => {
  const value = store();
  render(<PromptComposer store={value} sessionId="s" disabled={false} placeholder="input" onSubmit={() => {}} onNotify={() => {}} />);
  const editor = value.getEditor('s');
  await act(async () => value.insertText('s', '$hf-cli ', true));
  expect(await screen.findByLabelText('技能：hf-cli')).toBeTruthy();
  const snapshot = await value.capture('s');
  await act(async () => {
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.commands.setNodeSelection(1); editor.commands.deleteSelection();
  });
  expect(screen.queryByLabelText('技能：hf-cli')).toBeNull();
  await act(async () => { editor.commands.undo(); });
  expect((await value.capture('s')).content).toEqual(snapshot.content);
  expect(editor.getText()).toBe('$hf-cli ');
  const html = editor.getHTML();
  await act(async () => { editor.commands.selectAll(); editor.commands.deleteSelection(); });
  fireEvent.paste(screen.getByLabelText('发送给 Pulsara'), { clipboardData: {
    files: [], items: [], types: ['text/html', 'text/plain'],
    getData: (type: string) => type === 'text/html' ? html : '$hf-cli ',
  } });
  expect(await screen.findByLabelText('技能：hf-cli')).toBeTruthy();
  expect((await value.capture('s')).content).toEqual(snapshot.content);
});

it.each(['message', 'queue'] as const)('shows the same skill card in %s and copies its original marker', variant => {
  const raw = '先用 $huggingface-best 再用 $pdf。';
  const view = render(<PromptContentView variant={variant} content={{ parts: [{ type: 'text', text: raw }] }} onReadImage={vi.fn()} />);
  expect(screen.getByLabelText('技能：huggingface-best').textContent).toBe('huggingface-best');
  expect(screen.getByLabelText('技能：pdf')).toBeTruthy();
  const body = view.container.querySelector('.prompt-content-body')!;
  const selection = window.getSelection()!;
  const range = document.createRange(); range.selectNodeContents(body);
  selection.removeAllRanges(); selection.addRange(range);
  const setData = vi.fn();
  fireEvent.copy(body, { clipboardData: { setData } });
  expect(setData).toHaveBeenCalledWith('text/plain', raw);
  selection.removeAllRanges();
});
