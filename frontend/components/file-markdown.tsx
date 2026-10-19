import { useMemo } from 'react';
import remarkFrontmatter from 'remark-frontmatter';
import remarkParse from 'remark-parse';
import { unified } from 'unified';
import { parseDocument, stringify } from 'yaml';
import { MarkdownBody, type MarkdownNotify } from './markdown-body';

const parser = unified().use(remarkParse).use(remarkFrontmatter);

function readMetadata(source: string): [string, string][] | undefined {
  try {
    const document = parseDocument(source, { schema: 'core' });
    if (document.errors.length || document.warnings.length) return;
    const value: unknown = document.toJS({ mapAsMap: true });
    if (!(value instanceof Map) || [...value.keys()].some(key => typeof key !== 'string')) return;
    return [...value.entries()].map(([key, item]) => [
      key,
      typeof item === 'string' ? item : stringify(item).trimEnd(),
    ]);
  } catch {
    // Malformed YAML and library resource-boundary failures remain readable.
    return;
  }
}

export function FileMarkdown({ body, onNotify }: { body: string; onNotify: MarkdownNotify }) {
  const content = useMemo(() => {
    // The Markdown library owns fence recognition; only an initial YAML node
    // is metadata. Extract it before prose math normalization touches its text.
    const first = parser.parse(body).children[0];
    if (first?.type !== 'yaml' || first.position?.end.offset === undefined) return { body };
    return {
      body: body.slice(first.position.end.offset),
      source: first.value,
      fields: readMetadata(first.value),
    };
  }, [body]);

  return <>
    {content.source?.trim() && <section className="file-frontmatter" aria-label="文档属性">
      <div className="file-frontmatter-label">文档属性</div>
      {content.fields ? <dl>
        {content.fields.map(([key, value]) => <div className="file-frontmatter-field" key={key}>
          <dt>{key}</dt><dd>{value}</dd>
        </div>)}
      </dl> : <pre>{content.source}</pre>}
    </section>}
    <MarkdownBody body={content.body} onNotify={onNotify} />
  </>;
}
