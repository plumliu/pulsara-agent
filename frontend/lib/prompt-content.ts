export const PROMPT_BODY_MEDIA_TYPE = 'application/vnd.pulsara.prompt+json';

export type PromptContentOwner =
  | { kind: 'entry'; entryId: string }
  | { kind: 'queue'; queueItemId: string };

export interface PromptTextPart {
  type: 'text';
  text: string;
}

export interface LocalPromptImagePart {
  type: 'image';
  source: 'local';
  bytes: Uint8Array;
  declaredMediaType: string;
}

export interface CanonicalPromptImagePart {
  type: 'image';
  source: 'canonical';
  digest: string;
  encodedBytes: number;
  mediaType: string;
  width: number;
  height: number;
  refOrdinal: number;
  owner: PromptContentOwner;
}

export type EditablePromptPart = PromptTextPart | LocalPromptImagePart;
export type CanonicalPromptPart = PromptTextPart | CanonicalPromptImagePart;

export interface EditablePromptContent {
  parts: readonly EditablePromptPart[];
}

export interface CanonicalPromptContent {
  parts: readonly CanonicalPromptPart[];
}

export type DisplayPromptContent = EditablePromptContent | CanonicalPromptContent;

export interface PromptContentTransport {
  parts: Array<
    | { type: 'text'; text: string }
    | { type: 'image'; content_base64: string; declared_media_type: string }
  >;
}

const allowedImageMediaTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);
const digestPattern = /^sha256:[0-9a-f]{64}$/;

export function decodeCanonicalPromptContent(
  value: string,
  owner: PromptContentOwner,
): CanonicalPromptContent {
  let raw: unknown;
  try {
    raw = JSON.parse(value);
  } catch {
    throw new Error('输入正文格式无效。');
  }
  if (!isRecord(raw) || !hasExactKeys(raw, ['parts', 'schema'])
    || raw.schema !== 'pulsara.prompt/v1' || !Array.isArray(raw.parts)
    || raw.parts.length === 0) {
    throw new Error('输入正文格式无效。');
  }
  let refOrdinal = 0;
  const parts = raw.parts.map((item): CanonicalPromptPart => {
    if (!isRecord(item)) throw new Error('输入正文格式无效。');
    if (item.type === 'text' && hasExactKeys(item, ['text', 'type'])
      && typeof item.text === 'string') {
      return { type: 'text', text: item.text };
    }
    if (
      item.type !== 'image'
      || !hasExactKeys(item, [
        'digest', 'encoded_bytes', 'height', 'media_type', 'type', 'width',
      ])
      || typeof item.digest !== 'string'
      || !digestPattern.test(item.digest)
      || !positiveInteger(item.encoded_bytes)
      || typeof item.media_type !== 'string'
      || !allowedImageMediaTypes.has(item.media_type)
      || !positiveInteger(item.width)
      || !positiveInteger(item.height)
    ) throw new Error('输入正文格式无效。');
    const result: CanonicalPromptImagePart = {
      type: 'image',
      source: 'canonical',
      digest: item.digest,
      encodedBytes: item.encoded_bytes,
      mediaType: item.media_type,
      width: item.width,
      height: item.height,
      refOrdinal,
      owner,
    };
    refOrdinal += 1;
    return result;
  });
  if (!parts.some((part) => part.type === 'image' || part.text !== '')) {
    throw new Error('输入正文格式无效。');
  }
  return { parts };
}

export function editablePromptToTransport(
  content: EditablePromptContent,
): PromptContentTransport {
  return {
    parts: content.parts.map((part) => part.type === 'text'
      ? { type: 'text', text: part.text }
      : {
        type: 'image',
        content_base64: encodeBase64(part.bytes),
        declared_media_type: part.declaredMediaType,
      }),
  };
}

export function copyEditablePromptContent(
  content: EditablePromptContent,
): EditablePromptContent {
  return {
    parts: content.parts.map((part) => part.type === 'text'
      ? { ...part }
      : { ...part, bytes: new Uint8Array(part.bytes) }),
  };
}

export function promptContentTextProjection(content: DisplayPromptContent): string {
  return content.parts
    .filter((part): part is PromptTextPart => part.type === 'text')
    .map((part) => part.text)
    .join('\n');
}

export function promptContentHasImage(content: DisplayPromptContent): boolean {
  return content.parts.some((part) => part.type === 'image');
}

export function promptContentCanSubmit(content: EditablePromptContent): boolean {
  return content.parts.some(
    (part) => part.type === 'image' || part.text.trim().length > 0,
  );
}

export function promptImageCount(content: DisplayPromptContent): number {
  return content.parts.reduce(
    (count, part) => count + (part.type === 'image' ? 1 : 0),
    0,
  );
}

function encodeBase64(value: Uint8Array): string {
  let binary = '';
  const chunkSize = 32_768;
  for (let offset = 0; offset < value.length; offset += chunkSize) {
    binary += String.fromCharCode(...value.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length
    && keys.every((key, index) => key === expected[index]);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}
