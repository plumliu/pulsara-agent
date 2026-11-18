# `tools.web_search(args)`

Search webpages for sources and relevant summaries. Pass query, optional count (1–20, default 10), include_domains, exclude_domains, after and before (YYYY-MM-DD). Domains include their subdomains; exclusions take precedence. Returns ranked URL Targets with available title, snippet and date. Search summaries are not page contents; use web_fetch to read selected sources.

```ts
export interface WebSearchArgs {
  /**
   * Search after YYYY-MM-DD.
   */
  after?: string;
  /**
   * Search before YYYY-MM-DD; after must be earlier.
   */
  before?: string;
  count?: number;
  /**
   * Exclude these hostnames and their subdomains; exclusion takes precedence.
   */
  exclude_domains?: string[];
  /**
   * Include these hostnames and their subdomains; omit for unrestricted search.
   */
  include_domains?: string[];
  query: string;
}
export interface WebSearchSuccess {
  meta: {
    status: "SUCCESS";
  };
  content: (
    | {
        type: "text";
        value: string;
      }
    | {
        type: "object";
        value: {
          target: {
            kind: "URL";
            /**
             * Full HTTP(S) URL without embedded credentials.
             */
            url: string;
          };
          title?: string;
          snippet?: string;
          date?: string;
        };
      }
  )[];
}
export type WebSearchFailure = Omit<WebSearchSuccess, "meta"> & {
  meta: {status: "ERROR" | "CANCELLED"};
};

declare const tools: {
  web_search(args: WebSearchArgs): Promise<WebSearchSuccess>;
};
```
