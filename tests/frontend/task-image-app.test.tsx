import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { AgentTaskActivityPage, CanonicalPromptImagePart } from '../../frontend/lib/runtime-adapter';
import { deferred, FakeAdapter, renderWorkbench } from './pulsara-app-fixture';

afterEach(() => vi.restoreAllMocks());

it.each([false, true])('loads a view_image result through the app sidebar (inherited=%s)', async (inherited) => {
  const adapter = new FakeAdapter();
  adapter.taskInventory = [{ id: 'image-task', batchId: 'image-group', label: '图片核对', role: '研究',
    objective: '核对图表', status: 'running', parentId: 'turn-root', dependencyIds: [],
    completionAccepted: false, color: 'blue' }];
  const resultId = inherited ? 'ancestor-image-result' : 'child-image-result';
  const image: CanonicalPromptImagePart = {
    type: 'image', source: 'canonical', owner: { kind: 'entry', entryId: resultId },
    refOrdinal: 0, digest: `sha256:${'a'.repeat(64)}`, encodedBytes: 4,
    mediaType: 'image/png', width: 2, height: 1,
  };
  const common = { turnId: inherited ? 'ancestor-turn' : 'child-turn', acceptedAt: '2026-10-21T00:00:00Z',
    objective: '核对图表', body: '', contentKind: 'INLINE' as const, contentDigest: 'sha256:fixture',
    contentSize: 0, inherited, sourceTaskLabel: inherited ? '上游分析' : undefined };
  const toolResults = [{ assistantEntryId: 'image-request', toolCallId: 'image-call',
    resultEntryId: resultId, resultState: 'SUCCESS' }];
  const page: AgentTaskActivityPage = {
    inheritedContext: inherited ? { sourceLabel: '上游分析', materials: [] } : undefined,
    activities: [
      { ...common, entryId: 'image-request', entrySequence: 1, entryKind: 'ASSISTANT_TOOL_REQUEST',
        blocks: [{ blockId: 'image-block', ordinal: 0, kind: 'TOOL_CALL', toolCallId: 'image-call', toolName: 'view_image' }], toolResults },
      { ...common, entryId: resultId, entrySequence: 2, entryKind: 'TOOL_RESULT', blocks: [],
        toolResults, promptContent: { parts: [image] } },
    ],
  };
  adapter.listSessionTaskActivities.mockResolvedValue(page);
  const bytes = deferred<Uint8Array>();
  const createUrl = vi.spyOn(URL, 'createObjectURL');
  const revokeUrl = vi.spyOn(URL, 'revokeObjectURL');
  const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByLabelText('发送给 Pulsara');
  const read = vi.spyOn(adapter.lastConnection!, 'readPromptImage').mockReturnValue(bytes.promise);
  fireEvent.click(screen.getByRole('button', { name: '任务', exact: true }));
  fireEvent.click(await screen.findByRole('button', { name: /图片核对/ }));
  const detail = await screen.findByRole('complementary', { name: '图片核对 详情' });
  if (inherited) {
    const label = await within(detail).findByText('上下文继承自：上游分析');
    fireEvent.click(label);
    fireEvent.click(within(label.closest('details')!).getByRole('button', { name: '展开处理过程' }));
  }
  fireEvent.click(await within(detail).findByRole('button', { name: '展开工具详情：view_image' }));
  await waitFor(() => expect(read).toHaveBeenCalledExactlyOnceWith(image));
  expect(within(detail).getByText('正在读取图片…')).toBeTruthy();
  bytes.resolve(new Uint8Array([137, 80, 78, 71]));
  const preview = within(detail).getByRole('button', { name: '放大图片' });
  await waitFor(() => expect(preview.querySelector('img')?.getAttribute('src')).toMatch(/^blob:/));
  expect(createUrl).toHaveBeenCalledWith(expect.objectContaining({ type: 'image/png', size: 4 }));
  expect(within(detail).queryByText('图片读取失败，点击查看')).toBeNull();
  expect(preview.closest(inherited ? '.task-inherited-context' : '.task-conversation')).toBeTruthy();
  const url = preview.querySelector('img')!.getAttribute('src');
  view.unmount();
  expect(revokeUrl).toHaveBeenCalledWith(url);
});
