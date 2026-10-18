import {
  ArrowLeft,
  ArrowRight,
  Code2,
  Copy,
  FolderOpen,
  LoaderCircle,
  RotateCcw,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { SavedImageData } from "../lib/runtime-adapter";
import type {
  FilePreview,
  FilePreviewApi,
  FilePreviewPage,
} from "../lib/file-preview";
import { classifyFileLink } from "../lib/file-preview";
import { desktopHost, writeClipboardText } from "../lib/desktop-host";
import { useDesktopWorkspace } from "./desktop-workspace";
import { FileLinkContext } from "./file-link-context";
import { MarkdownBody, type MarkdownNotify } from "./markdown-body";
import { SandboxedHtmlPreview } from "./sandboxed-html-preview";

const detail = (error: unknown) =>
  error instanceof Error ? error.message : "文件暂时无法读取。";
type Position = { top: number; left: number };
type Visit = {
  path: string;
  anchor?: string;
  position: Position;
  cursor: number;
  source: boolean;
  title?: string;
  columnPage?: number;
  zoom?: number;
};
type Request = {
  path: string;
  mode: "new" | "history" | "refresh";
  index?: number;
};
type BrowserTab = {
  id: string;
  sessionId: string;
  root?: string;
  history: Visit[];
  index: number;
  file?: FilePreview;
  api?: FilePreviewApi;
  error?: string;
  failed?: Request;
  loading: boolean;
  request?: object;
  touched: number;
  preferredId?: string;
};
type OpenFile = (
  path: string,
  opener: HTMLElement,
  base?: string,
  newTab?: boolean,
) => void;
const fileUrl = (path: string) =>
  `file://${path.split("/").map(encodeURIComponent).join("/")}`;
const pageAddress = (path: string) =>
  path.startsWith("/") ? fileUrl(path) : path;
const visit = (path: string): Visit => ({
  path,
  position: { top: 0, left: 0 },
  cursor: 0,
  source: false,
});

export function FilePreviewProvider({
  api,
  children,
  onNotify,
  ownerKey,
  sessionId = ownerKey,
  readImageReference,
  imageOwnerKey,
}: {
  sessionId?: string;
  imageOwnerKey?: string;
  readImageReference?: (
    ref: string,
    signal: AbortSignal,
  ) => Promise<SavedImageData>;
  api?: FilePreviewApi;
  children: ReactNode;
  onNotify: MarkdownNotify;
  ownerKey: string;
}) {
  const workspace = useDesktopWorkspace();
  const workspaceRef = useRef(workspace);
  useLayoutEffect(() => {
    workspaceRef.current = workspace;
  }, [workspace]);
  const tabs = useRef(new Map<string, BrowserTab>());
  const [revision, setRevision] = useState(0);
  const changed = useCallback(() => setRevision((n) => n + 1), []);
  const connection = useRef({ api, ownerKey, sessionId });
  useLayoutEffect(() => {
    connection.current = { api, ownerKey, sessionId };
  }, [api, ownerKey, sessionId]);
  const release = (tab: BrowserTab) => {
    const old = tab.file;
    const owner = tab.api;
    tab.file = undefined;
    tab.api = undefined;
    if (old) void owner?.close(old.read_token).catch(() => undefined);
  };
  const close = (tab: BrowserTab) => {
    tab.request = undefined;
    release(tab);
    tabs.current.delete(tab.id);
    changed();
  };
  const load = async (
    tab: BrowserTab,
    request: Request,
    deduplicate = false,
  ) => {
    const currentConnection = connection.current;
    const owner = currentConnection.api;
    const serial = {};
    tab.request = serial;
    tab.loading = true;
    tab.error = undefined;
    tab.failed = undefined;
    if (request.mode === "history") {
      tab.index = request.index!;
      release(tab);
    }
    changed();
    if (!owner || tab.sessionId !== currentConnection.sessionId) {
      tab.loading = false;
      tab.error = "会话尚未连接，请连接后重试。";
      tab.failed = request;
      changed();
      return;
    }
    const old = tab.file;
    try {
      const file = await owner.open(request.path, old?.read_token, tab.root);
      if (
        tab.request !== serial ||
        !tabs.current.has(tab.id) ||
        connection.current.ownerKey !== currentConnection.ownerKey ||
        connection.current.sessionId !== tab.sessionId
      ) {
        void owner.close(file.read_token).catch(() => undefined);
        return;
      }
      if (deduplicate) {
        const matches = [...tabs.current.values()]
          .filter(
            (other) =>
              other !== tab &&
              other.sessionId === tab.sessionId &&
              other.file?.path === file.path &&
              other.root === file.reading_root,
          )
          .sort((a, b) =>
            a.id === tab.preferredId
              ? -1
              : b.id === tab.preferredId
                ? 1
                : b.touched - a.touched,
          );
        if (matches[0]) {
          const match = matches[0];
          match.history[match.index].anchor = new URL(
            request.path,
            fileUrl(file.path),
          ).hash.slice(1);
          match.touched = Date.now();
          void owner.close(file.read_token).catch(() => undefined);
          close(tab);
          workspaceRef.current?.close(tab.id);
          workspaceRef.current?.activate(match.id);
          changed();
          return;
        }
      }
      release(tab);
      tab.file = file;
      tab.api = owner;
      tab.root = file.reading_root;
      const anchor = new URL(request.path, fileUrl(file.path)).hash.slice(1);
      if (request.mode === "new" && tab.history.length) {
        tab.history = tab.history.slice(0, tab.index + 1);
        tab.history.push(visit(file.path));
        tab.index = tab.history.length - 1;
      } else if (!tab.history.length) {
        tab.history = [visit(file.path)];
        tab.index = 0;
      }
      const page = tab.history[tab.index];
      page.cursor = 0;
      page.path = file.path;
      page.anchor = anchor || page.anchor;
      tab.loading = false;
      changed();
    } catch (error) {
      if (tab.request !== serial || !tabs.current.has(tab.id)) return;
      tab.loading = false;
      tab.error = detail(error);
      tab.failed = request;
      if (!tab.history.length) tab.history = [visit(request.path)];
      if (
        (error as { code?: string })?.code === "PREVIEW_EXPIRED" ||
        (request.mode !== "new" && tab.file?.kind === "html")
      )
        release(tab);
      changed();
    }
  };
  const create = (path: string, root?: string, deduplicate = false) => {
    if (!workspaceRef.current) {
      onNotify("无法打开文件", "文件工作区当前不可用。", "warning");
      return;
    }
    const tab: BrowserTab = {
      id: crypto.randomUUID(),
      sessionId: connection.current.sessionId,
      root,
      history: [visit(path)],
      index: 0,
      loading: true,
      touched: Date.now(),
      preferredId: workspaceRef.current.activeId,
    };
    tabs.current.set(tab.id, tab);
    workspaceRef.current?.open({
      id: tab.id,
      sessionId: tab.sessionId,
      kind: "file",
      title: path.split("/").pop() || "文件",
      render: () => null,
      onClose: () => close(tab),
    });
    void load(tab, { path, mode: "refresh" }, deduplicate);
  };
  const navigate = (tab: BrowserTab, path: string, newTab = false) => {
    const page = tab.history[tab.index];
    let target: string;
    try {
      if (classifyFileLink(path) === "external") {
        const native = desktopHost();
        if (!native) throw new Error("当前环境无法安全打开外部链接。");
        void native
          .openExternal(path)
          .catch((error) => onNotify("无法打开链接", detail(error), "warning"));
        return;
      }
      const url = new URL(path, pageAddress(page.path));
      target = url.href;
      if (url.protocol !== "file:") throw new Error("此页面跳转不受支持。");
      if (!newTab && decodeURIComponent(url.pathname) === page.path) {
        tab.history = tab.history.slice(0, tab.index + 1);
        tab.history.push({
          ...page,
          position: { ...page.position },
          anchor: url.hash.slice(1),
        });
        tab.index++;
        changed();
        return;
      }
    } catch (error) {
      onNotify("无法打开文档", detail(error), "warning");
      return;
    }
    if (newTab) create(target, tab.root);
    else void load(tab, { path: target, mode: "new" });
  };
  const open: OpenFile = (path, _opener, base, newTab = false) => {
    const source = base
      ? [...tabs.current.values()].find((tab) => tab.file?.read_token === base)
      : undefined;
    if (source) navigate(source, path, newTab);
    else create(path, undefined, !newTab);
  };
  const actions = useRef({ load, navigate, open, close });
  useLayoutEffect(() => {
    actions.current = { load, navigate, open, close };
  });
  const stableOpen = useCallback<OpenFile>(
    (...args) => actions.current.open(...args),
    [],
  );
  useEffect(() => {
    for (const tab of tabs.current.values()) {
      tab.request = undefined;
      release(tab);
      if (tab.sessionId === sessionId && tab.history.length)
        void actions.current.load(tab, {
          path: pageAddress(tab.history[tab.index].path),
          mode: "refresh",
        });
    }
    return () => {
      for (const tab of tabs.current.values()) {
        tab.request = undefined;
        release(tab);
      }
    };
  }, [ownerKey, api, sessionId, changed]);
  useEffect(() => {
    const id = workspaceRef.current?.activeId;
    if (id) {
      const selected = tabs.current.get(id);
      if (selected) selected.touched = Date.now();
    }
  }, [workspace?.activeId]);
  useEffect(() => {
    for (const tab of tabs.current.values()) {
      const page = tab.history[tab.index];
      workspaceRef.current?.update(tab.id, {
        title:
          page?.title ||
          tab.file?.name ||
          page?.path.split("/").pop() ||
          "文件",
        render: () => (
          <FileBrowserView
            tab={tab}
            onNotify={onNotify}
            onVisitChange={(patch) => {
              Object.assign(page, patch);
              if (patch.title !== undefined || patch.source !== undefined)
                changed();
            }}
            onDismiss={() => {
              tab.error = undefined;
              tab.failed = undefined;
              changed();
            }}
            onRetry={() =>
              void actions.current.load(
                tab,
                tab.failed ?? { path: pageAddress(page.path), mode: "refresh" },
              )
            }
            onRefresh={() =>
              void actions.current.load(tab, {
                path: pageAddress(page.path),
                mode: "refresh",
              })
            }
            onHistory={(index) =>
              void actions.current.load(tab, {
                path: pageAddress(tab.history[index].path),
                mode: "history",
                index,
              })
            }
            open={(path, _opener, _base, newTab) =>
              actions.current.navigate(tab, path, newTab)
            }
          />
        ),
      });
    }
  }, [revision, onNotify, changed, ownerKey]);
  return (
    <FileLinkContext.Provider
      value={{ open: stableOpen, readImageReference, imageOwnerKey }}
    >
      {children}
    </FileLinkContext.Provider>
  );
}

function FileBrowserView({
  tab,
  onNotify,
  onVisitChange,
  onDismiss,
  onRetry,
  onRefresh,
  onHistory,
  open,
}: {
  tab: BrowserTab;
  onNotify: MarkdownNotify;
  onVisitChange: (patch: Partial<Visit>) => void;
  onDismiss: () => void;
  onRetry: () => void;
  onRefresh: () => void;
  onHistory: (index: number) => void;
  open: OpenFile;
}) {
  const file = tab.file;
  const page = tab.history[tab.index];
  const api = tab.api;
  const reveal = async () => {
    if (!file || !api) return;
    try {
      await api.action(file.read_token, "reveal");
    } catch (error) {
      onNotify("无法打开文件", detail(error), "warning");
    }
  };
  return (
    <section
      className="file-browser-panel"
      aria-label={`文件预览：${file?.name ?? page?.path ?? ""}`}
    >
      <header className="file-preview-header">
        <nav aria-label="文件操作">
          <button
            aria-label="后退"
            disabled={tab.index <= 0}
            onClick={() => onHistory(tab.index - 1)}
          >
            <ArrowLeft size={16} />
          </button>
          <button
            aria-label="前进"
            disabled={tab.index >= tab.history.length - 1}
            onClick={() => onHistory(tab.index + 1)}
          >
            <ArrowRight size={16} />
          </button>
          <button aria-label="刷新文件" onClick={onRefresh} disabled={!page}>
            <RotateCcw size={16} />
          </button>
          {file && (
            <>
              {!file.notice &&
                ["markdown", "table", "html", "svg"].includes(file.kind) && (
                  <button
                    aria-label={page.source ? "显示预览" : "查看源码"}
                    aria-pressed={page.source}
                    onClick={() => {
                      onVisitChange({
                        source: !page.source,
                        ...(file.kind === "table" ? { cursor: 0 } : {}),
                      });
                    }}
                  >
                    <Code2 size={16} />
                  </button>
                )}
              <button
                aria-label="复制路径"
                title={file.path}
                onClick={() =>
                  void writeClipboardText(file.path)
                    .catch(() => onNotify("无法复制路径", undefined, "warning"))
                }
              >
                <Copy size={16} />
              </button>
              <button
                aria-label="打开所在文件夹"
                onClick={() => void reveal()}
              >
                <FolderOpen size={16} />
              </button>
            </>
          )}
        </nav>
      </header>
      {tab.error && (
        <div className="file-preview-error" role="status">
          <p>{tab.failed?.path}</p>
          <p>{tab.error}</p>
          {file && <small>此前读取的内容，当前未能更新</small>}
          <button onClick={onRetry}>重试</button>
          {file && <button onClick={onDismiss}>返回</button>}
        </div>
      )}
      {tab.loading && (
        <div role="status">
          <LoaderCircle className="is-spinning" size={16} />
          正在读取文件…
        </div>
      )}
      <div className="file-preview-body">
        {file && api && (
          <FileLinkContext.Provider
            value={{
              open,
              basePreview: file.read_token,
              imagesUrl: file.images_url ?? undefined,
            }}
          >
            <FileBody
              key={file.read_token}
              file={file}
              api={api}
              source={page.source}
              onNotify={onNotify}
              onRetry={onRetry}
              visit={page}
              onVisitChange={onVisitChange}
              onNavigate={(href, newTab) => {
                const prefix = new URL(
                  file.document_url!,
                  window.location.origin,
                );
                const url = new URL(href, prefix);
                if (
                  url.origin === prefix.origin &&
                  url.pathname.startsWith(
                    prefix.pathname.split("/resources/")[0] + "/resources/",
                  )
                ) {
                  const relative = url.pathname.split("/resources/")[1];
                  open(
                    fileUrl(file.reading_root) + "/" + relative + url.hash,
                    document.body,
                    file.read_token,
                    newTab,
                  );
                } else if (url.origin === prefix.origin) {
                  open(
                    `file://${url.pathname}${url.hash}`,
                    document.body,
                    file.read_token,
                    newTab,
                  );
                } else if (url.protocol === "file:") {
                  open(url.href, document.body, file.read_token, newTab);
                } else if (
                  ["http:", "https:", "mailto:"].includes(url.protocol) &&
                  url.origin !== prefix.origin
                )
                  open(url.href, document.body, file.read_token, newTab);
                else
                  onNotify(
                    "无法打开文档",
                    "此页面跳转不在允许的目录范围内。",
                    "warning",
                  );
              }}
            />
          </FileLinkContext.Provider>
        )}
      </div>
    </section>
  );
}

type FileBodyProps = {
  file: FilePreview;
  api: FilePreviewApi;
  source: boolean;
  onNotify: MarkdownNotify;
  onRetry: () => void;
  visit?: Visit;
  onVisitChange?: (patch: Partial<Visit>) => void;
  onNavigate?: (href: string, newTab: boolean) => void;
};

function FileBody({
  file,
  api,
  source,
  onNotify,
  onRetry,
  visit,
  onVisitChange,
  onNavigate,
}: FileBodyProps) {
  const [error, setError] = useState<string>();
  const [htmlStatus, setHtmlStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [zoom, setZoom] = useState(visit?.zoom ?? 1);
  const zoomTo = (next: number) => {
    setZoom(next);
    onVisitChange?.({ zoom: next });
  };
  const reading = ["text", "markdown", "table", "svg"].includes(file.kind);
  const mode = file.kind === "table" && !source ? "table" : "text";
  const htmlObservation = useCallback((status: "ready" | "error") => {
    setHtmlStatus((previous) => (previous === "error" ? previous : status));
  }, []);
  if (file.notice)
    return (
      <div className="file-preview-status" role="status">
        {file.notice}
      </div>
    );
  if (file.kind === "html" && file.document_url)
    return (
      <>
        <div className="file-preview-html" hidden={source}>
          {htmlStatus !== "ready" && (
            <div className="file-preview-html-status" role="status">
              {htmlStatus === "error"
                ? "页面跳转已阻止或部分内容无法加载；请使用文档链接导航，或刷新重试。"
                : "正在加载页面…"}
            </div>
          )}
          <SandboxedHtmlPreview
            source={{ url: file.document_url }}
            title={file.name}
            onStatus={htmlObservation}
            onNavigation={onNavigate}
            onTitle={(title) => {
              if (visit && visit.title !== title) {
                onVisitChange?.({ title });
              }
            }}
            position={visit?.position}
            anchor={visit?.anchor}
            onPosition={(position) => {
              onVisitChange?.({ position });
            }}
          />
        </div>
        {source && (
          <FileReader
            file={file}
            api={api}
            source={source}
            mode="text"
            onNotify={onNotify}
            onRetry={onRetry}
            visit={visit}
            onVisitChange={onVisitChange}
          />
        )}
      </>
    );
  if (
    (file.kind === "image" || file.kind === "svg") &&
    !source &&
    file.content_url
  )
    return (
      <div className="file-preview-image">
        <div className="file-preview-pagebar">
          <button onClick={() => zoomTo(zoom / 1.25)} aria-label="缩小图片">
            −
          </button>
          <span>{Math.round(zoom * 100)}%</span>
          <button onClick={() => zoomTo(zoom * 1.25)} aria-label="放大图片">
            +
          </button>
          <button onClick={() => zoomTo(1)}>适应窗口</button>
        </div>
        <div
          onScroll={(event) =>
            onVisitChange?.({
              position: {
                top: event.currentTarget.scrollTop,
                left: event.currentTarget.scrollLeft,
              },
            })
          }
        >
          {/* Native image decoding keeps SVG out of the application's DOM. */}
          <img
            src={file.content_url}
            alt={file.name}
            onLoad={(event) => {
              const viewport = event.currentTarget.parentElement;
              if (viewport) {
                viewport.scrollTop = visit?.position.top ?? 0;
                viewport.scrollLeft = visit?.position.left ?? 0;
              }
            }}
            style={{ width: `${zoom * 100}%` }}
            onError={() =>
              setError(
                file.kind === "svg"
                  ? "SVG 暂时无法显示，可查看源码，或打开所在文件夹后查看原文件。"
                  : "图片暂时无法解码，请打开所在文件夹后使用系统应用查看。",
              )
            }
          />
          {error && <p role="status">{error}</p>}
        </div>
      </div>
    );
  if (file.kind === "pdf" && file.content_url)
    return (
      <object
        className="file-preview-pdf"
        data={file.content_url}
        type="application/pdf"
        aria-label={file.name}
      >
        <p>无法预览此 PDF，请打开所在文件夹后使用系统应用查看。</p>
      </object>
    );
  if (!reading)
    return (
      <div className="file-preview-status">
        <FolderOpen size={28} />
        <strong>{file.name}</strong>
        <p>
          {file.kind === "directory"
            ? "请点击“打开所在文件夹”，在文件管理器中查看。"
            : "请打开所在文件夹后使用系统应用查看原文件。"}
        </p>
        {file.size !== null && <small>{file.size.toLocaleString()} 字节</small>}
      </div>
    );
  return (
    <FileReader
      key={mode}
      file={file}
      api={api}
      source={source}
      mode={mode}
      onNotify={onNotify}
      onRetry={onRetry}
      visit={visit}
      onVisitChange={onVisitChange}
    />
  );
}

function FileReader({
  file,
  api,
  source,
  mode,
  onNotify,
  onRetry,
  visit,
  onVisitChange,
}: FileBodyProps & { mode: "text" | "table" }) {
  const [cursor, setCursor] = useState(visit?.cursor ?? 0);
  const [page, setPage] = useState<FilePreviewPage>();
  const [error, setError] = useState<string>();
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .page(file.read_token, cursor, mode, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setPage(value);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(detail(reason));
      });
    return () => controller.abort();
  }, [api, cursor, file.read_token, mode]);
  useEffect(() => {
    const element = scroller.current;
    if (!element || !page) return;
    element.scrollTop = visit?.position.top ?? 0;
    element.scrollLeft = visit?.position.left ?? 0;
    if (visit?.anchor) {
      const target = element.querySelector(
        `[id="${CSS.escape(decodeURIComponent(visit.anchor))}"]`,
      );
      target?.scrollIntoView();
      const line = /^L([1-9][0-9]*)(?:-L?[0-9]+)?$/i.exec(visit.anchor);
      if (!target && line) {
        const code = element.querySelector("code");
        if (code) {
          const style = getComputedStyle(code);
          const height =
            parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
          element.scrollTop = (Number(line[1]) - 1) * height;
        }
      }
    }
  }, [page, visit?.anchor]);
  const move = (next: number) => {
    onVisitChange?.({ cursor: next, position: { top: 0, left: 0 } });
    setPage(undefined);
    setError(undefined);
    setCursor(next);
  };
  const fullMarkdown =
    file.kind === "markdown" &&
    !source &&
    page?.cursor === 0 &&
    page.next_cursor === null;
  return (
    <div className="file-preview-reader">
      {error ? (
        <div className="file-preview-status" role="status">
          <p>{error}</p>
          <button onClick={onRetry}>
            <RotateCcw size={14} />
            重新打开
          </button>
        </div>
      ) : !page ? (
        <div className="file-preview-status" role="status">
          正在读取内容…
        </div>
      ) : page.mode === "table" ? (
        <PreviewTable
          rows={page.rows ?? []}
          cursor={page.cursor}
          visit={visit}
          onVisitChange={onVisitChange}
        />
      ) : (
        <div
          className={`file-preview-text ${fullMarkdown ? "is-markdown assistant-markdown" : ""}`}
          ref={scroller}
          onScroll={(event) => {
            onVisitChange?.({
              position: {
                top: event.currentTarget.scrollTop,
                left: event.currentTarget.scrollLeft,
              },
            });
          }}
        >
          {!page.text ? (
            <p className="file-preview-empty">空文件</p>
          ) : fullMarkdown ? (
            <MarkdownBody body={page.text} onNotify={onNotify} />
          ) : (
            <div className="file-preview-code">
              <pre className="file-preview-line-numbers" aria-hidden="true">
                {page.text
                  .split("\n")
                  .map((_, index) => index + 1)
                  .join("\n")}
              </pre>
              <pre>
                <code>{page.text}</code>
              </pre>
            </div>
          )}
        </div>
      )}
      {(cursor > 0 || page?.next_cursor != null) && (
        <footer
          className="file-preview-pagebar file-preview-pagination"
          aria-label="文件分页"
        >
          {file.kind === "markdown" && !source && (
            <small>大文档以源文本分页显示</small>
          )}
          {cursor > 0 && <button onClick={() => move(0)}>回到开头</button>}
          <span className="file-preview-page-position">
            {page
              ? page.mode === "table"
                ? `从第 ${page.cursor + 1} 条记录起`
                : `从字节 ${page.cursor} 起`
              : ""}
          </span>
          {page?.next_cursor != null && (
            <button onClick={() => move(page.next_cursor!)}>下一页 →</button>
          )}
        </footer>
      )}
    </div>
  );
}

