# `tools.web_search(args)`

Search webpages for sources and relevant summaries. Pass query, optional count (1–20, default 10), include_domains, exclude_domains, after and before (YYYY-MM-DD). Domains include their subdomains; exclusions take precedence. Returns ranked URL Targets with available title, snippet and date. Search summaries are not page contents; use web_fetch to read selected sources.

See the [API rules](index.md) for successful resolution, actual `err.result` and explicit output. Array constraints are retained in type annotations.

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

export type WebSearchValue =
  | {
      meta: {
        status: "SUCCESS" | "ERROR" | "CANCELLED";
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
  web_search(args: WebSearchArgs): Promise<Successful<WebSearchValue>>;
};
```
