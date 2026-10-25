import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './workspace-directory-picker.css';

export interface WorkspaceDirectoryPage {
  root: string;
  path: string;
  parent: string | null;
  entries: Array<{name: string; path: string}>;
  next_cursor: string | null;
}
export interface WorkspaceDirectoryApi {
  list(path: string | undefined, cursor: string | null, signal: AbortSignal): Promise<WorkspaceDirectoryPage>;
  initialize(signal: AbortSignal): Promise<{path: string}>;
  create(path: string, name: string, signal: AbortSignal): Promise<{path: string}>;
  select(path: string, signal: AbortSignal): Promise<{path: string}>;
}

/** A user-level picker; it never borrows the selected conversation's connection. */
export function WorkspaceDirectoryPicker({api, signal, finish}: {
  api: WorkspaceDirectoryApi; signal: AbortSignal; finish(path: string | null): void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [page, setPage] = useState<WorkspaceDirectoryPage>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [missingRoot, setMissingRoot] = useState(false);
  const [name, setName] = useState('');
  const showError = (reason: unknown) => {
    if (signal.aborted) return;
    setError(reason instanceof Error ? reason.message : '无法读取目录，请刷新后重试。');
    setMissingRoot((reason as {code?: string})?.code === 'WORKSPACE_ROOT_MISSING');
  };
  useEffect(() => {
    dialog.current?.showModal();
    void api.list(undefined, null, signal).then(value => {
      if (!signal.aborted) setPage(value);
    }).catch(showError).finally(() => { if (!signal.aborted) setBusy(false); });
    // This mounted picker owns exactly one request lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const browse = async (path?: string, cursor: string | null = null) => {
    setBusy(true); setError('');
    try {
      const next = await api.list(path, cursor, signal);
      if (!signal.aborted) setPage(previous => cursor && previous?.path === next.path
        ? {...next, entries: [...previous.entries, ...next.entries]} : next);
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const write = async (initialize = false) => {
    if (!initialize && !page) return;
    setBusy(true); setError('');
    try {
      const created = initialize ? await api.initialize(signal) : await api.create(page!.path, name, signal);
      if (!signal.aborted) { setName(''); setMissingRoot(false); await browse(created.path); }
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const select = async () => {
    if (!page) return;
    setBusy(true); setError('');
    try {
      const selected = await api.select(page.path, signal);
      if (!signal.aborted) finish(selected.path);
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const relative = page?.path.slice(page.root.length).split('/').filter(Boolean) ?? [];
  return <dialog ref={dialog} className="linux-directory-picker" aria-labelledby="linux-directory-title"
    onCancel={event => { event.preventDefault(); finish(null); }}>
    <h2 id="linux-directory-title">选择 Linux 工作目录</h2>
    <p>工程保存在 WSL 的 Linux 文件系统。Windows 工程可先通过资源管理器复制到这里。</p>
    {page && <>
      <nav aria-label="目录位置">
        <button disabled={busy} onClick={() => void browse(page.root)}>工程根目录</button>
        {relative.map((part, index) => <button key={index} disabled={busy}
          onClick={() => void browse(`${page.root}/${relative.slice(0, index + 1).join('/')}`)}>{part}</button>)}
      </nav>
      <code className="linux-directory-path">{page.path}</code>
      <div className="linux-directory-actions">
        <button disabled={busy || !page.parent} onClick={() => void browse(page.parent!)}>上一级</button>
        <button disabled={busy} onClick={() => void browse(page.path)}>刷新</button>
      </div>
      <ul aria-label="子目录" aria-busy={busy}>
        {page.entries.map(entry => <li key={entry.path}><button disabled={busy} onClick={() => void browse(entry.path)}>{entry.name}</button></li>)}
      </ul>
      {!busy && !page.entries.length && <p>此目录没有可选的子目录。</p>}
      {page.next_cursor && <button disabled={busy} onClick={() => void browse(page.path, page.next_cursor)}>加载更多</button>}
      <form onSubmit={event => { event.preventDefault(); void write(); }}>
        <label>新文件夹名称<input value={name} disabled={busy} onChange={event => setName(event.target.value)} /></label>
        <button disabled={busy || !name.trim()} type="submit">新建文件夹</button>
      </form>
    </>}
    {error && <p role="alert">{error}</p>}
    {missingRoot && <button disabled={busy} onClick={() => void write(true)}>创建工程根目录</button>}
    {!page && !missingRoot && !busy && <button onClick={() => void browse()}>重新读取</button>}
    <footer><button onClick={() => finish(null)}>取消</button><button disabled={busy || !page} onClick={() => void select()}>选择此目录</button></footer>
  </dialog>;
}

export function pickLinuxWorkspaceDirectory(api: WorkspaceDirectoryApi, signal?: AbortSignal): Promise<string | null> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const requests = new AbortController();
    let finished = false;
    const dispose = () => {
      requests.abort();
      signal?.removeEventListener('abort', abort);
      queueMicrotask(() => { root.unmount(); container.remove(); });
    };
    const finish = (path: string | null) => {
      if (finished) return;
      finished = true; dispose(); resolve(path);
    };
    const abort = () => {
      if (finished) return;
      finished = true; dispose(); reject(signal?.reason ?? new DOMException('Aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', abort, {once: true});
    root.render(<WorkspaceDirectoryPicker api={api} signal={requests.signal} finish={finish} />);
  });
}
