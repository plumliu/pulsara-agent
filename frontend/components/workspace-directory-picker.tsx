import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowLeft, ArrowRight, ArrowUp, Check, ChevronRight, CircleAlert, Folder, FolderOpen, FolderPlus, House, LoaderCircle, RefreshCw, X } from 'lucide-react';
import type { WorkspaceDirectoryCandidate } from '../../desktop/src/backend-paths';
import { BrandMark } from './brand-mark';
import './workspace-directory-picker.css';

export interface WorkspaceDirectoryPage {
  root: string;
  path: string;
  parent: string | null;
  entries: Array<{name: string; path: string}>;
  next_cursor: string | null;
}
export interface WorkspaceDirectoryApi {
  chooseNative?(): Promise<WorkspaceDirectoryCandidate | null>;
  list(path: string | undefined, cursor: string | null, signal: AbortSignal): Promise<WorkspaceDirectoryPage>;
  create(path: string, name: string, signal: AbortSignal): Promise<{path: string}>;
  select(path: string, signal: AbortSignal): Promise<{path: string}>;
}

/** A user-level picker; it never borrows the selected conversation's connection. */
export function WorkspaceDirectoryPicker({api, signal, finish}: {
  api: WorkspaceDirectoryApi; signal: AbortSignal; finish(path: string | null): void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const drag = useRef<{pointerId: number; x: number; y: number} | null>(null);
  const position = (x: number, y: number) => {
    const element = dialog.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    Object.assign(element.style, {margin: '0', inset: 'auto',
      left: `${Math.max(0, Math.min(x, window.innerWidth - rect.width))}px`,
      top: `${Math.max(0, Math.min(y, window.innerHeight - rect.height))}px`});
  };
  const startDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || event.isPrimary === false || drag.current
      || (event.target as Element).closest('button')) return;
    const rect = dialog.current!.getBoundingClientRect();
    drag.current = {pointerId: event.pointerId, x: event.clientX - rect.left, y: event.clientY - rect.top};
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const current = drag.current;
    if (current?.pointerId === event.pointerId) position(event.clientX - current.x, event.clientY - current.y);
  };
  const [page, setPage] = useState<WorkspaceDirectoryPage>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [blockedSelection, setBlockedSelection] = useState<{path: string; message: string} | null>(null);
  const [navigation, setNavigation] = useState<{paths: string[]; index: number}>({paths: [], index: -1});
  const showError = (reason: unknown) => {
    if (signal.aborted) return;
    setError(reason instanceof Error ? reason.message : '无法读取目录，请刷新后重试。');
  };
  useEffect(() => {
    dialog.current?.showModal();
    const resize = () => {
      const element = dialog.current;
      if (element?.style.left) {
        const rect = element.getBoundingClientRect();
        position(rect.left, rect.top);
      }
    };
    window.addEventListener('resize', resize);
    void api.list(undefined, null, signal).then(value => {
      if (!signal.aborted) { setPage(value); setNavigation({paths: [value.path], index: 0}); }
    }).catch(showError).finally(() => { if (!signal.aborted) setBusy(false); });
    return () => window.removeEventListener('resize', resize);
    // This mounted picker owns exactly one request lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const load = async (path?: string, cursor: string | null = null, historyIndex?: number) => {
    const next = await api.list(path, cursor, signal);
    if (!signal.aborted) {
      setPage(previous => cursor && previous?.path === next.path
        ? {...next, entries: [...previous.entries, ...next.entries]} : next);
      if (!cursor) {
        setSelectedPath(null); setBlockedSelection(null); setCreating(false); setName('');
        setNavigation(previous => {
          if (historyIndex !== undefined) return {...previous, index: historyIndex};
          if (previous.paths[previous.index] === next.path) return previous;
          const paths = [...previous.paths.slice(0, previous.index + 1), next.path];
          return {paths, index: paths.length - 1};
        });
      }
    }
  };
  const browse = async (path?: string, cursor: string | null = null, historyIndex?: number) => {
    setBusy(true); setError('');
    try { await load(path, cursor, historyIndex); }
    catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const chooseNative = async () => {
    if (!api.chooseNative || !page) return;
    setBusy(true);
    try {
      const candidate = await api.chooseNative();
      if (!candidate || signal.aborted) return;
      setError('');
      setCreating(false); setName(''); setSelectedPath(null);
      if (candidate.linuxPath === null) {
        setBlockedSelection({path: candidate.path, message: `该目录无效，请选择 ${page.root} 下的文件夹。`});
        return;
      }
      try { await load(candidate.linuxPath); }
      catch (reason) {
        if (!signal.aborted) setBlockedSelection({path: candidate.linuxPath,
          message: reason instanceof Error ? reason.message : '该目录不可用，请重新选择。'});
      }
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const highlight = (path: string) => { setSelectedPath(path); setBlockedSelection(null); };
  const write = async () => {
    if (!page || blockedSelection) return;
    setBusy(true); setError('');
    try {
      const created = await api.create(page.path, name, signal);
      if (!signal.aborted) { setName(''); await browse(created.path); }
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const select = async () => {
    if (!page || blockedSelection) return;
    setBusy(true); setError('');
    try {
      const selected = await api.select(selectedPath ?? page.path, signal);
      if (!signal.aborted) finish(selected.path);
    } catch (reason) { showError(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const displayedPath = blockedSelection?.path ?? selectedPath ?? page?.path;
  const relative = page?.path.slice(page.root.length).split('/').filter(Boolean) ?? [];
  return <dialog ref={dialog} className="linux-directory-picker" aria-labelledby="linux-directory-title"
    onCancel={event => { event.preventDefault(); finish(null); }}>
    <header className="linux-directory-header" onPointerDown={startDrag} onPointerMove={moveDrag}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
      onLostPointerCapture={() => { drag.current = null; }}>
      <FolderOpen size={18} className="linux-directory-heading-icon" aria-hidden="true" />
      <h2 id="linux-directory-title">选择 Linux 工作目录</h2>
      {page && <button className="linux-directory-icon-button" aria-label="刷新" title="刷新" disabled={busy} onClick={() => void browse(page.path)}><RefreshCw size={15} aria-hidden="true" className={busy ? 'linux-directory-spinning' : undefined} /></button>}
      <button className="linux-directory-icon-button" aria-label="关闭目录选择" onClick={() => finish(null)}><X size={17} aria-hidden="true" /></button>
    </header>
    {page && !blockedSelection && <div className="linux-directory-toolbar">
      <div className="linux-directory-actions">
        <button className="linux-directory-icon-button" aria-label="后退" title="后退" disabled={busy || navigation.index <= 0} onClick={() => void browse(navigation.paths[navigation.index - 1], null, navigation.index - 1)}><ArrowLeft size={16} aria-hidden="true" /></button>
        <button className="linux-directory-icon-button" aria-label="前进" title="前进" disabled={busy || navigation.index >= navigation.paths.length - 1} onClick={() => void browse(navigation.paths[navigation.index + 1], null, navigation.index + 1)}><ArrowRight size={16} aria-hidden="true" /></button>
        <button className="linux-directory-icon-button" aria-label="上一级" title="上一级" disabled={busy || !page.parent} onClick={() => void browse(page.parent!)}><ArrowUp size={16} aria-hidden="true" /></button>
      </div>
      <div className="linux-directory-location" title={page.path}>
            <nav aria-label="目录位置">
              <button disabled={busy} onClick={() => void browse(page.root)}><House size={14} aria-hidden="true" /><span>用户家目录</span></button>
              {relative.map((part, index) => <span className="linux-directory-crumb" key={index}>
                <ChevronRight size={12} aria-hidden="true" /><button title={part} disabled={busy}
                  onClick={() => void browse(`${page.root}/${relative.slice(0, index + 1).join('/')}`)}>{part}</button>
              </span>)}
            </nav>
      </div>
      <button className="linux-directory-new-folder" disabled={busy || !!blockedSelection} onClick={() => { setCreating(true); setName(''); }}><FolderPlus size={15} aria-hidden="true" />新建文件夹</button>
    </div>}
    <div className="linux-directory-body">
      {page ? blockedSelection ? <div className="linux-directory-blocked" aria-label="无效目录">
        <BrandMark />
        <strong>Pulsara</strong>
        <p>无法选择此目录</p>
      </div> : <div className="linux-directory-browser">
        <aside className="linux-directory-places" aria-label="目录导航">
          <span className="linux-directory-places-title">位置</span>
          <button className={!relative.length ? 'is-current' : undefined} aria-current={!relative.length ? 'location' : undefined} disabled={busy} onClick={() => void browse(page.root)}><House size={15} aria-hidden="true" /><span>用户家目录</span></button>
          {relative.map((part, index) => <button className={index === relative.length - 1 ? 'is-current' : undefined} aria-current={index === relative.length - 1 ? 'location' : undefined} key={index} disabled={busy} title={part} onClick={() => void browse(`${page.root}/${relative.slice(0, index + 1).join('/')}`)}><Folder size={15} aria-hidden="true" /><span>{part}</span></button>)}
        </aside>
        <section className="linux-directory-files" aria-label="目录列表">
          <div className="linux-directory-list-heading"><span>名称</span><span>类型</span></div>
          <div className="linux-directory-file-scroll">
          {creating && <form className="linux-directory-create" onSubmit={event => { event.preventDefault(); void write(); }}>
            <FolderPlus size={18} aria-hidden="true" /><label><span className="linux-directory-sr-only">新文件夹名称</span><input autoFocus placeholder="新文件夹名称" value={name} disabled={busy} onChange={event => setName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }} /></label>
            <button className="linux-directory-icon-button linux-directory-create-confirm" aria-label="创建" title="创建" disabled={busy || !name.trim()} type="submit"><Check size={14} aria-hidden="true" /></button><button className="linux-directory-icon-button linux-directory-create-cancel" aria-label="取消新建" title="取消新建" disabled={busy} type="button" onClick={() => { setCreating(false); setName(''); }}><X size={14} aria-hidden="true" /></button>
          </form>}
          <ul aria-label="子目录" aria-busy={busy}>
            {page.entries.map((entry, index) => <li key={entry.path}><button className={selectedPath === entry.path ? 'is-selected' : undefined} aria-pressed={selectedPath === entry.path} aria-label={entry.name} title={entry.name} disabled={busy} onClick={() => highlight(entry.path)} onDoubleClick={() => void browse(entry.path)} onKeyDown={event => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === 'Enter' || event.key === 'ArrowRight') { event.preventDefault(); void browse(entry.path); }
              else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                const offset = event.key === 'ArrowDown' ? 1 : -1;
                const adjacent = page.entries[index + offset];
                if (adjacent) { event.preventDefault(); highlight(adjacent.path); const row = offset === 1 ? event.currentTarget.parentElement?.nextElementSibling : event.currentTarget.parentElement?.previousElementSibling; row?.querySelector('button')?.focus(); }
              }
            }}>
              <span className="linux-directory-filename"><Folder size={18} aria-hidden="true" /><span>{entry.name}</span></span><span className="linux-directory-filetype" aria-hidden="true">文件夹</span>
            </button></li>)}
          </ul>
          {!page.entries.length && !creating && <div className="linux-directory-list-empty">
            {busy ? <LoaderCircle size={25} className="linux-directory-spinning" aria-hidden="true" /> : <FolderOpen size={28} aria-hidden="true" />}
            <span>{busy ? '正在读取目录…' : '暂无子文件夹'}</span>
          </div>}
          {page.next_cursor && <button className="linux-directory-more" disabled={busy} onClick={() => void browse(page.path, page.next_cursor)}>加载更多</button>}
          </div>
          {error && <div className="linux-directory-alert" role="alert"><CircleAlert size={16} aria-hidden="true" /><span>{error}</span></div>}
        </section>
      </div> : <div className="linux-directory-state">
        <span className={`linux-directory-state-icon${error ? ' is-error' : ''}`}>
          {busy ? <LoaderCircle size={28} className="linux-directory-spinning" aria-hidden="true" /> : <CircleAlert size={28} aria-hidden="true" />}
        </span>
        <h3>{busy ? '正在读取目录…' : '暂时无法读取目录'}</h3>
        {error && <p role="alert">{error}</p>}
        {!busy && <button onClick={() => void browse()}><RefreshCw size={14} aria-hidden="true" />重新读取</button>}
      </div>}
    </div>
    {page && <footer>
      <div className="linux-directory-selection"><span>文件夹</span><output aria-label="所选文件夹" title={displayedPath}>{displayedPath}</output></div>
      <div className="linux-directory-footer-actions">
        {api.chooseNative && <button className="linux-directory-native-browse" disabled={busy} onClick={() => void chooseNative()}><FolderOpen size={14} aria-hidden="true" />浏览…</button>}
        <div className="linux-directory-confirm-actions">
          {blockedSelection && <span className="linux-directory-selection-error" role="alert">{blockedSelection.message}</span>}
          <button onClick={() => finish(null)}>取消</button><button className="linux-directory-primary" disabled={busy || !!blockedSelection} onClick={() => void select()}>选择文件夹</button>
        </div>
      </div>
    </footer>}
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
