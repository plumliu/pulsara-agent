import { isCanonicalIdentity } from './identity';

export const TOOL_BUSINESS_MEDIA_TYPE = 'application/vnd.pulsara.tool-result+json';
export type ToolResultPart =
  | {type: 'text'; value: string}
  | {type: 'image'; value: {kind: 'IMAGE_REF'; image_ref: string}}
  | {type: 'object' | 'target'; value: Record<string, unknown>};
export interface ToolBusinessValue extends Record<string, unknown> {
  meta: {status: 'SUCCESS' | 'ERROR' | 'CANCELLED'; [key: string]: unknown};
  content: ToolResultPart[];
}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Decode the explicit typed business wire, never a model/display projection. */
export function decodeToolBusinessValue(text: string): ToolBusinessValue {
  const value: unknown = JSON.parse(text);
  if (!object(value) || !object(value.meta) || !['SUCCESS', 'ERROR', 'CANCELLED'].includes(String(value.meta.status)) || !Array.isArray(value.content)) throw new Error('工具完整结果格式无效。');
  for (const part of value.content) {
    if (!object(part) || Object.keys(part).length !== 2 || !('type' in part) || !('value' in part)) throw new Error('工具内容项格式无效。');
    if (part.type === 'text' && typeof part.value === 'string') continue;
    if (part.type === 'image' && object(part.value) && Object.keys(part.value).length === 2 && part.value.kind === 'IMAGE_REF' && isCanonicalIdentity(part.value.image_ref, 'image')) continue;
    if ((part.type === 'target' || part.type === 'object') && object(part.value)) continue;
    throw new Error('工具内容项格式无效。');
  }
  return value as ToolBusinessValue;
}

export function toolBusinessText(value: ToolBusinessValue): string {
  const {content, ...fields} = value;
  return JSON.stringify(fields, null, 2) + '\n' + content.map(part => part.type === 'text' ? part.value : JSON.stringify(part.value, null, part.type === 'object' ? 2 : undefined) + '\n').join('');
}
