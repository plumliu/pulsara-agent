import type { ReactElement } from "react";
import {
  DesktopWorkspace,
  DesktopWorkspaceProvider,
} from "../../frontend/components/desktop-workspace";
import {
  act,
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { FilePreviewProvider } from "../../frontend/components/file-browser";
import { MarkdownBody } from "../../frontend/components/markdown-body";
import type {
  FilePreview,
  FilePreviewApi,
} from "../../frontend/lib/file-preview";
import {
  classifyFileLink,
  markdownImageUrl,
} from "../../frontend/lib/file-preview";
import { htmlPreviewShell } from "../../frontend/components/sandboxed-html-preview";

function Harness({
  children,
}: {
  children: ReactElement<{ ownerKey?: string; sessionId?: string }>;
}) {
  return (
    <DesktopWorkspaceProvider
      sessionId={children.props.sessionId ?? children.props.ownerKey ?? "one"}
      onShow={() => {}}
    >
      {children}
      <DesktopWorkspace
        isOpen
        onClose={() => {}}
        toolbar={null}
        renderTab={() => null}
      />
    </DesktopWorkspaceProvider>
  );
}
function render(ui: ReactElement<{ ownerKey?: string; sessionId?: string }>) {
  const result = rtlRender(<Harness>{ui}</Harness>);
  return {
    ...result,
    rerender: (next: typeof ui) => result.rerender(<Harness>{next}</Harness>),
  };
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  HTMLElement.prototype.scrollTo = vi.fn();
});
afterEach(() => {
  cleanup();
  delete window.pulsaraDesktop;
  vi.unstubAllGlobals();
});
const notify = vi.fn();
const file = (name: string): FilePreview => ({
  read_token: name,
  name,
  path: `/project/${name}`,
  reading_root: "/project",
  kind: "text",
  size: 5,
  notice: null,
  content_url: `/api/file-previews/${name}/content`,
  document_url: null,
  images_url: null,
  can_open: false,
});
function api(): FilePreviewApi {
  return {
    open: vi.fn(async (path) => file(path)),
    close: vi.fn(async () => undefined),
    action: vi.fn(async () => undefined),
    page: vi.fn(async () => ({
      mode: "text" as const,
      text: "hello",
      cursor: 0,
      next_cursor: null,
    })),
  };
}
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

it("keeps file actions in one navigation row and copies the actual native path", async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({ ...file("SKILL.md"), kind: "markdown" });
  const copy = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { writeClipboardText: copy } as unknown as import('../../frontend/lib/desktop-host').PulsaraDesktop;
  const view = render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[手册](SKILL.md)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole('link', { name: '手册' }));
  await screen.findByRole('button', { name: '复制路径' });
  const row = screen.getByRole('navigation', { name: '文件操作' });
  expect(within(row).getAllByRole('button').map(button => button.getAttribute('aria-label')))
    .toEqual(['后退', '前进', '刷新文件', '查看源码', '复制路径', '打开所在文件夹']);
  expect(view.container.querySelector('.file-preview-title')).toBeNull();
  fireEvent.click(within(row).getByRole('button', { name: '复制路径' }));
  await waitFor(() => expect(copy).toHaveBeenCalledExactlyOnceWith('/project/SKILL.md'));
  fireEvent.click(within(row).getByRole('button', { name: '打开所在文件夹' }));
  await waitFor(() => expect(reader.action).toHaveBeenCalledExactlyOnceWith('SKILL.md', 'reveal'));
  fireEvent.click(screen.getByRole('button', { name: '关闭 SKILL.md' }));
  expect(reader.close).toHaveBeenCalledWith('SKILL.md');
});

