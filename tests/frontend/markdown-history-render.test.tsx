import {act, screen} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import {FakeAdapter, projection, renderWorkbench, typeComposer} from './pulsara-app-fixture';

const parsing = vi.hoisted(() => ({calls: 0}));
vi.mock('../../frontend/lib/markdown-source', async original => {
  const module = await original<typeof import('../../frontend/lib/markdown-source')>();
  return {...module, rehypeSourceMapping: (...args: Parameters<typeof module.rehypeSourceMapping>) => {
    parsing.calls += 1;
    return module.rehypeSourceMapping(...args);
  }};
});

it('does not reparse unchanged history on draft input, while real response updates still render', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = {...projection(), activeTurnId: undefined, isRunning: false,
    messages: [0, 1].map(index => ({
      id: `assistant-${index}`, turnId: `turn-${index}`, role: 'assistant' as const,
      time: '现在', body: `已保存回复 ${index}`, assistantKind: 'terminal' as const,
      rootFinal: true, status: 'completed' as const, visualizations: [],
    }))};
  renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByText('已保存回复 1');
  expect(parsing.calls).toBeGreaterThanOrEqual(2); // Real Markdown/source pipeline remains active.
  parsing.calls = 0;
  await typeComposer('未发送草稿');
  expect(parsing.calls).toBe(0);
  expect(adapter.lastConnection!.submitPrompt).not.toHaveBeenCalled();

  const current = adapter.lastConnection!.current();
  await act(async () => adapter.lastConnection!.emit({...current, eventSequence:current.eventSequence + 1,
    messages:current.messages.map((message, index) => index === 1 ? {...message,body:'真正更新的回复'} : message)}));
  expect(await screen.findByText('真正更新的回复')).toBeTruthy();
  expect(parsing.calls).toBe(1);
  expect(screen.getByRole('textbox', {name:'发送给 Pulsara'}).textContent).toBe('未发送草稿');
});
