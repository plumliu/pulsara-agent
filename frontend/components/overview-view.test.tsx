import type { ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
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

it('uses the shared page heading without a separate brand bar or large logo', () => {
  const { container } = renderOverview();

  expect(screen.getByText('工作空间')).toBeTruthy();
  expect(screen.getByRole('button', { name: '打开工作台' })).toBeTruthy();
  expect(screen.queryByText('Pulsara 工作台')).toBeNull();
  expect(screen.queryByText('你的本地智能工作台')).toBeNull();
  expect(screen.queryByText('私密 · 仅限本机')).toBeNull();
  expect(container.querySelector('.surface-topbar')).toBeNull();
  expect(container.querySelector('.overview-content img')).toBeNull();
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('工作总览');
  expect(screen.getByRole('heading', { level: 1 }).closest('.page-header')).toBeTruthy();
  expect(screen.getByRole('region', { name: '运行环境' })).toBeTruthy();
  expect(container.querySelector('.overview-metrics')).toBeNull();
});

it('keeps task and workbench actions and provides a settings shortcut', () => {
  const onNavigate = vi.fn();
  const onNewSession = vi.fn();
  renderOverview({ onNavigate, onNewSession });

  fireEvent.click(screen.getByRole('button', { name: '开始新任务' }));
  expect(onNewSession).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: '打开工作台' }));
  fireEvent.click(screen.getByRole('button', { name: '查看全部' }));
  fireEvent.click(screen.getByRole('button', { name: '管理配置' }));
  fireEvent.click(screen.getByRole('button', { name: /记忆库/ }));
  fireEvent.click(screen.getByRole('button', { name: /能力中心/ }));
  expect(onNavigate.mock.calls).toEqual([['workbench'], ['workbench'], ['settings'], ['memory'], ['capabilities']]);
  expect(screen.getByText('还没有会话')).toBeTruthy();
});

it('keeps recent session navigation, presence, and the four-row preview', () => {
  const onOpenSession = vi.fn();
  const sessions: ComponentProps<typeof OverviewView>['sessions'] = Array.from({ length: 5 }, (_, index) => ({
    id: `session-${index}`,
    title: `任务 ${index}`,
    subtitle: '/workspace/project',
    status: index === 0 ? 'running' : 'draft',
    updatedAt: '刚刚',
    live: index < 2,
  }));
  renderOverview({ sessions, activeSessionId: 'session-0', onOpenSession });

  expect(screen.getByText('当前会话')).toBeTruthy();
  expect(screen.getByText('已载入')).toBeTruthy();
  expect(screen.getAllByText('可恢复')).toHaveLength(2);
  expect(screen.queryByText('任务 4')).toBeNull();
  expect(screen.queryByText('还没有会话')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /任务 2/ }));
  expect(onOpenSession).toHaveBeenCalledWith('session-2');
  const metrics = within(screen.getByRole('region', { name: '运行概况' }));
  expect(screen.getByLabelText('共 5 个会话').textContent).toBe('5');
  expect(metrics.getByText('1')).toBeTruthy();
});

it('keeps running task counts scoped to the current session instead of all sessions', () => {
  renderOverview({
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
  });

  const activity = within(screen.getByRole('region', { name: '运行概况' }));
  expect(activity.getByText('当前会话运行中任务')).toBeTruthy();
  expect(activity.getByText('1')).toBeTruthy();
  expect(activity.getByText('0')).toBeTruthy();
});

it('distinguishes available models and configured retrieval services', () => {
  renderOverview({
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

  expect(screen.getByText('1 组可用')).toBeTruthy();
  expect(screen.getByText('1 组不可用')).toBeTruthy();
  expect(screen.getByText('已配置')).toBeTruthy();
  expect(screen.getByText('未配置')).toBeTruthy();
});

it('still directs an unconfigured database to setup instead of showing ready data', () => {
  const onNavigate = vi.fn();
  renderOverview({ databaseState: 'database_not_configured', canCreateSession: false, onNavigate });

  expect(screen.getByRole('region', { name: 'PostgreSQL 配置引导' })).toBeTruthy();
  expect(screen.queryByRole('region', { name: '运行概况' })).toBeNull();
  expect(screen.queryByRole('button', { name: '开始新任务' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '配置 PostgreSQL' }));
  expect(onNavigate).toHaveBeenCalledWith('settings');
});

it('disables task creation while offline and does not report local data as ready', () => {
  const onNewSession = vi.fn();
  renderOverview({ runtimeStatus: 'offline', canCreateSession: false, onNewSession });

  const createButton = screen.getByRole('button', { name: '开始新任务' });
  expect(createButton.hasAttribute('disabled')).toBe(true);
  fireEvent.click(createButton);
  expect(onNewSession).not.toHaveBeenCalled();
  expect(screen.getByText('连接已中断')).toBeTruthy();
  expect(screen.getByText('等待连接')).toBeTruthy();
  expect(screen.queryByText('就绪')).toBeNull();
});