it("classifies local links without granting scripts or foreign file hosts", () => {
  for (const value of [
    "a/b.csv",
    "/tmp/a.md",
    "~/notes.md",
    "file:///tmp/a%20b.html",
  ])
    expect(classifyFileLink(value)).toBe("local");
  for (const value of [
    "javascript:x",
    "data:text/html,hi",
    "sandbox:/a",
    "file://evil/a",
    "%00",
    "%zz",
    "a\\b",
  ])
    expect(classifyFileLink(value)).toBe("invalid");
  expect(classifyFileLink("https://example.com/a")).toBe("external");
  expect(markdownImageUrl("../secret.png", "/images/")).toBeUndefined();
  expect(markdownImageUrl("./plots/中文.png", "/images/")).toBe(
    "/images/plots/%E4%B8%AD%E6%96%87.png",
  );
});

it("intercepts file links and renders the original text while keeping external links", async () => {
  const reader = api();
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody
        body="[报告](report.txt) [官网](https://example.com) [坏地址](javascript:alert)"
        onNotify={notify}
      />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "报告" }), {
    ctrlKey: true,
  });
  await screen.findByText("hello");
  expect(reader.open).toHaveBeenCalledWith("report.txt", undefined, undefined);
  expect(
    screen.getByRole("link", { name: "官网" }).getAttribute("target"),
  ).toBe("_blank");
  expect(
    screen.getByRole("link", { name: "坏地址" }).getAttribute("href"),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "关闭 report.txt" }));
  await waitFor(() => expect(reader.close).toHaveBeenCalledWith("report.txt"));
});

it("opens independent reads concurrently and closes only the selected tab", async () => {
  const reader = api();
  const first = deferred<FilePreview>();
  vi.mocked(reader.open).mockImplementationOnce(() => first.promise);
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[甲](a.txt) [乙](b.txt)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "甲" }));
  await waitFor(() => expect(reader.open).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("link", { name: "乙" }));
  expect(reader.open).toHaveBeenCalledTimes(2);
  await act(async () => first.resolve(file("a.txt")));
  await screen.findByText("hello");
  expect(reader.close).not.toHaveBeenCalled();
  expect(reader.open).toHaveBeenLastCalledWith("b.txt", undefined, undefined);
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "关闭 b.txt" }));
  expect(reader.close).toHaveBeenCalledWith("b.txt");
  expect(reader.close).not.toHaveBeenCalledWith("a.txt");
});

it("switches owners without remounting conversation children or accepting old results", async () => {
  const a = api(),
    b = api(),
    pending = deferred<FilePreview>();
  vi.mocked(a.open).mockReturnValue(pending.promise);
  function Child() {
    return <input defaultValue="draft" aria-label="unchanged composer" />;
  }
  const body = (
    <>
      <Child />
      <MarkdownBody body="[报告](a.txt)" onNotify={notify} />
    </>
  );
  const view = render(
    <FilePreviewProvider api={a} ownerKey="one" onNotify={notify}>
      {body}
    </FilePreviewProvider>,
  );
  const editor = screen.getByLabelText("unchanged composer");
  fireEvent.click(screen.getByRole("link", { name: "报告" }));
  await waitFor(() => expect(a.open).toHaveBeenCalled());
  view.rerender(
    <FilePreviewProvider api={b} ownerKey="two" onNotify={notify}>
      {body}
    </FilePreviewProvider>,
  );
  await act(async () => pending.resolve(file("a.txt")));
  expect(screen.getByLabelText("unchanged composer")).toBe(editor);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(a.close).toHaveBeenCalledWith("a.txt");
  expect(b.close).not.toHaveBeenCalled();
});

it("shares a restricted HTML shell without letting content break into its trusted script", () => {
  const shell = htmlPreviewShell({
    html: "</script><script>parent.escaped=true</script>",
    measure: true,
  });
  const parsed = new DOMParser().parseFromString(shell, "text/html");
  expect(parsed.querySelectorAll("script")).toHaveLength(1);
  expect(parsed.querySelector("iframe")?.getAttribute("sandbox")).toBe(
    "allow-scripts",
  );
  expect(parsed.querySelector("meta")?.getAttribute("content")).toContain(
    "frame-src 'none'",
  );
  expect(parsed.querySelector("meta")?.getAttribute("content")).toContain(
    "script-src 'unsafe-inline' data:",
  );
  const local = new DOMParser().parseFromString(
    htmlPreviewShell({ url: "/api/file-previews/test/resources/index.html" }),
    "text/html",
  );
  expect(local.querySelector("meta")?.getAttribute("content")).not.toContain(
    "script-src 'unsafe-inline' data:",
  );
  expect(() => htmlPreviewShell({ url: "https://evil.example/a" })).toThrow();
});