function PreviewTable({
  rows,
  cursor,
  visit,
  onVisitChange,
}: {
  rows: string[][];
  cursor: number;
  visit?: Visit;
  onVisitChange?: (patch: Partial<Visit>) => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  // Segment horizontal scrolling to stay within browser layout-coordinate limits.
  // Every column remains reachable; this is a display window, never a data cap.
  const columnWindow = 1024;
  const [columnPage, setColumnPage] = useState(visit?.columnPage ?? 0);
  const [position, setPosition] = useState({
    top: visit?.position.top ?? 0,
    left: visit?.position.left ?? 0,
    width: 900,
    height: 600,
  });
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    element.scrollTop = visit?.position.top ?? 0;
    element.scrollLeft = visit?.position.left ?? 0;
    const resize = new ResizeObserver(() =>
      setPosition((value) => ({
        ...value,
        width: element.clientWidth,
        height: element.clientHeight,
      })),
    );
    resize.observe(element);
    return () => resize.disconnect();
  }, []);
  // Window both axes: a CSV record with many empty columns must not create
  // hundreds of thousands of cells even though its byte page is small.
  const columns = rows.reduce((n, row) => Math.max(n, row.length), 0);
  const columnStart =
    Math.min(columnPage, Math.max(0, Math.ceil(columns / columnWindow) - 1)) *
    columnWindow;
  const columnCount = Math.min(columnWindow, columns - columnStart);
  const firstRow = Math.max(0, Math.floor(position.top / 34) - 2);
  const lastRow = Math.min(
    rows.length,
    firstRow + Math.ceil(position.height / 34) + 4,
  );
  const firstCol =
    columnStart + Math.max(0, Math.floor(position.left / 180) - 1);
  const lastCol = Math.min(
    columnStart + columnCount,
    firstCol + Math.ceil(position.width / 180) + 2,
  );
  return (
    <>
      {columns > columnWindow && (
        <div className="file-preview-pagebar">
          <label>
            列分组{" "}
            <input
              aria-label="列分组"
              type="number"
              min={1}
              max={Math.ceil(columns / columnWindow)}
              value={columnStart / columnWindow + 1}
              onChange={(event) => {
                const next = Number(event.target.value);
                if (
                  Number.isInteger(next) &&
                  next > 0 &&
                  next <= Math.ceil(columns / columnWindow)
                ) {
                  setColumnPage(next - 1);
                  onVisitChange?.({
                    columnPage: next - 1,
                    position: { top: position.top, left: 0 },
                  });
                  setPosition((value) => ({ ...value, left: 0 }));
                  if (viewport.current) viewport.current.scrollLeft = 0;
                }
              }}
            />
          </label>
          <span>
            第 {columnStart + 1}–{columnStart + columnCount} 列，共 {columns} 列
          </span>
        </div>
      )}
      <div
        className="file-preview-table"
        ref={viewport}
        role="table"
        aria-label="文件表格"
        aria-rowcount={rows.length}
        aria-colcount={columns}
        onScroll={(event) => {
          // React clears currentTarget after dispatch; batched updates run later.
          const { scrollTop: top, scrollLeft: left } = event.currentTarget;
          setPosition((value) => ({ ...value, top, left }));
          onVisitChange?.({ position: { top, left } });
        }}
      >
        {rows.length === 0 ? (
          <p>空表格</p>
        ) : (
          <div
            style={{
              width: columnCount * 180,
              height: rows.length * 34,
              position: "relative",
            }}
          >
            {rows.slice(firstRow, lastRow).map((row, offset) => (
              <div
                role="row"
                aria-rowindex={firstRow + offset + 1}
                key={firstRow + offset}
                style={{ top: (firstRow + offset) * 34 }}
              >
                {Array.from(
                  { length: Math.max(0, lastCol - firstCol) },
                  (_, index) => firstCol + index,
                ).map((column) => (
                  <div
                    role={
                      cursor === 0 && firstRow + offset === 0
                        ? "columnheader"
                        : "cell"
                    }
                    aria-colindex={column + 1}
                    key={column}
                    style={{ left: (column - columnStart) * 180 }}
                    title={row[column] ?? ""}
                  >
                    {row[column] ?? ""}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
