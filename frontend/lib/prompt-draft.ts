import { closeHistory } from '@tiptap/pm/history';
import { Editor, type JSONContent } from '@tiptap/core';
import { PromptAnnotationNode } from '../components/prompt-annotation-node';
import type { PromptAnnotationPart } from './prompt-content';
import { copyAnnotation, decodeAnnotation } from './prompt-content';
import Document from '@tiptap/extension-document';
import HardBreak from '@tiptap/extension-hard-break';
import Paragraph from '@tiptap/extension-paragraph';
import Text from '@tiptap/extension-text';
import { UndoRedo } from '@tiptap/extensions';
import { FileReferenceNode } from '../components/file-reference-node';
import { PromptImageNode } from '../components/prompt-image-node';
import { SkillReferenceNode } from '../components/skill-reference-node';
import { fileReference, formatFileReference, splitFileReferences, type ImportFiles } from './file-reference';
import { skillReference, splitSkillReferences } from './skill-reference';
import type { EditablePromptContent, LocalPromptImagePart } from './prompt-content';

const acceptedImageMediaTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);

const SingleParagraphDocument = Document.extend({
  content: 'annotation* paragraph',
});

interface DraftAsset {
  id: string;
  blob: Blob;
  declaredMediaType: string;
  objectUrl: string;
  bytes?: Uint8Array;
  error?: string;
  ready: Promise<void>;
}

interface DraftSession {
  editor: Editor;
  assets: Map<string, DraftAsset>;
  imports: Map<string, DraftImport>;
  revision: number;
}

interface DraftImport {
  id: string;
  files: readonly File[];
  directory: boolean;
  controller: AbortController;
  raw: string;
  error?: string;
}

export interface PromptDraftSnapshot {
  readonly owner: object;
  revision: number;
  content: EditablePromptContent;
}

export interface PromptDraftSummary {
  revision: number;
  text: string;
  hasContent: boolean;
  hasImage: boolean;
  pendingImages: number;
  failedImages: number;
  pendingFiles: number;
  failedFiles: number;
}

export interface PromptDraftImageStatus {
  assetId: string;
  state: 'loading' | 'ready' | 'failed';
  reason?: string;
}

export interface DraftAnnotation {
  id: string;
  position: number;
  number: number;
  value: PromptAnnotationPart;
}

export class PromptDraftStore {
  private readonly sessions = new Map<string, DraftSession>();
  private readonly listeners = new Set<() => void>();
  private version = 0;
  private importer?: ImportFiles;

  setImporter(importer: ImportFiles): void { this.importer = importer; }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getVersion = (): number => this.version;

  getEditor(sessionId: string): Editor {
    if (!sessionId) throw new Error('草稿缺少会话身份。');
    const current = this.sessions.get(sessionId);
    if (current) return current.editor;
    if (typeof document === 'undefined') {
      throw new Error('编辑器只能在浏览器中创建。');
    }
    const session: DraftSession = {
      editor: undefined as unknown as Editor,
      assets: new Map(),
      imports: new Map(),
      revision: 0,
    };
    session.editor = this.createEditor(session);
    this.sessions.set(sessionId, session);
    return session.editor;
  }

  summary(sessionId: string): PromptDraftSummary {
    const session = this.sessions.get(sessionId);
    if (!session) {
      return {
        revision: 0,
        text: '',
        hasContent: false,
        hasImage: false,
        pendingImages: 0,
        failedImages: 0,
        pendingFiles: 0,
        failedFiles: 0,
      };
    }
    const document: JSONContent = session.editor.getJSON();
    const text = draftText(document);
    const assetIds = imageAssetIds(document);
    const references = (document.content?.find(node => node.type === 'paragraph')?.content ?? []).filter(node => node.type === 'fileReference');
    return {
      revision: session.revision,
      text,
      hasContent: Boolean(document.content?.some(node => node.type === 'annotation')) || references.length > 0 || assetIds.length > 0 || text.trim().length > 0,
      hasImage: assetIds.length > 0,
      pendingImages: assetIds.filter((id) => !session.assets.get(id)?.bytes
        && !session.assets.get(id)?.error).length,
      failedImages: assetIds.filter((id) => Boolean(session.assets.get(id)?.error)).length,
      pendingFiles: references.filter(node => node.attrs?.state === 'uploading').length,
      failedFiles: references.filter(node => node.attrs?.state === 'failed').length,
    };
  }

  annotations(sessionId: string): DraftAnnotation[] {
    const result: DraftAnnotation[] = [];
    this.sessions.get(sessionId)?.editor.state.doc.forEach((node, position) => {
      if (node.type.name === 'annotation') result.push({ id: node.attrs.draftId, position, number: result.length + 1, value: node.attrs.value });
    });
    return result;
  }