it("switches Markdown source without losing the loaded page", async () => {
  const reader = api();
  const text = '---\nname: skill-example\ndescription: Read this document\n---\n# Heading';
  vi.mocked(reader.open).mockResolvedValue({
    ...file("report.md"),
    kind: "markdown",
  });
  vi.mocked(reader.page).mockResolvedValue({
    mode: "text",
    text,
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[报告](report.md)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "报告" }));
  await screen.findByRole("heading", { name: "Heading" });
  expect(screen.getByRole('region', { name: '文档属性' }).textContent).toContain('skill-example');
  fireEvent.click(screen.getByRole("button", { name: "查看源码" }));
  await waitFor(() => expect(document.querySelector('.file-preview-code code')?.textContent).toBe(text));
  expect(screen.queryByRole('region', { name: '文档属性' })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "显示预览" }));
  await screen.findByRole("heading", { name: "Heading" });
  expect(reader.page).toHaveBeenCalledTimes(1);
});

it('highlights code one preview page at a time without fetching the whole file', async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue(file('main.py'));
  const first = 'def greet():\n    return "hello';
  const second = ' world"\n\nprint(42)\n';
  vi.mocked(reader.page).mockImplementation(async (_token, cursor) => ({
    mode: 'text', text: cursor === 0 ? first : second,
    cursor, next_cursor: cursor === 0 ? 30 : null,
  }));
  const view = render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
    <MarkdownBody body="[代码](main.py)" onNotify={notify} />
  </FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', { name: '代码' }));
  await waitFor(() => expect(view.container.querySelector('.file-preview-code code')?.textContent).toBe(first));
  await waitFor(() => expect(view.container.querySelector('.file-preview-code [class*="code-card__token--"]')).toBeTruthy());
  expect(reader.page).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '下一页 →' }));
  await waitFor(() => expect(view.container.querySelector('.file-preview-code code')?.textContent).toBe(second));
  expect(reader.page).toHaveBeenCalledTimes(2);
  expect(vi.mocked(reader.page).mock.calls.map(call => call[1])).toEqual([0, 30]);
});

it("shows HTML source as text while preserving the preview frame", async () => {
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({
    ...file("plot.html"),
    kind: "html",
    document_url: "/api/file-previews/preview/resources/plot.html",
  });
  const html = "<h1>Raw heading</h1><script>window.injected=true</script>";
  vi.mocked(reader.page).mockResolvedValue({
    mode: "text",
    text: html,
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[图表](plot.html)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "图表" }));
  const toggle = await screen.findByRole("button", { name: "查看源码" });
  expect(toggle.closest("nav")?.getAttribute("aria-label")).toBe("文件操作");
  const frame = document.querySelector('iframe[title="plot.html"]')!;
  fireEvent.click(toggle);
  await screen.findByText(html);
  expect(screen.queryByRole("heading", { name: "Raw heading" })).toBeNull();
  expect(frame.parentElement?.hidden).toBe(true);
  expect(screen.queryByLabelText("文件分页")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "显示预览" }));
  expect(document.querySelector('iframe[title="plot.html"]')!).toBe(frame);
  expect(frame.parentElement?.hidden).toBe(false);
});

