import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BackgroundProcess } from '../lib/runtime-adapter';
import type { CapabilitySnapshot } from '../lib/pulsara-types';
import { InspectorPanel } from './inspector-panel';

afterEach(cleanup);

const backgroundProcess: BackgroundProcess = {
  processId: 'process-one',
  command: 'sleep 30',
  cwd: '/tmp/pr03',
  status: 'running',
  physicalState: 'RUNNING',
  ioMode: 'pipe',
  streamId: 'stream-one',
  outputCursor: '0',
  retainedFromCursor: '0',
  durationSeconds: 1,
  timedOut: false,
  originTurnId: 'turn-root',
};

const capabilitySnapshot: CapabilitySnapshot = {
  sessionId: 'session-one',
  workspacePath: '/tmp/pr03',
  workspaceKind: 'quick',
  credentialScopeKey: 'workspace-one',
  adoption: { scope: 'workspace', pending: false, when: 'next-provider-dispatch' },
  skills: {
    status: 'ready',
    configPath: '/tmp/pr03/.pulsara/skills.yaml',
    items: [
      {
        id: 'workspace-skill',
        name: '工作区技能',
        description: '工作区能力',
        location: '/tmp/pr03/.agents/skills/workspace-skill/SKILL.md',
        path: '/tmp/pr03/.agents/skills/workspace-skill/SKILL.md',
        source: 'workspace',
        editable: true,
        enabled: true,
        effective: true,
        configured: true,
        authoringNotes: [],
      },
      {
        id: 'user-skill',
        name: '用户技能',
        description: '继承的用户能力',
        location: '/Users/test/.agents/skills/user-skill/SKILL.md',
        path: '/Users/test/.agents/skills/user-skill/SKILL.md',
        source: 'user',
        editable: false,
        enabled: true,
        effective: true,
        configured: false,
        authoringNotes: [],
      },
    ],
    issues: [],
    details: [],
    roots: [],
  },
  mcp: {
    configPath: '/tmp/pr03/.pulsara/mcp.yaml',
    servers: [
      {
        id: 'workspace-mcp',
        name: '工作区 MCP',
        source: 'workspace',
        editable: true,
        enabled: true,
        configuredEnabled: true,
        needsApproval: false,
        effective: true,
        status: 'ready',
        required: false,
        availableToSubagents: false,
        toolCount: 0,
        discoveredToolCount: 0,
        resourceCount: 0,
        resourceTemplateCount: 0,
        promptCount: 0,
        instructions: '',
        hasFailure: false,
        tools: [],
      },
      {
        id: 'user-mcp',
        name: '用户 MCP',
        source: 'user',
        editable: false,
        enabled: true,
        configuredEnabled: true,
        needsApproval: false,
        effective: true,
        status: 'ready',
        required: false,
        availableToSubagents: false,
        toolCount: 0,
        discoveredToolCount: 0,
        resourceCount: 0,
        resourceTemplateCount: 0,
        promptCount: 0,
        instructions: '',
        hasFailure: false,
        tools: [],
      },
    ],
    collisions: [],
  },
};

