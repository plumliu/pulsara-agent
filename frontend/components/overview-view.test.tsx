import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { OverviewView } from './overview-view';

afterEach(cleanup);

it('keeps the overview heading and actions without the marked captions', () => {
  render(
    <OverviewView
      sessions={[]}
      activeSessionId=""
      runtimeStatus="online"
      agentTasks={[]}
      modelConfigurations={[]}
      onNavigate={() => {}}
      onOpenSession={() => {}}
      onNewSession={() => {}}
      canCreateSession
    />,
  );

  expect(screen.getByText('Pulsara')).toBeTruthy();
  expect(screen.getByRole('button', { name: '打开工作台' })).toBeTruthy();
  expect(screen.queryByText('Pulsara 工作台')).toBeNull();
  expect(screen.queryByText('你的本地智能工作台')).toBeNull();
  expect(screen.queryByText('私密 · 仅限本机')).toBeNull();
});
