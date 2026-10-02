'use client';
import { Code2, Copy, Download, ExternalLink, FolderOpen, LoaderCircle, RotateCcw, X } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import type { FilePreview, FilePreviewApi, FilePreviewPage } from '../lib/file-preview';
import { FileLinkContext } from './file-link-context';
import { MarkdownBody, type MarkdownNotify } from './markdown-body';
import { SandboxedHtmlPreview } from './sandboxed-html-preview';

type Intent = { path: string; opener: HTMLElement; base?: string; retryPath: string };
type View = { intent: Intent; file?: FilePreview; error?: string };
const detail = (error: unknown) => error instanceof Error ? error.message : '文件暂时无法读取。';

type OpenFile = (path: string, opener: HTMLElement, base?: string) => void;
export function FilePreviewProvider({ api, children, onNotify, ownerKey }: { api?: FilePreviewApi; children: ReactNode; onNotify: MarkdownNotify; ownerKey: string }) {
  const delegateRef = useRef<OpenFile | null>(null);
  const open = useCallback<OpenFile>((...args) => {
    if (delegateRef.current) delegateRef.current(...args);
    else onNotify('无法打开文件', '会话尚未连接，请连接后重新打开。', 'warning');
  }, [onNotify]);
  return <FileLinkContext.Provider value={{ open }}>
    {children}
    {api && <FilePreviewHost key={ownerKey} api={api} onNotify={onNotify} delegateRef={delegateRef} />}
  </FileLinkContext.Provider>;
}

function FilePreviewHost({ api, onNotify, delegateRef }: { api?: FilePreviewApi; onNotify: MarkdownNotify; delegateRef: RefObject<OpenFile | null> }) {
  const [view, setView] = useState<View>();
  const desired = useRef<Intent | null>(null);
  const owned = useRef<FilePreview | null>(null);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const alive = useRef(true);
  const openerRef = useRef<HTMLElement | null>(null);
  const release = useCallback(async () => {
    const file = owned.current;
    owned.current = null;
    if (file) await api?.close(file.read_token).catch(() => undefined);
  }, [api]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      const active = desired.current !== null || owned.current !== null;
      desired.current = null;
      if (active) queue.current = queue.current.then(release);
    };
  }, [release]);
  const open = useCallback((path: string, opener: HTMLElement, base?: string) => {
    if (!opener.closest('.file-preview-dialog')) openerRef.current = opener;
    // Retain the clicked target independently of the outgoing preview token.
    // Opening a relative sibling revokes that token even if the file is missing.
    const parent = owned.current;
    const retryPath = base && parent?.read_token === base && !/^(?:\/|~|[a-z][a-z\d+.-]*:)/i.test(path)
      ? new URL(path, `file://${parent.path.split('/').map(encodeURIComponent).join('/')}`).href : path;
    const intent = { path, opener, base, retryPath };
    desired.current = intent;
    setView({ intent });
    queue.current = queue.current.then(async () => {
      if (desired.current !== intent || !alive.current) return;
      try {
        if (!api) throw new Error('会话尚未连接，请连接后重新打开。');
        const file = await api.open(path, base);
        owned.current = file;
        if (desired.current !== intent || !alive.current) { await release(); return; }
        setView({ intent, file });
      } catch (error) {
        owned.current = null;
        if (desired.current === intent && alive.current) setView({ intent, error: detail(error) });
      }
    });
  }, [api, release]);
  useLayoutEffect(() => {
    delegateRef.current = open;
    return () => { delegateRef.current = null; };
  }, [delegateRef, open]);
  const close = () => {
    desired.current = null;
    setView(undefined);
    queue.current = queue.current.then(release);
    openerRef.current?.focus({ preventScroll: true });
  };
  return view ? <FilePreviewDialog view={view} api={api} onClose={close} onNotify={onNotify}
      onRetry={() => open(view.file ? `file://${view.file.path.split('/').map(encodeURIComponent).join('/')}` : view.intent.retryPath, view.intent.opener)}
      open={open} /> : null;
}

