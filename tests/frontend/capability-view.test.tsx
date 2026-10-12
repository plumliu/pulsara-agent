import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CapabilityView } from '../../frontend/components/capability-view';
import type { UserCapabilitySnapshot, UserMcpServerCapability } from '../../frontend/lib/pulsara-types';

afterEach(cleanup);

const server: UserMcpServerCapability = {
  id: 'docs', name: '文档服务', config: {}, currentIdentity: 'docs-config', enabled: true,
  status: 'configured', required: false, availableToSubagents: true,
  toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0,
  instructions: '', hasFailure: false, transport: { kind: 'http', summary: 'https://example.com/mcp' }, tools: [],
};
const snapshot: UserCapabilitySnapshot = {
  roots: [], skills: { status: 'ready', configPath: '', items: [], issues: [], details: [], roots: [] },
  plugins: { status: 'ready', items: [], details: [] }, mcp: { configPath: '', servers: [server] },
};
const callbacks = {
  onNotify: vi.fn(), onRefresh: vi.fn(), onOpenRoot: vi.fn(), onInstallSkill: vi.fn(),
  onPreviewSkills: vi.fn(), onInstallPlugin: vi.fn(), onPreviewPlugin: vi.fn(),
  onCreateMcp: vi.fn(), onTestMcp: vi.fn(), onPreviewMcpImport: vi.fn(), onImportMcp: vi.fn(),
  onEditMcp: vi.fn(), onRemoveMcp: vi.fn(), onMcpAuthorization: vi.fn(),
  onToggleSkill: vi.fn(), onRemoveSkill: vi.fn(), onToggleMcp: vi.fn(),
  onTogglePlugin: vi.fn(), onRemovePlugin: vi.fn(), onEditPluginConnection: vi.fn(), onPluginMcpAuthorization: vi.fn(),
};

it('distinguishes unread inventory from a connected server with genuinely zero tools', () => {
  const view = render(<CapabilityView {...callbacks} loading={false} snapshot={snapshot} />);
  fireEvent.click(screen.getByRole('tab', { name: 'MCP 1' }));
  fireEvent.click(screen.getByText('文档服务'));
  expect(screen.getByText('尚未读取工具列表')).toBeTruthy();
  const stats = view.container.querySelector('.capability-detail-stats')!;
  expect(stats.textContent).not.toContain('0 工具');
  expect(stats.textContent).not.toContain('0 资源');
  view.rerender(<CapabilityView {...callbacks} loading={false} snapshot={{ ...snapshot, mcp: { ...snapshot.mcp, servers: [{ ...server, status: 'ready' }] } }} />);
  expect(screen.queryByText('尚未读取工具列表')).toBeNull();
  expect(stats.textContent).toContain('0 工具');
});

it('shows tool counts and tool descriptions from the connected session', () => {
  render(<CapabilityView {...callbacks} loading={false} snapshot={{ ...snapshot, mcp: { ...snapshot.mcp, servers: [{ ...server, status: 'ready', toolCount: 28, tools: [{ name: 'mcp__docs__search', remoteName: 'search', description: '搜索文档', effect: 'read-only', availableToSubagents: true, parallelSafe: true }] }] } }} />);
  fireEvent.click(screen.getByRole('tab', { name: 'MCP 1' }));
  fireEvent.click(screen.getByText('文档服务'));
  expect(screen.getByText('28')).toBeTruthy();
  expect(screen.getByText('mcp__docs__search')).toBeTruthy();
  expect(screen.getByText('搜索文档')).toBeTruthy();
  expect(within(screen.getByRole('tablist')).getByRole('tab', { name: 'MCP 1' })).toBeTruthy();
});
