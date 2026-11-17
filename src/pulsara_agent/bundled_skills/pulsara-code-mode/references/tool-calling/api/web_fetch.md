# `tools.web_fetch(args)`

Read extracted Markdown from one HTTP(S) webpage. Pass target:URL. Returns the requested URL Target, available title and extracted text.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

```ts
export interface WebFetchArgs {
  target: {
    kind: "URL";
    /**
     * Full HTTP(S) URL without embedded credentials.
     */
    url: string;
  };
}

export type WebFetchValue =
  | (
      | {
          target: {
            kind: "URL";
            /**
             * Full HTTP(S) URL without embedded credentials.
             */
            url: string;
          };
          meta: {
            status: "SUCCESS";
            title?: string;
          };
          content: {
            type: "text";
            value: string;
          }[];
        }
      | {
          target?: {
            kind: "URL";
            /**
             * Full HTTP(S) URL without embedded credentials.
             */
            url: string;
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
  web_fetch(args: WebFetchArgs): Promise<Successful<WebFetchValue>>;
};
```
