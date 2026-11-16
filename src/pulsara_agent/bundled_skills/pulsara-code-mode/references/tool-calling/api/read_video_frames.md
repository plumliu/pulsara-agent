# `tools.read_video_frames(args)`

Read local video frames. Pass target:PATH, from?, to?, action? and crop?. overview locates content across a range; detail reads text or objects within explicit from/to at most two seconds apart; motion examines changes and order. detail samples start, midpoint and just before the end. Decimal seconds and normalized crop are supported. Returned indices describe the images that follow them.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface ReadVideoFramesArgs {
  /**
   * overview locates content; detail reads start/middle/end within an explicit range of at most 2 seconds; motion examines changes and order.
   */
  action?: "overview" | "detail" | "motion";
  /**
   * Normalized [left, top, right, bottom] corners of the displayed frame: 0 <= left < right <= 1 and 0 <= top < bottom <= 1.
   *
   * @minItems 4
   * @maxItems 4
   */
  crop?: [number, number, number, number, ...number[]];
  /**
   * Start in seconds (decimals supported). Defaults to 0 for overview/motion; required for detail.
   */
  from?: number;
  target: {
    kind: "PATH";
    path: string;
  };
  /**
   * Exclusive end in seconds. Defaults to media end for overview/motion; detail requires an explicit end at most 2 seconds after from.
   */
  to?: number;
}

export type ReadVideoFramesValue =
  | (
      | {
          target: {
            kind: "PATH";
            path: string;
          };
          meta: {
            status: "SUCCESS";
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "image";
                value: {
                  kind: "IMAGE_REF";
                  image_ref: string;
                };
              }
            | {
                type: "object";
                value:
                  | {
                      t: number;
                      /**
                       * @minItems 2
                       * @maxItems 2
                       */
                      visible_range: [number, number, ...number[]];
                      dt?: number;
                    }
                  | {
                      frames: {
                        n: number;
                        t: number;
                        /**
                         * @minItems 4
                         * @maxItems 4
                         */
                        cell_rect: [number, number, number, number, ...number[]];
                        dt?: number;
                        /**
                         * @minItems 2
                         * @maxItems 2
                         */
                        visible_range?: [number, number, ...number[]];
                        /**
                         * @minItems 2
                         * @maxItems 2
                         */
                        representative_range?: [number, number, ...number[]];
                      }[];
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
          };
          content: (
            | {
                type: "text";
                value: string;
              }
            | {
                type: "image";
                value: {
                  kind: "IMAGE_REF";
                  image_ref: string;
                };
              }
            | {
                type: "object";
                value:
                  | {
                      t: number;
                      /**
                       * @minItems 2
                       * @maxItems 2
                       */
                      visible_range: [number, number, ...number[]];
                      dt?: number;
                    }
                  | {
                      frames: {
                        n: number;
                        t: number;
                        /**
                         * @minItems 4
                         * @maxItems 4
                         */
                        cell_rect: [number, number, number, number, ...number[]];
                        dt?: number;
                        /**
                         * @minItems 2
                         * @maxItems 2
                         */
                        visible_range?: [number, number, ...number[]];
                        /**
                         * @minItems 2
                         * @maxItems 2
                         */
                        representative_range?: [number, number, ...number[]];
                      }[];
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
  read_video_frames(args: ReadVideoFramesArgs): Promise<Successful<ReadVideoFramesValue>>;
};
```