it("keeps CSV pagination available and restarts at row zero after a source toggle", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({
    ...file("rows.csv"),
    kind: "table",
  });
  vi.mocked(reader.page).mockImplementation(async (_, cursor, mode) =>
    mode === "table"
      ? {
          mode,
          rows: [[cursor === 0 ? "name" : "next record"]],
          cursor,
          next_cursor: cursor === 0 ? 1 : null,
        }
      : { mode, text: "name\nnext record", cursor, next_cursor: null },
  );
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[表格](rows.csv)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "表格" }));
  await screen.findByRole("columnheader", { name: "name" });
  fireEvent.click(screen.getByRole("button", { name: "下一页 →" }));
  await screen.findByRole("cell", { name: "next record" });
  expect(screen.getByLabelText("文件分页").tagName).toBe("FOOTER");
  fireEvent.click(screen.getByRole("button", { name: "查看源码" }));
  await screen.findByText("name next record");
  expect(screen.queryByLabelText("文件分页")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "显示预览" }));
  await screen.findByRole("columnheader", { name: "name" });
  expect(
    vi.mocked(reader.page).mock.calls.map(([, cursor, mode]) => [cursor, mode]),
  ).toEqual([
    [0, "table"],
    [1, "table"],
    [0, "text"],
    [0, "table"],
  ]);
});

it("retries a missing Markdown sibling using its original resolved target", async () => {
  const reader = api();
  vi.mocked(reader.open)
    .mockResolvedValueOnce({
      ...file("report.md"),
      path: "/project/reports/report.md",
      kind: "markdown",
    })
    .mockRejectedValueOnce(new Error("文件不存在"))
    .mockResolvedValueOnce(file("new.txt"));
  vi.mocked(reader.page).mockResolvedValueOnce({
    mode: "text",
    text: "[下一份](sub/new.txt)",
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[报告](report.md)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "报告" }));
  fireEvent.click(await screen.findByRole("link", { name: "下一份" }));
  await screen.findByText("文件不存在");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("hello");
  expect(reader.open).toHaveBeenLastCalledWith(
    "file:///project/reports/sub/new.txt",
    "report.md",
    "/project",
  );
});

it("keeps the final columns reachable without an oversized scroll surface", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({
    ...file("wide.csv"),
    kind: "table",
  });
  const row = Array.from({ length: 200001 }, (_, index) =>
    index === 199680 ? "final group" : "",
  );
  vi.mocked(reader.page).mockResolvedValue({
    mode: "table",
    rows: [row],
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[表格](wide.csv)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "表格" }));
  fireEvent.change(await screen.findByLabelText("列分组"), {
    target: { value: "196" },
  });
  await screen.findByText("final group");
  expect(
    screen.getByRole("table").querySelectorAll("[role=columnheader]").length,
  ).toBeLessThan(20);
  vi.unstubAllGlobals();
});

