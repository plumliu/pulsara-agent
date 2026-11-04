import { useContext } from 'react';
import type { ToolTrace } from '../lib/pulsara-types';
import { FileMediaContext } from './file-link-context';

const record = (value: unknown): Record<string, unknown> | undefined => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
const seconds = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const time = (value: number) => `${Math.floor(value / 60)}:${(value % 60).toFixed(3).padStart(6, '0')}`;

/** Navigation consumes only an already-delivered complete source page. It never
 * reads a media source, follows next, or reconstructs evidence from arguments. */
export function MediaEvidenceIndex({ trace, completeOutput }: { trace: ToolTrace; completeOutput?: string }) {
  const media = useContext(FileMediaContext);
  if (!media || trace.status === 'running' || !['read_video_frames', 'read_transcript'].includes(trace.toolName ?? '')) return null;
  const text = completeOutput ?? trace.resultContent?.parts.filter(part => part.type === 'text').map(part => part.text).join('\n\n') ?? trace.resultText;
  let page: Record<string, unknown> | undefined;
  try { page = record(JSON.parse(text ?? '')); } catch { return null; }
  const target = record(page?.target), version = record(page?.media_version);
  const path = target?.kind === 'PATH' ? target.path : undefined;
  if (typeof path !== 'string' || !path.startsWith('/') || typeof version?.modified_ns !== 'string' || !/^-?\d+$/.test(version.modified_ns)
    || typeof version.size !== 'number' || !Number.isSafeInteger(version.size) || version.size < 0) return null;
  const source = { modified_ns: version.modified_ns, size: version.size };
  const frames = trace.toolName === 'read_video_frames';
  const values = frames ? page?.frames : page?.entries;
  if (!Array.isArray(values) || !values.length) return null;
  const crop = frames ? page?.crop : undefined;
  if (frames && (!Array.isArray(crop) || crop.length !== 4 || !crop.every(seconds) || crop.some(value => value > 1) || crop[0] >= crop[2] || crop[1] >= crop[3])) return null;
  const positions = values.map((raw, index) => {
    const item = record(raw);
    if (!item) return undefined;
    if (frames) {
      if (!seconds(item.t) || !Number.isInteger(item.n) || (item.n as number) < 1 || !Number.isInteger(item.image) || (item.image as number) < 1) return undefined;
      return { seconds: item.t, label: `帧 ${item.n} · ${time(item.t)}`, title: `图 ${item.image} · ${item.t} 秒 · crop=${JSON.stringify(crop)}`, aria: `定位第 ${item.n} 帧到 ${item.t} 秒` };
    }
    if (!seconds(item.from) || !seconds(item.to) || item.to < item.from || typeof item.text !== 'string') return undefined;
    return { seconds: item.from, label: `${time(item.from)}–${time(item.to)}`, title: item.text, aria: `定位原文 ${index + 1} 到 ${item.from} 秒` };
  });
  if (positions.some(item => !item)) return null;
  return <nav className="media-evidence-index" aria-label={frames ? '视频画面时间索引' : '媒体原文时间索引'}>
    {positions.map((item, index) => <button key={index} type="button" title={item!.title} aria-label={item!.aria} onClick={() => media.open(path, item!.seconds, source)}>{item!.label}</button>)}
  </nav>;
}