  addAnnotation(sessionId: string, annotation: PromptAnnotationPart): string {
    const editor = this.getEditor(sessionId);
    let position = 0;
    editor.state.doc.forEach(node => { if (node.type.name === 'annotation') position += node.nodeSize; });
    // Only identifies a draft node across edits, undo and duplicate quotes.
    // Canonical serialization carries value alone, never this UI identity.
    const id = crypto.randomUUID();
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.commands.insertContentAt(position, { type: 'annotation', attrs: { value: copyAnnotation(annotation), draftId: id } });
    return id;
  }

  updateAnnotation(sessionId: string, id: string, comment: string): void {
    const annotation = this.annotations(sessionId).find(item => item.id === id);
    if (!annotation) return;
    const editor = this.getEditor(sessionId);
    editor.view.dispatch(editor.state.tr.setNodeMarkup(annotation.position, undefined, {
      draftId: id, value: { ...annotation.value, comment },
    }));
  }

  removeAnnotation(sessionId: string, id: string): void {
    const annotation = this.annotations(sessionId).find(item => item.id === id);
    if (!annotation) return;
    const editor = this.getEditor(sessionId);
    editor.view.dispatch(closeHistory(editor.state.tr).delete(annotation.position, annotation.position + 1));
  }

  insertFiles(sessionId: string, files: readonly File[], position?: number | 'end'): void {
    const session = this.requireSession(sessionId);
    for (const file of files) {
      if (file.type.startsWith('image/') && !acceptedImageMediaTypes.has(file.type)) {
        throw new Error('图片仅支持 PNG、JPEG 和 WebP。');
      }
    }
    const nodes: JSONContent[] = [];
    for (const file of files) {
      if (!acceptedImageMediaTypes.has(file.type)) {
        nodes.push(this.createImport(sessionId, session, [file], false));
        continue;
      }
      const asset = this.createAsset(file, file.type);
      session.assets.set(asset.id, asset);
      nodes.push({
        type: 'image',
        attrs: {
          src: asset.objectUrl,
          alt: '',
          title: null,
          assetId: asset.id,
        },
      });
    }
    const chain = session.editor.chain().focus(undefined, { scrollIntoView: false });
    if (position !== undefined) chain.setTextSelection(position === 'end' ? session.editor.state.doc.content.size - 1 : position);
    chain.insertContent(nodes).run();
    this.changed();
  }

  insertDirectory(sessionId: string, files: readonly File[]): void {
    if (!files.length) return;
    const session = this.requireSession(sessionId);
    const node = this.createImport(sessionId, session, files, true);
    session.editor.chain().focus('end', { scrollIntoView: false }).insertContent(node).run();
  }

  private createImport(sessionId: string, session: DraftSession, files: readonly File[], directory: boolean): JSONContent {
    const item: DraftImport = { id: crypto.randomUUID(), files, directory, controller: new AbortController(), raw: '' };
    session.imports.set(item.id, item);
    // Reserve all editor positions synchronously before starting asynchronous IO.
    queueMicrotask(() => { void this.runImport(sessionId, session, item); });
    return { type: 'fileReference', attrs: { id: item.id, name: directory
      ? files[0].webkitRelativePath.split('/')[0] : files[0].name, kind: directory ? 'directory' : 'file', state: 'uploading' } };
  }

  private async runImport(sessionId: string, session: DraftSession, item: DraftImport): Promise<void> {
    if (session.editor.isDestroyed || !this.hasImport(session, item.id)) return;
    const controller = new AbortController();
    item.controller = controller;
    item.error = undefined;
    this.syncImports(session);
    try {
      if (!this.importer) throw new Error('本地连接尚未就绪，请重新连接后重试。');
      const result = await this.importer(sessionId, item.files, item.directory, controller.signal);
      if (item.controller !== controller || controller.signal.aborted) return;
      const raw = formatFileReference(result, item.directory);
      if (!fileReference(raw)) throw new Error('导入服务返回了无效路径。');
      item.raw = raw;
    } catch (error) {
      if (item.controller !== controller || controller.signal.aborted) return;
      item.error = error instanceof Error ? error.message : '导入失败，请重试。';
    }
    if (!session.editor.isDestroyed) { this.syncImports(session); this.changed(); }
  }

  private hasImport(session: DraftSession, id: string): boolean {
    return ((session.editor.getJSON() as JSONContent).content?.find(node => node.type === 'paragraph')?.content ?? []).some(node => node.attrs?.id === id);
  }

