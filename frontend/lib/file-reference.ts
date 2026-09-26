/** File references remain ordinary text in the canonical prompt and provider input. */
export interface ImportedPath {
  path: string;
  name: string;
  bytes: number;
  file_count: number;
}

export interface FileReference {
  kind: 'file' | 'directory';
  path: string;
  name: string;
  raw: string;
}

export type ImportFiles = (sessionId: string, files: readonly File[], directory: boolean,
  signal: AbortSignal) => Promise<ImportedPath>;

export interface WorkspacePathCandidate {
  name: string;
  path: string;
  relative_path: string;
  kind: 'file' | 'directory';
}

export interface WorkspacePathPage {
  directory: string;
  items: WorkspacePathCandidate[];
  next_cursor: string | null;
}

export type CompleteWorkspacePaths = (prefix: string, cursor: string | null, signal: AbortSignal) => Promise<WorkspacePathPage>;

export function formatWorkspaceReference(value: Pick<WorkspacePathCandidate, 'kind' | 'path'>): string {
  return `【本地${value.kind === 'directory' ? '文件夹' : '文件'}：${JSON.stringify(value.path)}】`;
}

export function formatFileReference(value: ImportedPath, directory: boolean): string {
  return directory
    ? `【本地文件夹（只读副本，${value.file_count} 个文件）：${JSON.stringify(value.path)}】`
    : `【本地文件（只读副本，${value.bytes} bytes）：${JSON.stringify(value.path)}】`;
}

export function splitFileReferences(text: string): Array<string | FileReference> {
  const pattern = /【本地(文件夹|文件)(?:（只读副本，(\d+) (个文件|bytes)）)?：("(?:\\.|[^"\\])*?")】/g;
  const result: Array<string | FileReference> = [];
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match[2] !== undefined && ((match[1] === '文件夹') !== (match[3] === '个文件')
      || !Number.isSafeInteger(Number(match[2])))) continue;
    let path: unknown;
    try { path = JSON.parse(match[4]); } catch { continue; }
    if (typeof path !== 'string' || !/^(\/|[A-Za-z]:[\\/]|\\\\)/.test(path)
      || /[\u0000-\u001f\u007f]/.test(path)) continue;
    const name = path.split(/[\\/]/).filter(Boolean).at(-1);
    if (!name) continue;
    if (match.index > offset) result.push(text.slice(offset, match.index));
    result.push({ kind: match[1] === '文件夹' ? 'directory' : 'file', path, name, raw: match[0] });
    offset = match.index + match[0].length;
  }
  if (offset < text.length) result.push(text.slice(offset));
  return result;
}

export function fileReference(raw: string): FileReference | undefined {
  const parts = splitFileReferences(raw);
  return parts.length === 1 && typeof parts[0] !== 'string' ? parts[0] : undefined;
}

export function fileAppearance(name: string, directory: boolean): { type: string; label: string } {
  if (directory) return { type: 'directory', label: '目录' };
  const extension = name.split('.').at(-1)?.toLowerCase();
  if (extension === 'pdf') return { type: 'pdf', label: 'PDF' };
  if (extension === 'doc' || extension === 'docx') return { type: 'word', label: 'DOC' };
  if (extension === 'ppt' || extension === 'pptx') return { type: 'slides', label: 'PPT' };
  if (extension === 'xls' || extension === 'xlsx') return { type: 'sheet', label: 'XLS' };
  return { type: 'file', label: '文件' };
}
