# `tools.read_transcript(args)`

Read timed original text from local video or audio. Pass target:PATH with optional from/to seconds. Embedded text subtitles are preferred; cached or configured ASR fills gaps. The complete requested range is acquired before display pagination. Transcription does not verify subtitles against speech or interpret music.

```ts
export interface ReadTranscriptArgs {
  from?: number;
  target: {
    kind: "PATH";
    path: string;
  };
  to?: number;
}
export interface ReadTranscriptSuccess {
  target: PathTarget;
  meta: {
    status: "SUCCESS";
    source: "subtitle" | "asr";
    language?: string;
  };
  content: (TextPart | ObjectPart)[];
}
export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface ObjectPart {
  type: "object";
  value: Value;
}
export interface Value {
  from: number;
  to: number;
  text: string;
  speaker?: string;
  visible_range?: VisibleRange;
}
export interface VisibleRange {
  from: number;
  to: number;
}
export interface ReadTranscriptFailure {
  target?: PathTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
    source?: "subtitle" | "asr";
    language?: string;
  };
  content: (TextPart | ObjectPart)[];
}

declare const tools: {
  read_transcript(args: ReadTranscriptArgs): Promise<ReadTranscriptSuccess>;
};
```
