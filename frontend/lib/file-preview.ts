export interface FilePreview {
  read_token: string;
  path: string;
  reading_root: string;
  name: string;
  kind:
    | "text"
    | "markdown"
    | "table"
    | "image"
    | "svg"
    | "pdf"
    | "html"
    | "file"
    | "directory";
  size: number | null;
  notice: string | null;
  content_url: string | null;
  document_url: string | null;
  images_url: string | null;
  can_open: boolean;
}
export interface FilePreviewPage {
  mode: "text" | "table";
  text?: string;
  rows?: string[][];
  cursor: number;
  next_cursor: number | null;
}
export interface FilePreviewApi {
  open(
    path: string,
    basePreview?: string,
    readingRoot?: string,
  ): Promise<FilePreview>;
  close(token: string): Promise<void>;
  page(
    token: string,
    cursor: number,
    mode: "text" | "table",
    signal: AbortSignal,
  ): Promise<FilePreviewPage>;
  action(token: string, action: "reveal" | "open"): Promise<void>;
}
export function classifyFileLink(
  href: string,
): "local" | "external" | "anchor" | "invalid" {
  // eslint-disable-next-line no-control-regex -- Reject control characters in user-supplied paths.
  if (!href || /[\u0000-\u001f\u007f\\]/.test(href)) return "invalid";
  if (href.startsWith("#")) return "anchor";
  if (/^(https?:|mailto:|\/\/)/i.test(href)) return "external";
  const scheme = /^([a-z][a-z\d+.-]*):/i.exec(href)?.[1]?.toLowerCase();
  if (scheme && scheme !== "file") return "invalid";
  try {
    if (scheme === "file") {
      const url = new URL(href);
      if (url.hostname && url.hostname !== "localhost") return "invalid";
    }
    const raw = decodeURIComponent(href.split(/[?#]/)[0]);
    // eslint-disable-next-line no-control-regex -- Reject control characters in user-supplied paths.
    if (/[\u0000-\u001f\u007f\\]/.test(raw)) return "invalid";
  } catch {
    return "invalid";
  }
  return "local";
}
export function markdownImageUrl(
  href: string,
  base: string,
): string | undefined {
  if (classifyFileLink(href) !== "local" || /^([a-z]+:|\/|~)/i.test(href))
    return undefined;
  try {
    const root = base.slice(0, base.indexOf("/images/") + "/images/".length);
    const resolved = new URL(href, `https://preview.invalid${base}`);
    if (!resolved.pathname.startsWith(root)) return undefined;
    const relative = decodeURIComponent(resolved.pathname.slice(root.length));
    if (
      relative
        .split("/")
        .some((part) => !part || part.startsWith(".") || part.includes("\\"))
    )
      return undefined;
    return resolved.pathname;
  } catch {
    return undefined;
  }
}
