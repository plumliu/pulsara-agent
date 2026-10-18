import { AlarmClock, Bot, LoaderCircle, UserRound } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { RuntimeAdapter } from '../lib/runtime-adapter';
import { AvatarCropDialog } from './avatar-crop-dialog';

const unavailable = async (): Promise<void> => { throw new Error('头像服务尚未连接。'); };
const AvatarContext = createContext({ src: undefined as string | undefined, upload: unavailable as (file: File) => Promise<void>, clear: unavailable });

export function UserAvatarProvider({ adapter, children }: { adapter: RuntimeAdapter; children: ReactNode }) {
  const [src, setSrc] = useState<string>();
  const revision = useRef(0);
  const requestOrder = useRef(0);
  const mutating = useRef(false);
  const publish = useCallback((configured: boolean) => {
    setSrc(configured ? `/api/user-avatar/image?v=${++revision.current}` : undefined);
  }, []);
  useEffect(() => {
    let disposed = false;
    const refresh = async () => {
      if (mutating.current) return;
      const order = ++requestOrder.current;
      try {
        const result = await adapter.userAvatar();
        if (!disposed && order === requestOrder.current) publish(result.configured);
      } catch { /* Presentation remains usable while the local service reconnects. */ }
    };
    void refresh();
    window.addEventListener('focus', refresh);
    return () => { disposed = true; window.removeEventListener('focus', refresh); };
  }, [adapter, publish]);
  const mutate = async (operation: () => Promise<{ configured: boolean }>) => {
    mutating.current = true;
    ++requestOrder.current;
    try { publish((await operation()).configured); }
    finally { mutating.current = false; }
  };
  return <AvatarContext.Provider value={{ src, upload: file => mutate(() => adapter.uploadUserAvatar(file)), clear: () => mutate(() => adapter.clearUserAvatar()) }}>{children}</AvatarContext.Provider>;
}

export function UserAvatar({ className = '' }: { className?: string }) {
  const { src } = useContext(AvatarContext);
  const [failedSrc, setFailedSrc] = useState<string>();
  return <span className={`person-avatar ${className}`} role="img" aria-label="用户头像">
    {src && src !== failedSrc
      // Local image, no Next image service.
      ? <img src={src} alt="" draggable={false} onError={() => setFailedSrc(src)} />
      : <UserRound size={15} aria-hidden="true" />}
  </span>;
}

export function AgentInputAvatar({ kind }: { kind: 'agent' | 'scheduled' }) {
  return <span className="person-avatar person-avatar--agent" role="img" aria-label={kind === 'agent' ? '主 Agent 头像' : '定时任务头像'}>
    {kind === 'scheduled' ? <AlarmClock size={15} aria-hidden="true" /> : <Bot size={15} aria-hidden="true" />}
  </span>;
}

export function AvatarSetting({ onNotify }: { onNotify: (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void }) {
  const avatar = useContext(AvatarContext);
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [cropFile, setCropFile] = useState<File>();
  const run = async (operation: () => Promise<void>, title: string) => {
    if (busy) return;
    setBusy(true);
    try { await operation(); onNotify(title, undefined, 'success'); }
    catch (error) { onNotify('头像未更新', error instanceof Error ? error.message : '请重试。', 'warning'); }
    finally { setBusy(false); }
  };
  return <><div className="setting-row avatar-setting">
    <UserAvatar />
    <span className="setting-row__copy"><strong>用户头像</strong></span>
    <div className="setting-row__control">
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp,image/gif" aria-label="上传用户头像" hidden
        onChange={event => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) setCropFile(file);
        }} />
      <button type="button" disabled={busy} onClick={() => fileInput.current?.click()}>{busy && <LoaderCircle size={12} className="avatar-spinner" />}{avatar.src ? '替换头像' : '上传头像'}</button>
      {avatar.src && <button type="button" disabled={busy} onClick={() => void run(avatar.clear, '已恢复默认头像')}>恢复默认</button>}
    </div>
  </div>{cropFile && <AvatarCropDialog file={cropFile} onClose={() => setCropFile(undefined)} onSave={async file => {
    await avatar.upload(file);
    onNotify('头像已更新', undefined, 'success');
  }} />}</>;
}