function props(overrides: Partial<ComponentProps<typeof InspectorPanel>> = {}): ComponentProps<typeof InspectorPanel> {
  return {
    projectMcpForms: {
      preview: vi.fn(async () => []),
      import: vi.fn(async () => true),
      test: vi.fn(async () => ({ status: 'ready' as const, tools: 0, resources: 0, resource_templates: 0, prompts: 0 })),
      authorize: vi.fn(async () => undefined),
    },
    session: { id: 'session-one', title: 'PR03 会话', subtitle: '', status: 'running', updatedAt: '刚刚', live: true },
    isOpen: true,
    agentTasks: [],
    loading: false,
    canControl: false,
    capabilities: undefined,
    capabilityLoading: true,
    capabilityError: undefined,
    capabilityBusy: undefined,
    error: undefined,
    onRetry: vi.fn(),
    taskActivities: new Map(),
    onLoadTaskActivities: vi.fn(async () => ({ activities: [] })),
    taskArtifactOwnerKey: 'session-one:generation-one',
    onReadToolArtifact: vi.fn(),
    onCancelTask: vi.fn(async () => undefined),
    backgroundOwnerKey: 'session-one:host-one',
    backgroundHostSessionId: 'host-one',
    backgroundControlAdmissionDeadlineMs: 4_200,
    onLoadBackgroundProcesses: vi.fn(async () => ({ processes: [backgroundProcess] })),
    onReadBackgroundProcessLog: vi.fn(async () => ({
      process: backgroundProcess,
      output: '',
      outputCursor: '0',
      retainedFromCursor: '0',
      gapBeforeOutput: false,
      truncatedByResponseBound: false,
      sourceCoverage: 'COMPLETE',
    })),
    onTerminateBackgroundProcess: vi.fn(),
    onQueryControl: vi.fn(async () => ({ status: 'RESULT_UNAVAILABLE' as const })),
    onRetryCapabilities: vi.fn(),
    onToggleProjectSkill: vi.fn(async () => undefined),
    onPreviewProjectSkills: vi.fn(async () => []),
    onInstallProjectSkill: vi.fn(async () => undefined),
    onRemoveProjectSkill: vi.fn(async () => undefined),
    onCreateProjectMcp: vi.fn(async () => undefined),
    onEditProjectMcp: vi.fn(async () => undefined),
    onToggleProjectMcp: vi.fn(async () => undefined),
    onRemoveProjectMcp: vi.fn(async () => undefined),
    onReconnectProjectMcp: vi.fn(async () => undefined),
    onOpenUserCapabilities: vi.fn(),
    onNotify: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
}

describe('InspectorPanel PR03 production navigation', () => {
  it('keeps an unselected session out of data loading and loads after selection', async () => {
    const loadBackground = vi.fn(async () => ({ processes: [backgroundProcess] }));
    const emptyProps = props({
      session: { id: '', title: '尚未选择会话', subtitle: '', status: 'draft', updatedAt: '', live: false },
      onLoadBackgroundProcesses: loadBackground,
      capabilityLoading: false,
    });
    const panel = render(<InspectorPanel {...emptyProps} />);
    expect(screen.getByText('创建或选择会话后查看项目能力')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '添加' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    expect(screen.getByText('创建或选择会话后查看任务')).toBeTruthy();
    expect(screen.queryByText('这个会话还没有子任务')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '后台终端' }));
    expect(screen.getByText('创建或选择会话后查看后台终端')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
    expect(loadBackground).not.toHaveBeenCalled();

    panel.rerender(<InspectorPanel {...props({ onLoadBackgroundProcesses: loadBackground })} />);
    expect(await screen.findByText('sleep 30')).toBeTruthy();
    expect(loadBackground).toHaveBeenCalledWith(undefined);

    panel.rerender(<InspectorPanel {...emptyProps} />);
    expect(screen.getByText('创建或选择会话后查看后台终端')).toBeTruthy();
    expect(screen.queryByText('sleep 30')).toBeNull();
  });

  it('keeps capability access and adds task/background tabs in the frozen order', async () => {
    const loadBackground = vi.fn(async () => ({ processes: [backgroundProcess] }));
    render(<InspectorPanel {...props({ onLoadBackgroundProcesses: loadBackground })} />);

    const navigation = screen.getByRole('navigation', { name: '详情视图' });
    expect(within(navigation).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual([
      '能力', '任务', '后台终端',
    ]);
    expect(within(navigation).getByRole('button', { name: '项目能力' }).classList.contains('is-active')).toBe(true);
    expect(screen.getByText('正在读取项目能力')).toBeTruthy();

    fireEvent.click(within(navigation).getByRole('button', { name: '任务' }));
    expect(screen.getByText('这个会话还没有子任务')).toBeTruthy();

    fireEvent.click(within(navigation).getByRole('button', { name: '后台终端' }));
    await waitFor(() => expect(loadBackground).toHaveBeenCalledWith(undefined));
    expect(await screen.findByText('sleep 30')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '终止 sleep 30' })).toBeNull();

    fireEvent.click(within(navigation).getByRole('button', { name: '项目能力' }));
    expect(await screen.findByText('正在读取项目能力')).toBeTruthy();
  });

  it('keeps capability tabs on the left, groups MCP actions on the right, and shows effective totals', () => {
    render(<InspectorPanel {...props({ capabilities: capabilitySnapshot, capabilityLoading: false })} />);

    const tabs = screen.getByRole('tablist', { name: '能力类型' });
    expect(within(tabs).getByRole('tab', { name: '技能 2' })).toBeTruthy();
    expect(within(tabs).getByRole('tab', { name: 'MCP 2' })).toBeTruthy();

    const actions = screen.getByRole('group', { name: '能力操作' });
    expect(Array.from(tabs.parentElement!.children)).toEqual([tabs, actions]);
    expect(within(actions).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual(['添加']);

    fireEvent.click(within(tabs).getByRole('tab', { name: 'MCP 2' }));

    expect(screen.getByRole('tablist', { name: '能力类型' })).toBe(tabs);
    expect(within(actions).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual(['导入 MCP', '添加']);
    expect(within(actions).getAllByRole('button')[0].className).toBe(within(actions).getAllByRole('button')[1].className);
  });

  it('animates inherited capabilities and nested MCP details without unmounting on collapse', () => {
    const view = render(<InspectorPanel {...props({ capabilities: capabilitySnapshot, capabilityLoading: false })} />);
    fireEvent.click(screen.getByRole('tab', { name: 'MCP 2' }));

    const inheritedToggle = screen.getByRole('button', { name: '继承的能力1' });
    const inheritedGroup = inheritedToggle.closest('.project-capability-group')!;
    const inheritedDisclosure = inheritedGroup.querySelector('.project-capability-group__disclosure')!;
    expect(inheritedDisclosure.classList.contains('is-open')).toBe(false);

    fireEvent.click(inheritedToggle);
    expect(inheritedDisclosure.classList.contains('is-open')).toBe(true);
    const mcpRow = screen.getByText('用户 MCP').closest('.project-capability-row')!;
    const detailDisclosure = mcpRow.querySelector('.project-capability-row__detail-disclosure')!;
    expect(detailDisclosure.classList.contains('is-open')).toBe(false);

    fireEvent.click(within(mcpRow as HTMLElement).getByRole('button', { name: /用户 MCP/ }));
    expect(detailDisclosure.classList.contains('is-open')).toBe(true);
    expect(mcpRow.querySelector('.project-capability-row__detail')).toBeTruthy();

    fireEvent.click(within(mcpRow as HTMLElement).getByRole('button', { name: '收起详情' }));
    expect(detailDisclosure.classList.contains('is-open')).toBe(false);
    expect(mcpRow.querySelector('.project-capability-row__detail')).toBeTruthy();

    fireEvent.click(inheritedToggle);
    expect(inheritedDisclosure.classList.contains('is-open')).toBe(false);
    expect(view.container.querySelector('.project-capability-row__detail')).toBeTruthy();
  });
});
