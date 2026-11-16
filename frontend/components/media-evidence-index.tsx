import { useContext } from 'react';
import type { ToolTrace } from '../lib/pulsara-types';
import { FileMediaContext } from './file-link-context';

const record = (value: unknown): Record<string, unknown> | undefined => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
const seconds = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const time = (value: number) => `${Math.floor(value / 60)}:${(value % 60).toFixed(3).padStart(6, '0')}`;

/** Navigate actual frozen index values. Opening the current file is explicit;
 * its mutable version is not reconstructed from saved media evidence. */
export function MediaEvidenceIndex({ trace }: { trace: ToolTrace }) {
  const media = useContext(FileMediaContext);
  const value = trace.businessValue;
  if (!media || !value || trace.status === 'running' || !['read_video_frames', 'read_transcript'].includes(trace.toolName ?? '')) return null;
  const target = record(value.target);
  const path = target?.kind === 'PATH' ? target.path : undefined;
  if (typeof path !== 'string' || !path) return null;
  const frames = trace.toolName === 'read_video_frames';
  const values = value.content.flatMap(part => {
    if (part.type !== 'object') return [];
    const item = part.value;
    return frames && Array.isArray(item.frames) ? item.frames : [item];
  });
  const positions = values.map((raw, index) => {
    const item = record(raw);
    if (!item) return undefined;
    if (frames) {
      if (!seconds(item.t)) return undefined;
      return { seconds: item.t, label: `帧 ${index + 1} · ${time(item.t)}`, title: `${item.t} 秒`, aria: `定位第 ${index + 1} 帧到 ${item.t} 秒` };
    }
    if (!seconds(item.from) || !seconds(item.to) || item.to < item.from || typeof item.text !== 'string') return undefined;
    return { seconds: item.from, label: `${time(item.from)}–${time(item.to)}`, title: item.text, aria: `定位原文 ${index + 1} 到 ${item.from} 秒` };
  });
  if (!positions.length || positions.some(item => !item)) return null;
  return <nav className="media-evidence-index" aria-label={frames ? '视频画面时间索引' : '媒体原文时间索引'}>
    {positions.map((item, index) => <button key={index} type="button" title={item!.title} aria-label={item!.aria} onClick={() => media.open(path, item!.seconds)}>{item!.label}</button>)}
  </nav>;
}