  private syncImports(session: DraftSession): void {
    const transaction = session.editor.state.tr;
    session.editor.state.doc.descendants((node, position) => {
      if (node.type.name !== 'fileReference') return;
      const item = session.imports.get(node.attrs.id);
      if (!item) return;
      const state = item.raw ? 'ready' : item.error ? 'failed' : 'uploading';
      if (node.attrs.raw !== item.raw || node.attrs.state !== state || node.attrs.error !== (item.error ?? '')) {
        transaction.setNodeMarkup(position, undefined, { ...node.attrs, raw: item.raw, state, error: item.error ?? '' });
      }
    });
    if (transaction.docChanged) session.editor.view.dispatch(transaction.setMeta('addToHistory', false));
  }

  private removeImport(session: DraftSession, id: string): void {
    const transaction = session.editor.state.tr;
    const positions: Array<{ from: number; to: number }> = [];
    session.editor.state.doc.descendants((node, pos) => {
      if (node.type.name === 'fileReference' && node.attrs.id === id) positions.push({ from: pos, to: pos + node.nodeSize });
    });
    for (const position of positions.reverse()) transaction.delete(position.from, position.to);
    session.editor.view.dispatch(transaction);
  }

  insertText(sessionId: string, value: string, atStart = false): void {
    const session = this.requireSession(sessionId);
    const nodes = plainTextNodes(value);
    const chain = session.editor.chain().focus(undefined, { scrollIntoView: false });
    if (atStart) {
      let start = 1;
      session.editor.state.doc.forEach(node => { if (node.type.name === 'annotation') start += node.nodeSize; });
      chain.setTextSelection(start);
    }
    chain.insertContent(nodes).run();
  }

