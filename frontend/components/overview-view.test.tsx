import type { ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { OverviewView } from './overview-view';

afterEach(cleanup);

function renderOverview(overrides: Partial<ComponentProps<typeof OverviewView>> = {}) {
  return render(
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
      {...overrides}
    />,
  );
}

it('shows the original hero, brand bar, metrics, and empty recent sessions', () => {
  const { container } = renderOverview();

  expect(container.querySelector('.surface-topbar')).toBeTruthy();
  expect(screen.getByText('Pulsara')).toBeTruthy();
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('准备好继续航行了吗？');
  expect(container.querySelectorAll('.metric-grid article')).toHaveLength(4);
  expect(screen.getByRole('heading', { name: '最近会话' })).toBeTruthy();
  expect(screen.getByText('会话列表为空')).toBeTruthy();
});

it('opens task creation, workbench, and all sessions from the original actions', () => {
  const onNavigate = vi.fn();
  const onNewSession = vi.fn();
  renderOverview({ onNavigate, onNewSession });

  fireEvent.click(screen.getByRole('button', { name: '开始新任务' }));
  expect(onNewSession).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: '打开工作台' }));
  fireEvent.click(screen.getByRole('button', { name: '查看全部' }));
  expect(onNavigate.mock.calls).toEqual([['workbench'], ['workbench']]);
});

it('shows four recent sessions with shared presence and opens the selected session', () => {
  const onOpenSession = vi.fn();
  const sessions: ComponentProps<typeof OverviewView>['sessions'] = Array.from({ length: 5 }, (_, index) => ({
    id: `session-${index}`,
    title: `任务 ${index}`,
    subtitle: '/workspace/project',
    status: index === 0 ? 'running' : 'draft',
    updatedAt: '刚刚',
    live: index < 2,
  }));
  const { container } = renderOverview({ sessions, activeSessionId: 'session-0', onOpenSession });

  expect(container.querySelectorAll('.recent-table > button')).toHaveLength(4);
  expect(container.querySelectorAll('.recent-table .session-presence--current')).toHaveLength(1);
  expect(container.querySelectorAll('.recent-table .session-presence--loaded')).toHaveLength(1);
  expect(container.querySelectorAll('.recent-table .session-presence--resumable')).toHaveLength(2);
  expect(screen.queryByText('任务 4')).toBeNull();
  expect(screen.getByText('共 5 个会话')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /任务 2/ }));
  expect(onOpenSession).toHaveBeenCalledWith('session-2');
});

it('counts running tasks and shows model and retrieval configuration', () => {
  const { container } = renderOverview({
    agentTasks: (['running', 'completed', 'pending'] as const).map((status) => ({
      id: status,
      label: status,
      role: 'worker',
      objective: 'Test task',
      status,
      completionAccepted: status === 'completed',
      dependencyIds: [],
      color: 'amber',
    })),
    localSettings: {
      postgres: null,
      dashscope_credentials: { embedding_configured: true, rerank_configured: false },
    },
    modelConfigurations: (['ready', 'unavailable'] as const).map((status, index) => ({
      id: `model-${index}`,
      source: 'user_declared',
      route_id: 'custom',
      wire_api: 'openai_responses',
      model_id: 'test-model',
      base_url: 'https://example.com/v1',
      status,
      authentication: 'none',
      credential_configured: false,
      reasoning: { kind: 'provider_default' },
    })),
  });

  expect(container.querySelectorAll('.metric-grid article')[1]?.querySelector('strong')?.textContent).toBe('1');
  expect(screen.getByText('1 组可用 · 1 组不可用')).toBeTruthy();
  expect(screen.getByText('已配置')).toBeTruthy();
  expect(screen.getByText('未配置')).toBeTruthy();
});

it('directs an unconfigured database to setup instead of showing session data', () => {
  const onNavigate = vi.fn();
  const { container } = renderOverview({ databaseState: 'database_not_configured', canCreateSession: false, onNavigate });

  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('先准备好本地数据');
  expect(screen.getByRole('region', { name: 'PostgreSQL 配置引导' })).toBeTruthy();
  expect(container.querySelector('.metric-grid')).toBeNull();
  expect(screen.queryByRole('button', { name: '开始新任务' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '配置 PostgreSQL' }));
  expect(onNavigate).toHaveBeenCalledWith('settings');
});

it('disables task creation while offline and reports the connection state', () => {
  const onNewSession = vi.fn();
  renderOverview({ runtimeStatus: 'offline', canCreateSession: false, onNewSession });

  const createButton = screen.getByRole('button', { name: '开始新任务' });
  expect(createButton.hasAttribute('disabled')).toBe(true);
  fireEvent.click(createButton);
  expect(onNewSession).not.toHaveBeenCalled();
  expect(screen.getByText('连接已中断')).toBeTruthy();
  expect(screen.getByText('等待连接')).toBeTruthy();
});
