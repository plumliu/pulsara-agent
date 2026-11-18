# `tools.web_fetch(args)`

Read extracted Markdown from one HTTP(S) webpage. Pass target:URL. Returns the requested URL Target, available title and extracted text.

```ts
export interface WebFetchArgs {
  target: UrlTarget;
}
export interface UrlTarget {
  kind: "URL";
  /**
   * Full HTTP(S) URL without embedded credentials.
   */
  url: string;
}
export interface WebFetchSuccess {
  target: UrlTarget;
  meta: {
    status: "SUCCESS";
    title?: string;
  };
  content: TextPart[];
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface WebFetchFailure {
  target?: UrlTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  web_fetch(args: WebFetchArgs): Promise<WebFetchSuccess>;
};
```
