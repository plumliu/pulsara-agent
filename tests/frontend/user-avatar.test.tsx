import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ActivityRail } from '../../frontend/components/activity-rail';
import { SettingsView } from '../../frontend/components/settings-view';
import { ConversationMessages } from '../../frontend/components/workbench-view';
import { UserAvatarProvider } from '../../frontend/components/user-avatar';
import { LocalHttpRuntimeAdapter, type RuntimeAdapter } from '../../frontend/lib/runtime-adapter';
import { exportAvatarCrop, prepareAvatarCrop, releaseAvatarCrop } from '../../frontend/lib/avatar-crop';

// Test our boundary to the maintained cropper, rather than its gesture math.
vi.mock('react-easy-crop', () => ({ default: (props: { aspect: number; zoom: number; onCropAreaChange: (percent: unknown, pixels: unknown) => void }) =>
  <button type="button" data-aspect={props.aspect} data-zoom={props.zoom} onClick={() => props.onCropAreaChange({}, { x: 100, y: 10, width: 200, height: 200 })}>定位裁剪区域</button> }));

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true,
    value: function (this: HTMLDialogElement) { this.setAttribute('open', ''); } });
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 600, height: 300, close: vi.fn() })));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['PNG pixels'], { type: 'image/png' })));
});

afterEach(() => { cleanup(); Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function selectAndSave(file: File) {
  fireEvent.change(screen.getByLabelText('上传用户头像'), { target: { files: [file] } });
  const dialog = screen.getByRole('dialog');
  const locate = await within(dialog).findByRole('button', { name: '定位裁剪区域' });
  expect(locate.getAttribute('data-aspect')).toBe('1');
  fireEvent.change(screen.getByLabelText('缩放'), { target: { value: '2' } });
  expect(locate.getAttribute('data-zoom')).toBe('2');
  fireEvent.click(locate);
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
}

function setup() {
  const userAvatar = vi.fn(async () => ({ configured: false }));
  const uploadUserAvatar = vi.fn(async (_file: File) => ({ configured: true }));
  const clearUserAvatar = vi.fn(async () => ({ configured: false }));
  const adapter = {
    userAvatar, uploadUserAvatar, clearUserAvatar,
    localSettings: async () => ({ local_settings: { pulsara_home: '/test/home', postgres: null,
      memory_retrieval: { embedding: null, embedding_enabled: false, history_enabled: true, rerank: null, decision: null, ranking_mode: 'off' } },
    database_state: 'database_not_configured', model_configurations: [] }),
    modelCatalog: async () => ({ status: 'ready', routes: [] }),
  } as unknown as RuntimeAdapter;
  const notify = vi.fn();
  const history = (userSenderKind: 'user' | 'agent', scheduled = false) => <ConversationMessages
    messages={[{ id: scheduled ? 'scheduled' : userSenderKind, role: 'user', userKind: scheduled ? 'scheduled' : 'prompt', body: 'hello', time: '10:00' }]}
    userSenderKind={userSenderKind} userLabel={userSenderKind === 'agent' ? '主 Agent' : '你'}
    artifactOwnerKey="test" onReadToolArtifact={vi.fn()} onNotify={notify} />;
  render(<UserAvatarProvider adapter={adapter}>
    <ActivityRail activeView="settings" onNavigate={vi.fn()} />
    <SettingsView initialSection="general" adapter={adapter} theme="light" runtimeStatus="online" onThemeChange={vi.fn()} onConfigurationChanged={vi.fn()}
      onNotify={notify} sessionRevision={0} onSessionsChanged={vi.fn()} onDeleteSession={vi.fn()} />
    {history('user')}{history('agent')}{history('user', true)}
  </UserAvatarProvider>);
  const avatars = () => screen.getAllByRole('img', { name: '用户头像' });
  return { adapter, userAvatar, uploadUserAvatar, clearUserAvatar, notify, avatars };
}

it('updates the profile, rail, message and preview together while keeping agent and scheduler presets', async () => {
  const state = setup();
  await waitFor(() => expect(state.userAvatar).toHaveBeenCalledOnce());
  expect(state.avatars()).toHaveLength(4);
  for (const avatar of state.avatars()) {
    expect(avatar.classList.contains('person-avatar--default')).toBe(true);
    expect(avatar.querySelector('img')?.getAttribute('src')).toBe('/assets/pulsara-icon.png');
  }
  const file = new File(['image'], 'portrait.png', { type: 'image/png' });
  await selectAndSave(file);
  await waitFor(() => expect(screen.getByRole('button', { name: '替换头像' })).toBeTruthy());
  expect(state.uploadUserAvatar).toHaveBeenCalledOnce();
  const cropped = state.uploadUserAvatar.mock.calls[0][0];
  expect(cropped).not.toBe(file);
  expect(cropped.type).toBe('image/png');
  expect(cropped.name).toBe('avatar.png');
  const urls = state.avatars().map(avatar => avatar.querySelector('img')?.getAttribute('src'));
  expect(new Set(urls).size).toBe(1);
  expect(urls[0]).toMatch(/^\/api\/user-avatar\/image\?v=/);
  expect(screen.getByRole('img', { name: '主 Agent 头像' }).querySelector('img')).toBeNull();
  expect(screen.getByRole('img', { name: '定时任务头像' }).querySelector('img')).toBeNull();
  await selectAndSave(file);
  await waitFor(() => expect(state.uploadUserAvatar).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(state.avatars()[0].querySelector('img')?.getAttribute('src')).not.toBe(urls[0]));
  fireEvent.click(screen.getByRole('button', { name: '恢复默认' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '上传头像' })).toBeTruthy());
  expect(state.avatars().every(avatar => avatar.querySelector('img')?.getAttribute('src') === '/assets/pulsara-icon.png')).toBe(true);
});

it('keeps the previous avatar on upload failure and ignores a stale focus read', async () => {
  const state = setup();
  await waitFor(() => expect(state.userAvatar).toHaveBeenCalledOnce());
  let resolve!: (result: { configured: boolean }) => void;
  state.userAvatar.mockImplementationOnce(() => new Promise(result => { resolve = result; }));
  fireEvent(window, new Event('focus'));
  const file = new File(['image'], 'photo.png');
  await selectAndSave(file);
  await waitFor(() => expect(screen.getByRole('button', { name: '替换头像' })).toBeTruthy());
  await act(async () => { resolve({ configured: false }); });
  expect(screen.getByRole('button', { name: '替换头像' })).toBeTruthy();
  const before = state.avatars()[0].querySelector('img')?.getAttribute('src');
  state.uploadUserAvatar.mockRejectedValueOnce(new Error('不是有效图片'));
  await selectAndSave(file);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '不是有效图片');
  expect(state.avatars()[0].querySelector('img')?.getAttribute('src')).toBe(before);
  expect(screen.getByRole('button', { name: '保存', exact: true }).hasAttribute('disabled')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '取消', exact: true }));
  fireEvent(window, new Event('focus'));
  await waitFor(() => expect(screen.getByRole('button', { name: '上传头像' })).toBeTruthy());
});

