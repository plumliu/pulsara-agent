# `tools.view_image(args)`

Read a local image, render a local HTML file, or reread a known image reference. Pass only target: PATH or IMAGE_REF. Image references may come from any conversation. Local HTML rendering produces an image; it does not publish the page. Readable image content is returned in order with its explanation.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface ViewImageArgs {
  target:
    | {
        kind: "PATH";
        path: string;
      }
    | {
        image_ref: string;
        kind: "IMAGE_REF";
      };
}

export type ViewImageValue =
  | (
      | {
          target:
            | {
                kind: "PATH";
                path: string;
              }
            | {
                kind: "IMAGE_REF";
                image_ref: string;
              };
          meta: {
            status: "SUCCESS";
          };
          /**
           * @minItems 2
           * @maxItems 2
           */
          content: [
            {
              type: "text";
              value: string;
            },
            {
              type: "image";
              value: {
                kind: "IMAGE_REF";
                image_ref: string;
              };
            }
          ];
        }
      | {
          target?:
            | {
                kind: "PATH";
                path: string;
              }
            | {
                kind: "IMAGE_REF";
                image_ref: string;
              };
          meta: {
            status: "ERROR" | "CANCELLED";
          };
          content: {
            type: "text";
            value: string;
          }[];
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
  view_image(args: ViewImageArgs): Promise<Successful<ViewImageValue>>;
};
```