function FilePreviewDialog({ view, api, onClose, onNotify, onRetry, open }: {
  view: View; api?: FilePreviewApi; onClose: () => void; onNotify: MarkdownNotify; onRetry: () => void;
  open: (path: string, opener: HTMLElement, base?: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const file = view.file;
  const [sourceToken, setSourceToken] = useState<string | null>(null);
  const source = Boolean(file && sourceToken === file.read_token);
  const close = () => { dialog.current?.close(); onClose(); };
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  const action = async (kind: 'open' | 'reveal') => {
    if (!file || !api) return;
    try { await api.action(file.read_token, kind); }
    catch (error) { onNotify('无法打开文件', detail(error), 'warning'); }
  };
  return createPortal(<dialog ref={dialog} className="file-preview-dialog" aria-label={`文件预览${file ? `：${file.name}` : ''}`}
    onCancel={event => { event.preventDefault(); close(); }}
    onClick={event => { if (event.target === event.currentTarget) { const r = event.currentTarget.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close(); } }}>
    <header className="file-preview-header">
      <div className="file-preview-title"><strong>{file?.name ?? '文件预览'}</strong><small title={file?.path ?? view.intent.path}>{file?.path ?? view.intent.path}</small></div>
      <nav aria-label="文件操作">
        {file && <>
          {!file.notice && ['markdown', 'table', 'html', 'svg'].includes(file.kind) && <button
            title={source ? '显示预览' : '查看源码'} aria-label={source ? '显示预览' : '查看源码'} aria-pressed={source}
            onClick={() => setSourceToken(source ? null : file.read_token)}><Code2 size={16} /></button>}
          <button title="复制路径" aria-label="复制路径" onClick={() => { void navigator.clipboard.writeText(file.path).then(() => onNotify('路径已复制', undefined, 'success'), () => onNotify('无法复制路径', undefined, 'warning')); }}><Copy size={16} /></button>
          <button title="打开所在文件夹" aria-label="打开所在文件夹" onClick={() => void action('reveal')}><FolderOpen size={16} /></button>
          {file.can_open && <button title="使用系统应用打开" aria-label="使用系统应用打开" onClick={() => void action('open')}><ExternalLink size={16} /></button>}
          {file.content_url && <a aria-label="下载原文件" title="下载原文件" href={`${file.content_url}?download=1`} download={file.name} referrerPolicy="no-referrer"><Download size={16} /></a>}
        </>}
        <button title="关闭预览" aria-label="关闭预览" onClick={close}><X size={18} /></button>
      </nav>
    </header>
    <div className="file-preview-body">
      {view.error ? <div className="file-preview-status" role="status"><p>{view.error}</p><button onClick={onRetry}><RotateCcw size={14} />重试</button></div>
        : !file || !api ? <div className="file-preview-status" role="status"><LoaderCircle className="is-spinning" size={20} />正在读取文件…</div>
          : <FileLinkContext.Provider value={{ open, basePreview: file.read_token, imagesUrl: file.images_url ?? undefined }}>
            <FileBody key={file.read_token} file={file} api={api} source={source} onNotify={onNotify} onRetry={onRetry} />
          </FileLinkContext.Provider>}
    </div>
  </dialog>, document.body);
}

type FileBodyProps = { file: FilePreview; api: FilePreviewApi; source: boolean; onNotify: MarkdownNotify; onRetry: () => void };

function FileBody({ file, api, source, onNotify, onRetry }: FileBodyProps) {
  const [error, setError] = useState<string>();
  const [htmlStatus, setHtmlStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [zoom, setZoom] = useState(1);
  const reading = ['text', 'markdown', 'table', 'svg'].includes(file.kind);
  const mode = file.kind === 'table' && !source ? 'table' : 'text';
  const htmlObservation = useCallback((status: 'ready' | 'error') => {
    setHtmlStatus(previous => previous === 'error' ? previous : status);
  }, []);
  if (file.notice) return <div className="file-preview-status" role="status">{file.notice}</div>;
  if (file.kind === 'html' && file.document_url) return <><div className="file-preview-html" hidden={source}>
    {htmlStatus !== 'ready' && <div className="file-preview-html-status" role="status">{htmlStatus === 'error' ? '部分内容未能加载，请检查 HTML 的静态依赖或脚本。' : '正在加载页面…'}</div>}
    <SandboxedHtmlPreview source={{ url: file.document_url }} title={file.name} onStatus={htmlObservation} />
  </div>{source && <FileReader file={file} api={api} source={source} mode="text" onNotify={onNotify} onRetry={onRetry} />}</>;
  if ((file.kind === 'image' || file.kind === 'svg') && !source && file.content_url) return <div className="file-preview-image">
    <div className="file-preview-pagebar"><button onClick={() => setZoom(value => value / 1.25)} aria-label="缩小图片">−</button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom(value => value * 1.25)} aria-label="放大图片">+</button><button onClick={() => setZoom(1)}>适应窗口</button></div>
    <div>{/* Native image decoding keeps SVG out of the application's DOM. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={file.content_url} alt={file.name} style={{ width: `${zoom * 100}%` }} onError={() => setError(file.kind === 'svg' ? 'SVG 暂时无法显示，可查看源码或下载原文件。' : '图片暂时无法解码，可下载或使用系统应用打开。')} />{error && <p role="status">{error}</p>}
    </div>
  </div>;
  if (file.kind === 'pdf' && file.content_url) return <object className="file-preview-pdf" data={file.content_url} type="application/pdf" aria-label={file.name}><p>浏览器无法预览此 PDF，请下载或使用系统应用打开。</p></object>;
  if (!reading) return <div className="file-preview-status"><FolderOpen size={28} /><strong>{file.name}</strong><p>{file.kind === 'directory' ? '可使用系统文件管理器打开此文件夹。' : file.can_open ? '可下载原文件或使用系统应用打开。' : '可下载原文件或打开所在文件夹。'}</p>{file.size !== null && <small>{file.size.toLocaleString()} 字节</small>}</div>;
  return <FileReader key={mode} file={file} api={api} source={source} mode={mode} onNotify={onNotify} onRetry={onRetry} />;
}

function FileReader({ file, api, source, mode, onNotify, onRetry }: FileBodyProps & { mode: 'text' | 'table' }) {
  const [cursor, setCursor] = useState(0);
  const [page, setPage] = useState<FilePreviewPage>();
  const [error, setError] = useState<string>();
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api.page(file.read_token, cursor, mode, controller.signal).then(value => {
      if (!controller.signal.aborted) { setPage(value); scroller.current?.scrollTo(0, 0); }
    }).catch(reason => { if (!controller.signal.aborted) setError(detail(reason)); });
    return () => controller.abort();
  }, [api, cursor, file.read_token, mode]);
  const move = (next: number) => { setPage(undefined); setError(undefined); setCursor(next); };
  const fullMarkdown = file.kind === 'markdown' && !source && page?.cursor === 0 && page.next_cursor === null;
  return <div className="file-preview-reader">
    {error ? <div className="file-preview-status" role="status"><p>{error}</p><button onClick={onRetry}><RotateCcw size={14} />重新打开</button></div>
      : !page ? <div className="file-preview-status" role="status">正在读取内容…</div>
        : page.mode === 'table' ? <PreviewTable rows={page.rows ?? []} cursor={page.cursor} />
          : <div className={`file-preview-text ${fullMarkdown ? 'is-markdown assistant-markdown' : ''}`} ref={scroller}>
            {!page.text ? <p className="file-preview-empty">空文件</p> : fullMarkdown ? <MarkdownBody body={page.text} onNotify={onNotify} />
              : <div className="file-preview-code"><pre className="file-preview-line-numbers" aria-hidden="true">{page.text.split('\n').map((_, index) => index + 1).join('\n')}</pre><pre><code>{page.text}</code></pre></div>}
          </div>}
    {(cursor > 0 || page?.next_cursor != null) && <footer className="file-preview-pagebar file-preview-pagination" aria-label="文件分页">
      {file.kind === 'markdown' && !source && <small>大文档以源文本分页显示</small>}
      {cursor > 0 && <button onClick={() => move(0)}>回到开头</button>}
      <span className="file-preview-page-position">{page ? page.mode === 'table' ? `从第 ${page.cursor + 1} 条记录起` : `从字节 ${page.cursor} 起` : ''}</span>
      {page?.next_cursor != null && <button onClick={() => move(page.next_cursor!)}>下一页 →</button>}
    </footer>}
  </div>;
}

function PreviewTable({ rows, cursor }: { rows: string[][]; cursor: number }) {
  const viewport = useRef<HTMLDivElement>(null);
  // Segment horizontal scrolling to stay within browser layout-coordinate limits.
  // Every column remains reachable; this is a display window, never a data cap.
  const columnWindow = 1024;
  const [columnPage, setColumnPage] = useState(0);
  const [position, setPosition] = useState({ top: 0, left: 0, width: 900, height: 600 });
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const resize = new ResizeObserver(() => setPosition(value => ({ ...value, width: element.clientWidth, height: element.clientHeight })));
    resize.observe(element);
    return () => resize.disconnect();
  }, []);
  // Window both axes: a CSV record with many empty columns must not create
  // hundreds of thousands of cells even though its byte page is small.
  const columns = rows.reduce((n, row) => Math.max(n, row.length), 0);
  const columnStart = Math.min(columnPage, Math.max(0, Math.ceil(columns / columnWindow) - 1)) * columnWindow;
  const columnCount = Math.min(columnWindow, columns - columnStart);
  const firstRow = Math.max(0, Math.floor(position.top / 34) - 2);
  const lastRow = Math.min(rows.length, firstRow + Math.ceil(position.height / 34) + 4);
  const firstCol = columnStart + Math.max(0, Math.floor(position.left / 180) - 1);
  const lastCol = Math.min(columnStart + columnCount, firstCol + Math.ceil(position.width / 180) + 2);
  return <>
    {columns > columnWindow && <div className="file-preview-pagebar"><label>列分组 <input aria-label="列分组" type="number" min={1} max={Math.ceil(columns / columnWindow)} value={columnStart / columnWindow + 1} onChange={event => {
      const next = Number(event.target.value);
      if (Number.isInteger(next) && next > 0 && next <= Math.ceil(columns / columnWindow)) {
        setColumnPage(next - 1); setPosition(value => ({ ...value, left: 0 })); if (viewport.current) viewport.current.scrollLeft = 0;
      }
    }} /></label><span>第 {columnStart + 1}–{columnStart + columnCount} 列，共 {columns} 列</span></div>}
    <div className="file-preview-table" ref={viewport} role="table" aria-label="文件表格" aria-rowcount={rows.length} aria-colcount={columns}
    onScroll={event => {
      // React clears currentTarget after dispatch; batched updates run later.
      const { scrollTop: top, scrollLeft: left } = event.currentTarget;
      setPosition(value => ({ ...value, top, left }));
    }}>
    {rows.length === 0 ? <p>空表格</p> : <div style={{ width: columnCount * 180, height: rows.length * 34, position: 'relative' }}>
      {rows.slice(firstRow, lastRow).map((row, offset) => <div role="row" aria-rowindex={firstRow + offset + 1} key={firstRow + offset} style={{ top: (firstRow + offset) * 34 }}>
        {Array.from({ length: Math.max(0, lastCol - firstCol) }, (_, index) => firstCol + index).map(column => <div role={cursor === 0 && firstRow + offset === 0 ? 'columnheader' : 'cell'} aria-colindex={column + 1} key={column}
          style={{ left: (column - columnStart) * 180 }} title={row[column] ?? ''}>{row[column] ?? ''}</div>)}
      </div>)}
    </div>}
  </div></>;
}