  insertInternalHtml(sessionId: string, value: string, position?: number): boolean {
    const session = this.requireSession(sessionId);
    const template = document.createElement('template');
    template.innerHTML = value;
    if (!template.content.querySelector('img, [data-file-reference], [data-skill-reference]')) return false;
    const nodes: JSONContent[] = [];
    let valid = true;
    const visit = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent) nodes.push({ type: 'text', text: node.textContent });
        return;
      }
      if (!(node instanceof HTMLElement)) return;
      if (node.hasAttribute('data-skill-reference')) {
        const raw = node.getAttribute('data-skill-reference') ?? '';
        if (skillReference(raw)) nodes.push(skillReferenceNode(raw));
        else valid = false;
        return;
      }
      if (node.hasAttribute('data-file-reference')) {
        const raw = node.getAttribute('data-file-reference') ?? '';
        if (fileReference(raw)) nodes.push(referenceNode(raw));
        else valid = false;
        return;
      }
      if (node.tagName === 'BR') {
        nodes.push({ type: 'hardBreak' });
        return;
      }
      if (node.tagName === 'IMG') {
        const assetId = node.dataset.promptAssetId;
        const asset = assetId ? session.assets.get(assetId) : undefined;
        if (!asset) {
          valid = false;
          return;
        }
        nodes.push({
          type: 'image',
          attrs: {
            src: asset.objectUrl,
            alt: '',
            title: null,
            assetId: asset.id,
          },
        });
        return;
      }
      for (const child of node.childNodes) visit(child);
    };
    for (const child of template.content.childNodes) visit(child);
    if (!valid || !nodes.some((node) => node.type === 'image' || node.type === 'fileReference' || node.type === 'skillReference')) return false;
    const chain = session.editor.chain().focus(undefined, { scrollIntoView: false });
    if (position !== undefined) chain.setTextSelection(position);
    chain.insertContent(nodes).run();
    return true;
  }

  focus(sessionId: string, position?: 'end'): void {
    this.requireSession(sessionId).editor.commands.focus(
      position,
      { scrollIntoView: false },
    );
  }

  imageObjectUrl(sessionId: string, assetId: string): string | undefined {
    return this.sessions.get(sessionId)?.assets.get(assetId)?.objectUrl;
  }

  imageStatuses(sessionId: string): PromptDraftImageStatus[] {
    const session = this.sessions.get(sessionId);
    if (!session) return [];
    return imageAssetIds(session.editor.getJSON()).map((assetId) => {
      const asset = session.assets.get(assetId);
      if (!asset || asset.error) {
        return { assetId, state: 'failed', reason: asset?.error ?? '原始图片已丢失。' };
      }
      return { assetId, state: asset.bytes ? 'ready' : 'loading' };
    });
  }

  async capture(sessionId: string): Promise<PromptDraftSnapshot> {
    const session = this.requireSession(sessionId);
    const revision = session.revision;
    const document: JSONContent = session.editor.getJSON();
    if ((document.content?.find(node => node.type === 'paragraph')?.content ?? []).some(node => node.type === 'fileReference' && !fileReference(node.attrs?.raw ?? ''))) {
      throw new Error('请等待文件导入完成，或重试/移除失败的文件。');
    }
    const selectedAssets = new Map<string, DraftAsset>();
    for (const assetId of imageAssetIds(document)) {
      const asset = session.assets.get(assetId);
      if (!asset) throw new Error('图片节点缺少原始文件。');
      selectedAssets.set(assetId, asset);
    }
    await Promise.all([...selectedAssets.values()].map((asset) => asset.ready));
    for (const asset of selectedAssets.values()) {
      if (asset.error || !asset.bytes) {
        throw new Error(asset.error ?? '图片读取尚未完成。');
      }
    }
    return {
      owner: session,
      revision,
      content: serializePromptDocument(document, selectedAssets),
    };
  }

  restoreIfEmpty(sessionId: string, content: EditablePromptContent): boolean {
    const current = this.sessions.get(sessionId);
    if (current && this.summary(sessionId).hasContent) return false;
    if (current) this.releaseSession(current);
    const assets = new Map<string, DraftAsset>();
    const nodes: JSONContent[] = [];
    const annotations: JSONContent[] = [];
    for (const part of content.parts) {
      if (part.type === 'text') {
        nodes.push(...plainTextNodes(part.text));
        continue;
      }
      if (part.type === 'annotation') {
        annotations.push({ type: 'annotation', attrs: { value: copyAnnotation(part), draftId: crypto.randomUUID() } });
        continue;
      }
      const blob = new Blob([new Uint8Array(part.bytes)], {
        type: part.declaredMediaType,
      });
      const asset = this.createReadyAsset(blob, part.declaredMediaType, part.bytes);
      assets.set(asset.id, asset);
      nodes.push({
        type: 'image',
        attrs: { src: asset.objectUrl, alt: '', title: null, assetId: asset.id },
      });
    }
    const session: DraftSession = {
      editor: undefined as unknown as Editor,
      assets,
      imports: new Map(),
      revision: 0,
    };
    session.editor = this.createEditor(session, { type: 'doc', content: [...annotations, ...paragraphDocument(nodes).content!] });
    this.sessions.set(sessionId, session);
    this.changed();
    return true;
  }

  clearIfSnapshot(sessionId: string, snapshot: PromptDraftSnapshot): boolean {
    const session = this.sessions.get(sessionId);
    if (!session || session !== snapshot.owner || session.revision !== snapshot.revision) {
      return false;
    }
    this.releaseSession(session);
    this.sessions.delete(sessionId);
    this.changed();
    return true;
  }

  removeSession(sessionId: string): void {
    const session = this.sessions.get(sessionId);
    if (!session) return;
    this.releaseSession(session);
    this.sessions.delete(sessionId);
    this.changed();
  }

  destroy(): void {
    for (const session of this.sessions.values()) this.releaseSession(session);
    this.sessions.clear();
    this.listeners.clear();
  }

  private requireSession(sessionId: string): DraftSession {
    this.getEditor(sessionId);
    return this.sessions.get(sessionId)!;
  }

  private createEditor(session: DraftSession, content?: JSONContent): Editor {
    return new Editor({
      element: document.createElement('div'),
      extensions: [
        SingleParagraphDocument,
        PromptAnnotationNode,
        Paragraph,
        Text,
        HardBreak,
        PromptImageNode,
        SkillReferenceNode,
        FileReferenceNode.configure({
          retry: (id: string) => { const item = session.imports.get(id); if (item?.error) void this.runImport(this.sessionIdFor(session), session, item); },
          remove: (id: string) => this.removeImport(session, id),
        }),
        UndoRedo,
      ],
      content: content ?? paragraphDocument([]),
      enableInputRules: false,
      enablePasteRules: false,
      enableContentCheck: true,
      injectCSS: false,
      onUpdate: () => {
        session.revision += 1;
        for (const item of session.imports.values()) {
          if (!item.raw && !item.error && !this.hasImport(session, item.id)) {
            item.controller.abort();
            item.error = '导入已取消，可重试。';
          }
        }
        this.syncImports(session);
        this.changed();
      },
    });
  }

  private createAsset(blob: Blob, declaredMediaType: string): DraftAsset {
    const id = crypto.randomUUID();
    const asset: DraftAsset = {
      id,
      blob,
      declaredMediaType,
      objectUrl: URL.createObjectURL(blob),
      ready: Promise.resolve(),
    };
    asset.ready = blob.arrayBuffer().then((value) => {
      asset.bytes = new Uint8Array(value.slice(0));
    }).catch((error: unknown) => {
      asset.error = error instanceof Error ? error.message : '图片读取失败。';
    }).finally(() => this.changed());
    return asset;
  }

  private createReadyAsset(
    blob: Blob,
    declaredMediaType: string,
    value: Uint8Array,
  ): DraftAsset {
    return {
      id: crypto.randomUUID(),
      blob,
      declaredMediaType,
      objectUrl: URL.createObjectURL(blob),
      bytes: new Uint8Array(value),
      ready: Promise.resolve(),
    };
  }

  private releaseSession(session: DraftSession): void {
    for (const item of session.imports.values()) item.controller.abort();
    session.editor.destroy();
    for (const asset of session.assets.values()) URL.revokeObjectURL(asset.objectUrl);
    session.assets.clear();
  }

  private sessionIdFor(session: DraftSession): string {
    for (const [id, value] of this.sessions) if (value === session) return id;
    throw new Error('草稿已经关闭。');
  }

  private changed(): void {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }
}

