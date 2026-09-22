import { Editor, type JSONContent } from '@tiptap/core';
import Document from '@tiptap/extension-document';
import HardBreak from '@tiptap/extension-hard-break';
import Image from '@tiptap/extension-image';
import Paragraph from '@tiptap/extension-paragraph';
import Text from '@tiptap/extension-text';
import { UndoRedo } from '@tiptap/extensions';
import type { EditablePromptContent, LocalPromptImagePart } from './prompt-content';

const acceptedImageMediaTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);

const SingleParagraphDocument = Document.extend({
  content: 'paragraph',
});

const LocalImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      assetId: {
        default: null,
        parseHTML: () => null,
        renderHTML: (attributes) => typeof attributes.assetId === 'string'
          ? { 'data-prompt-asset-id': attributes.assetId }
          : {},
      },
    };
  },
  parseHTML() {
    return [];
  },
}).configure({
  inline: true,
  allowBase64: false,
  resize: false,
  HTMLAttributes: { class: 'composer-inline-image' },
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
  revision: number;
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
}

export interface PromptDraftImageStatus {
  assetId: string;
  state: 'loading' | 'ready' | 'failed';
  reason?: string;
}

export class PromptDraftStore {
  private readonly sessions = new Map<string, DraftSession>();
  private readonly listeners = new Set<() => void>();
  private version = 0;

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
      };
    }
    const document = session.editor.getJSON();
    const text = draftText(document);
    const assetIds = imageAssetIds(document);
    return {
      revision: session.revision,
      text,
      hasContent: assetIds.length > 0 || text.trim().length > 0,
      hasImage: assetIds.length > 0,
      pendingImages: assetIds.filter((id) => !session.assets.get(id)?.bytes
        && !session.assets.get(id)?.error).length,
      failedImages: assetIds.filter((id) => Boolean(session.assets.get(id)?.error)).length,
    };
  }

  insertFiles(sessionId: string, files: readonly File[], position?: number): void {
    const session = this.requireSession(sessionId);
    for (const file of files) {
      if (!acceptedImageMediaTypes.has(file.type)) {
        throw new Error('仅支持 PNG、JPEG 和 WebP 图片。');
      }
    }
    const nodes: JSONContent[] = [];
    for (const file of files) {
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
    if (position !== undefined) chain.setTextSelection(position);
    chain.insertContent(nodes).run();
    this.changed();
  }

  insertText(sessionId: string, value: string, atStart = false): void {
    const session = this.requireSession(sessionId);
    const nodes = plainTextNodes(value);
    const chain = session.editor.chain().focus(undefined, { scrollIntoView: false });
    if (atStart) chain.setTextSelection(1);
    chain.insertContent(nodes).run();
  }

  insertInternalHtml(sessionId: string, value: string, position?: number): boolean {
    const session = this.requireSession(sessionId);
    const template = document.createElement('template');
    template.innerHTML = value;
    if (!template.content.querySelector('img')) return false;
    const nodes: JSONContent[] = [];
    let valid = true;
    const visit = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent) nodes.push({ type: 'text', text: node.textContent });
        return;
      }
      if (!(node instanceof HTMLElement)) return;
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
    if (!valid || !nodes.some((node) => node.type === 'image')) return false;
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
    const document = session.editor.getJSON();
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
    for (const part of content.parts) {
      if (part.type === 'text') {
        nodes.push(...plainTextNodes(part.text));
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
      revision: 0,
    };
    session.editor = this.createEditor(session, paragraphDocument(nodes));
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
        Paragraph,
        Text,
        HardBreak,
        LocalImage,
        UndoRedo,
      ],
      content: content ?? paragraphDocument([]),
      enableInputRules: false,
      enablePasteRules: false,
      enableContentCheck: true,
      injectCSS: false,
      onUpdate: () => {
        session.revision += 1;
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
    session.editor.destroy();
    for (const asset of session.assets.values()) URL.revokeObjectURL(asset.objectUrl);
    session.assets.clear();
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
  for (const node of document.content?.[0]?.content ?? []) {
    if (node.type === 'text') result += node.text ?? '';
    else if (node.type === 'hardBreak') result += '\n';
  }
  return result;
}

function imageAssetIds(document: JSONContent): string[] {
  const result: string[] = [];
  for (const node of document.content?.[0]?.content ?? []) {
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
  const parts: Array<EditablePromptContent['parts'][number]> = [];
  let text = '';
  const flushText = () => {
    if (!text) return;
    parts.push({ type: 'text', text });
    text = '';
  };
  for (const node of document.content?.[0]?.content ?? []) {
    if (node.type === 'text') {
      text += node.text ?? '';
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