it("keeps the conversation mounted during consecutive table scroll events", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const reader = api();
  vi.mocked(reader.open).mockResolvedValue({
    ...file("wide.csv"),
    kind: "table",
  });
  const rows = Array.from({ length: 80 }, (_, r) =>
    Array.from({ length: 40 }, (_, c) => `r${r}c${c}`),
  );
  vi.mocked(reader.page).mockResolvedValue({
    mode: "table",
    rows,
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <p>当前会话</p>
      <MarkdownBody body="[表格](wide.csv)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "表格" }));
  const table = await screen.findByRole("table");
  // Scroll updates may be batched; React clears currentTarget after dispatch.
  act(() => {
    fireEvent.scroll(table, { target: { scrollLeft: 180, scrollTop: 34 } });
    fireEvent.scroll(table, { target: { scrollLeft: 1800, scrollTop: 340 } });
  });
  expect(await screen.findByText("r10c10")).toBeTruthy();
  expect(screen.queryByText("r0c0")).toBeNull();
  expect(screen.getByText("当前会话")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭 wide.csv" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("当前会话")).toBeTruthy();
});

it("navigates HTML to HTML to Markdown in one instance with its inherited root", async () => {
  const reader = api();
  vi.mocked(reader.open).mockImplementation(async (path) => {
    const pathname = new URL(path, "file:///project/").pathname;
    const name = pathname.split("/").pop()!;
    const html = name.endsWith(".html");
    return {
      ...file(name),
      path: pathname,
      kind: html ? "html" : "markdown",
      read_token: pathname.includes("learner") ? "child" : "entry",
      document_url: html
        ? `/api/file-previews/${pathname.includes("learner") ? "child" : "entry"}/resources/${pathname.slice("/project/".length)}`
        : null,
    };
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[入口](index.html)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "入口" }));
  await waitFor(() => expect(document.querySelector("iframe")).not.toBeNull());
  const navigate = (token: string, path: string) =>
    fireEvent(
      window,
      new MessageEvent("message", {
        source: document.querySelector("iframe")!.contentWindow,
        data: {
          type: "pulsara-preview-navigation",
          token,
          href: `${window.location.origin}/api/file-previews/${token}/resources/${path}`,
        },
      }),
    );
  navigate("entry", "learner/index.html");
  await waitFor(() =>
    expect(reader.open).toHaveBeenLastCalledWith(
      "file:///project/learner/index.html",
      "entry",
      "/project",
    ),
  );
  await waitFor(() => expect(reader.close).toHaveBeenCalledWith("entry"));
  await waitFor(() => expect(document.querySelector("iframe")?.srcdoc).toContain("/child/resources/learner/index.html"));
  navigate("child", "learner/role_manuals/ACCESS-07.md");
  await screen.findByText("hello");
  expect(reader.open).toHaveBeenLastCalledWith(
    "file:///project/learner/role_manuals/ACCESS-07.md",
    "child",
    "/project",
  );
  expect(screen.getAllByRole("tab")).toHaveLength(1);
  expect(screen.getByRole("tab").textContent).toContain("ACCESS-07.md");
});

it("keeps new-navigation failures off history and retries historical failures in place", async () => {
  const reader = api();
  let broken = false;
  vi.mocked(reader.open).mockImplementation(async (path) => {
    const name = new URL(path, "file:///project/").pathname.split("/").pop()!;
    if (name === "missing.md" || (name === "a.md" && broken))
      throw new Error("FILE_NOT_FOUND");
    return { ...file(name), kind: "markdown" };
  });
  vi.mocked(reader.page).mockResolvedValue({
    mode: "text",
    text: "[乙](b.md) [缺失](missing.md)",
    cursor: 0,
    next_cursor: null,
  });
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[甲](a.md)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "甲" }));
  fireEvent.click(await screen.findByRole("link", { name: "缺失" }));
  await screen.findByText("FILE_NOT_FOUND");
  expect(
    (screen.getByRole("button", { name: "后退" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(screen.getByRole("tab").textContent).toContain("a.md");
  fireEvent.click(screen.getByRole("link", { name: "乙" }));
  await waitFor(() =>
    expect(screen.getByRole("tab").textContent).toContain("b.md"),
  );
  broken = true;
  fireEvent.click(screen.getByRole("button", { name: "后退" }));
  await screen.findByText("FILE_NOT_FOUND");
  expect(screen.getByRole("tab").textContent).toContain("a.md");
  expect(
    (screen.getByRole("button", { name: "前进" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
  broken = false;
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("link", { name: "乙" });
  expect(
    (screen.getByRole("button", { name: "后退" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "前进" }));
  await waitFor(() =>
    expect(screen.getByRole("tab").textContent).toContain("b.md"),
  );
});

it("deduplicates only current pages and allows explicit independent copies", async () => {
  const reader = api();
  render(
    <FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
      <MarkdownBody body="[甲](a.txt)" onNotify={notify} />
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "甲" }));
  await screen.findByText("hello");
  fireEvent.click(screen.getByRole("link", { name: "甲" }));
  await waitFor(() => expect(reader.open).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(1));
  fireEvent.click(screen.getByRole("link", { name: "甲" }), { ctrlKey: true });
  await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(2));
});

it("reopens the current child using the original root after a session round trip", async () => {
  const a = api(),
    b = api();
  vi.mocked(a.open).mockResolvedValue({
    ...file("index.html"),
    path: "/project/site/index.html",
    reading_root: "/project/site",
    kind: "markdown",
  });
  vi.mocked(a.page).mockResolvedValue({
    mode: "text",
    text: "[Child](nested/manual.md)",
    cursor: 0,
    next_cursor: null,
  });
  const contents = (
    <MarkdownBody body="[Entry](site/index.html)" onNotify={notify} />
  );
  const view = render(
    <FilePreviewProvider
      api={a}
      ownerKey="connection-a"
      sessionId="one"
      onNotify={notify}
    >
      {contents}
    </FilePreviewProvider>,
  );
  fireEvent.click(screen.getByRole("link", { name: "Entry" }));
  await screen.findByRole("link", { name: "Child" });
  vi.mocked(a.open).mockResolvedValue({
    ...file("manual.md"),
    path: "/project/site/nested/manual.md",
    reading_root: "/project/site",
    kind: "markdown",
  });
  fireEvent.click(screen.getByRole("link", { name: "Child" }));
  await waitFor(() =>
    expect(screen.getByRole("tab").textContent).toContain("manual.md"),
  );
  view.rerender(
    <FilePreviewProvider
      api={b}
      ownerKey="connection-b"
      sessionId="two"
      onNotify={notify}
    >
      {contents}
    </FilePreviewProvider>,
  );
  expect(screen.queryByRole("tab")).toBeNull();
  view.rerender(
    <FilePreviewProvider
      api={a}
      ownerKey="connection-a-new"
      sessionId="one"
      onNotify={notify}
    >
      {contents}
    </FilePreviewProvider>,
  );
  await waitFor(() =>
    expect(a.open).toHaveBeenLastCalledWith(
      "file:///project/site/nested/manual.md",
      undefined,
      "/project/site",
    ),
  );
  expect(screen.getAllByRole("tab")).toHaveLength(1);
});

it('preserves file URL fragments on a new view and a deduplicated view', async () => {
  // jsdom lacks CSS.escape; these fixture anchors contain only ASCII letters.
  vi.stubGlobal('CSS', { ...CSS, escape: (value: string) => value });
  const reader=api();
  vi.mocked(reader.open).mockResolvedValue({...file('anchor.md'),kind:'markdown'});
  vi.mocked(reader.page).mockResolvedValue({mode:'text',text:'# First\n\n# Second',cursor:0,next_cursor:null});
  const scrolled:string[]=[];
  const original=HTMLElement.prototype.scrollIntoView;
  HTMLElement.prototype.scrollIntoView=function(){scrolled.push(this.id);};
  try {
    render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}><MarkdownBody body={'[first](file:///project/anchor.md#first) [second](file:///project/anchor.md#second)'} onNotify={notify}/></FilePreviewProvider>);
    fireEvent.click(screen.getByRole('link',{name:'first'}));
    await waitFor(()=>expect(scrolled).toContain('first'));
    fireEvent.click(screen.getByRole('link',{name:'second'}));
    await waitFor(()=>expect(scrolled).toContain('second'));
    expect(screen.getAllByRole('tab')).toHaveLength(1);expect(reader.close).toHaveBeenCalledOnce();
  } finally {HTMLElement.prototype.scrollIntoView=original;}
});

it.each([
  ['/Users/demo/网店经营数据/促销复盘看板.html', 'file:///Users/demo/%E7%BD%91%E5%BA%97%E7%BB%8F%E8%90%A5%E6%95%B0%E6%8D%AE/%E4%BF%83%E9%94%80%E5%A4%8D%E7%9B%98%E7%9C%8B%E6%9D%BF.html'],
  ['/tmp/%E5%9B%BE%20one.html#section', 'file:///tmp/%E5%9B%BE%20one.html#section'],
  ['/tmp/100%25.html', 'file:///tmp/100%25.html'],
])('opens Markdown URL %s without encoding it as a native path again', async (href, expected) => {
  vi.stubGlobal('CSS', {escape: (value: string) => value});
  const reader = api();
  render(<FilePreviewProvider api={reader} ownerKey="one" onNotify={notify}>
    <MarkdownBody body={`[看板](${href})`} onNotify={notify} />
  </FilePreviewProvider>);
  fireEvent.click(screen.getByRole('link', {name:'看板'}));
  await waitFor(() => expect(reader.open).toHaveBeenCalledWith(expected, undefined, undefined));
});
