import { useCallback, useContext } from 'react';
import { Copy } from 'lucide-react';
import type { ToolBusinessValue, ToolResultPart } from '../lib/tool-business-value';
import { FileLinkContext, FileMediaContext } from './file-link-context';
import { SavedImage } from './saved-image';

function ToolImage({reference}: {reference: string}) {
  const context = useContext(FileLinkContext);
  const reader = context?.readImageReference;
  const read = useCallback((signal: AbortSignal) => reader
    ? reader(reference, signal) : Promise.reject(new Error('图片连接暂不可用。')), [reader, reference]);
  return <SavedImage key={context?.imageOwnerKey} read={read} title="工具读取的图片" inline />;
}
const time = (value: number) => `${Math.floor(value / 60)}:${(value % 60).toFixed(3).padStart(6, '0')}`;
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const seconds = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;

function VideoIndex({value, path}: {value: Record<string, unknown>; path?: string}) {
  const media = useContext(FileMediaContext);
  const frames = Array.isArray(value.frames) ? value.frames : [value];
  return <nav className="media-evidence-index" aria-label="视频画面时间索引">{frames.map((raw, index) => {
    if (!object(raw) || !seconds(raw.t)) return null;
    const at = raw.t;
    return media && path
      ? <button key={index} type="button" title={`在当前媒体中定位到 ${at} 秒`} onClick={()=>media.open(path,at)}>{time(at)}</button>
      : <span key={index}>{time(at)}</span>;
  })}</nav>;
}
function TranscriptSegment({value, path}: {value: Record<string, unknown>; path?: string}) {
  const media = useContext(FileMediaContext);
  if (!seconds(value.from) || !seconds(value.to) || typeof value.text !== 'string') return null;
  const start=value.from;
  return <article className="media-transcript-segment">
    <header>{media && path ? <button type="button" onClick={()=>media.open(path,start)}>{time(start)}–{time(value.to)}</button> : <span>{time(start)}–{time(value.to)}</span>}{typeof value.speaker === 'string' && <span>{value.speaker}</span>}</header>
    <p>{value.text}</p>
  </article>;
}
function ResultTarget({value}: {value: Record<string, unknown>}) {
  const context=useContext(FileLinkContext);
  if(value.kind !== 'PATH' || typeof value.path !== 'string') return null;
  const path=value.path;
  return context ? <button type="button" className="file-reference__label" onClick={event=>context.open(path,event.currentTarget)}>{path}</button> : <span>{path}</span>;
}
function ObjectPart({part,toolName,path}: {part: Extract<ToolResultPart,{type:'object'|'target'}>;toolName?:string;path?:string}) {
  if(part.type === 'target') return <ResultTarget value={part.value}/>;
  if(toolName === 'read_video_frames') return <VideoIndex value={part.value} path={path}/>;
  if(toolName === 'read_transcript') return <TranscriptSegment value={part.value} path={path}/>;
  return <details className="tool-result-details"><summary>查看结构化结果</summary><pre className="tool-output-scroll">{JSON.stringify(part.value,null,2)}</pre></details>;
}

/** Public values stay inspectable; normal reading shows content and actions. */
export function ToolBusinessContent({value,toolName,onCopy}: {value: ToolBusinessValue;toolName?:string;onCopy?:(text:string)=>void}) {
  const target=object(value.target) ? value.target : undefined;
  const path=target?.kind === 'PATH' && typeof target.path === 'string' ? target.path : undefined;
  const source=toolName === 'read_transcript' ? value.meta.source === 'subtitle' ? '字幕原文' : value.meta.source === 'asr' ? '语音识别原文' : undefined : undefined;
  const structuredText=Object.hasOwn(value,'structuredContent') ? JSON.stringify(value.structuredContent,null,2) : undefined;
  return <section className="tool-result-raw tool-result-images" aria-label="工具完整结果">
    {(path || source) && <header>{target && <ResultTarget value={target}/>} {source && <span>{source}</span>}</header>}
    {value.content.map((part, index) => part.type === 'image'
      ? <ToolImage key={index} reference={part.value.image_ref}/>
      : part.type === 'text' ? <pre key={index}>{part.value}</pre>
      : <ObjectPart key={index} part={part} toolName={toolName} path={path}/>)}
    {structuredText !== undefined && <details className="tool-result-details">
      <summary>查看结构化结果</summary>
      {onCopy && <header><button type="button" aria-label="复制结构化结果" onClick={()=>onCopy(structuredText)}><Copy size={12}/></button></header>}
      <pre className="tool-output-scroll">{structuredText}</pre>
    </details>}
  </section>;
}
