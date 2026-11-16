# `tools.read_transcript(args)`

Read timed original text from local video or audio. Pass target:PATH with optional from/to seconds. Embedded text subtitles are preferred; cached or configured ASR fills gaps. The complete requested range is acquired before display pagination. Transcription does not verify subtitles against speech or interpret music.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface ReadTranscriptArgs {
  from?: number;
  target: {
    kind: "PATH";
    path: string;
  };
  to?: number;
}

export type ReadTranscriptValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
            source: "subtitle" | "asr";
            language?: string;
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "object";
                value: {
                  from: number;
                  to: number;
                  text: string;
                  speaker?: string;
                  visible_range?: {
                    from: number;
                    to: number;
                  };
                };
              }
          )[];
        }
      | {
          target?: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "ERROR" | "CANCELLED";
            source?: "subtitle" | "asr";
            language?: string;
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "object";
                value: {
                  from: number;
                  to: number;
                  text: string;
                  speaker?: string;
                  visible_range?: {
                    from: number;
                    to: number;
                  };
                };
              }
          )[];
        }
    )
  | {
      meta: {
        status: "ERROR" | "CANCELLED";
      };
      content: {
        type: "text";
        value: string;
      }[];
    };

type Successful<T> = T extends { meta: { status: infer S } }
  ? "SUCCESS" extends S ? T & { meta: { status: "SUCCESS" } } : never
  : never;

declare const tools: {
  read_transcript(args: ReadTranscriptArgs): Promise<Successful<ReadTranscriptValue>>;
};
```