it('cancels without uploading and releases decoded pixels even if decoding finishes after dismissal', async () => {
  const state = setup();
  const revoke = vi.spyOn(URL, 'revokeObjectURL');
  const file = new File(['original'], 'original.png');
  fireEvent.change(screen.getByLabelText('上传用户头像'), { target: { files: [file] } });
  await within(screen.getByRole('dialog')).findByRole('button', { name: '定位裁剪区域' });
  expect(state.uploadUserAvatar).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '取消', exact: true }));
  expect(revoke).toHaveBeenCalledOnce();
  expect(state.uploadUserAvatar).not.toHaveBeenCalled();
  let resolve!: (value: ImageBitmap) => void;
  vi.mocked(createImageBitmap).mockImplementationOnce(() => new Promise(result => { resolve = result; }));
  fireEvent.change(screen.getByLabelText('上传用户头像'), { target: { files: [file] } });
  const save = screen.getByRole('button', { name: '保存', exact: true });
  expect(save.hasAttribute('disabled')).toBe(true);
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
  const close = vi.fn();
  await act(async () => { resolve({ width: 600, height: 300, close } as unknown as ImageBitmap); });
  expect(close).toHaveBeenCalledOnce();
  expect(revoke).toHaveBeenCalledTimes(2);
  expect(state.uploadUserAvatar).not.toHaveBeenCalled();
});

it('reports unreadable images without uploading', async () => {
  const state = setup();
  vi.mocked(createImageBitmap).mockRejectedValueOnce(new Error('invalid pixels'));
  fireEvent.change(screen.getByLabelText('上传用户头像'), { target: { files: [new File(['bad'], 'bad.png')] } });
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '无法读取这张图片，请重新选择。');
  expect(state.uploadUserAvatar).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '保存', exact: true }).hasAttribute('disabled')).toBe(true);
});

it('exports the cropper pixel rectangle at the stored resolution with oriented source pixels', async () => {
  const file = new File(['original'], 'source.jpg');
  const source = await prepareAvatarCrop(file);
  expect(createImageBitmap).toHaveBeenCalledWith(file, { imageOrientation: 'from-image' });
  const drawImage = vi.fn();
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  const result = await exportAvatarCrop(source, { x: 120, y: 50, width: 200, height: 200 });
  expect(drawImage).toHaveBeenCalledExactlyOnceWith(source.image, 120, 50, 200, 200, 0, 0, 256, 256);
  expect(result.type).toBe('image/png');
  releaseAvatarCrop(source);
  expect(source.image.close).toHaveBeenCalledOnce();
});

it('sends the file body through the shared transport without JSON encoding', async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ configured: true }), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  const adapter = new LocalHttpRuntimeAdapter('avatar-test');
  const file = new File(['bytes'], 'avatar.webp', { type: 'image/webp' });
  await adapter.uploadUserAvatar(file);
  expect(fetch).toHaveBeenCalledWith('/api/user-avatar', expect.objectContaining({ method: 'PUT', body: file,
    credentials: 'same-origin', headers: { 'Content-Type': 'image/webp' } }));
});