function paragraphDocument(content: JSONContent[]): JSONContent {
  return {
    type: 'doc',
    content: [{ type: 'paragraph', ...(content.length ? { content } : {}) }],
  };
}

function plainTextNodes(value: string): JSONContent[] {
  return splitFileReferences(value).flatMap(part => typeof part === 'string'
    ? splitSkillReferences(part).flatMap(piece => typeof piece === 'string'
      ? literalTextNodes(piece) : [skillReferenceNode(piece.raw)])
    : [referenceNode(part.raw)]);
}

function skillReferenceNode(raw: string): JSONContent {
  return { type: 'skillReference', attrs: { raw } };
}

function referenceNode(raw: string): JSONContent {
  const reference = fileReference(raw)!;
  return { type: 'fileReference', attrs: { raw, name: reference.name, kind: reference.kind, state: 'ready' } };
}

function literalTextNodes(value: string): JSONContent[] {
  const result: JSONContent[] = [];
  const lines = value.split('\n');
  lines.forEach((line, index) => {
    if (line) result.push({ type: 'text', text: line });
    if (index < lines.length - 1) result.push({ type: 'hardBreak' });
  });
  return result;
}

function draftText(document: JSONContent): string {
  let result = '';
  for (const node of document.content?.find(node => node.type === 'paragraph')?.content ?? []) {
    if (node.type === 'text') result += node.text ?? '';
    else if (node.type === 'fileReference' || node.type === 'skillReference') result += node.attrs?.raw ?? '';
    else if (node.type === 'hardBreak') result += '\n';
  }
  return result;
}

function imageAssetIds(document: JSONContent): string[] {
  const result: string[] = [];
  for (const node of document.content?.find(node => node.type === 'paragraph')?.content ?? []) {
    if (node.type === 'image' && typeof node.attrs?.assetId === 'string') {
      result.push(node.attrs.assetId);
    }
  }
  return result;
}

function serializePromptDocument(
  document: JSONContent,
  assets: ReadonlyMap<string, DraftAsset>,
): EditablePromptContent {
  const parts: Array<EditablePromptContent['parts'][number]> = (document.content ?? [])
    .filter(node => node.type === 'annotation').map(node => decodeAnnotation(node.attrs?.value));
  let text = '';
  const flushText = () => {
    if (!text) return;
    parts.push({ type: 'text', text });
    text = '';
  };
  for (const node of document.content?.find(node => node.type === 'paragraph')?.content ?? []) {
    if (node.type === 'text') {
      text += node.text ?? '';
    } else if (node.type === 'fileReference') {
      if (!fileReference(node.attrs?.raw ?? '')) throw new Error('文件尚未完成导入。');
      text += node.attrs!.raw;
    } else if (node.type === 'skillReference') {
      if (!skillReference(node.attrs?.raw ?? '')) throw new Error('技能引用格式不正确。');
      text += node.attrs!.raw;
    } else if (node.type === 'hardBreak') {
      text += '\n';
    } else if (node.type === 'image') {
      flushText();
      const assetId = node.attrs?.assetId;
      const asset = typeof assetId === 'string' ? assets.get(assetId) : undefined;
      if (!asset?.bytes) throw new Error('图片节点尚未取得完整文件。');
      const image: LocalPromptImagePart = {
        type: 'image',
        source: 'local',
        bytes: new Uint8Array(asset.bytes),
        declaredMediaType: asset.declaredMediaType,
      };
      parts.push(image);
    } else {
      throw new Error('编辑器包含不支持的内容。');
    }
  }
  flushText();
  return { parts };
}
