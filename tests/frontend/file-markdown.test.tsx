import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FileMarkdown } from '../../frontend/components/file-markdown';
import { MarkdownBody } from '../../frontend/components/markdown-body';

afterEach(cleanup);
const onNotify = vi.fn();

it('separates skill metadata from prose, preserving folded descriptions and literal markup', () => {
  render(<FileMarkdown onNotify={onNotify} body={'\uFEFF---\r\nname: find-skills\r\ndescription: >\r\n  Find skills for $x$ and\r\n  <script>alert(1)</script>.\r\nmetadata:\r\n  tags: [search, tools]\r\n---\r\n# Find Skills\r\n\r\nBody\r\n\r\n---\r\n\r\nFooter'} />);
  const metadata = screen.getByRole('region', { name: '文档属性' });
  expect(within(metadata).getByText('find-skills')).toBeTruthy();
  expect(within(metadata).getByText('Find skills for $x$ and <script>alert(1)</script>.')).toBeTruthy();
  expect(metadata.querySelector('script, h1, h2, .katex')).toBeNull();
  expect(within(metadata).getByText(/tags:\s+- search\s+- tools/)).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Find Skills' })).toBeTruthy();
  expect(screen.getAllByRole('separator')).toHaveLength(1);
});

it.each(['name: [unfinished', 'name: first\nname: duplicate', 'name: !unknown value', '- one\n- two'])(
  'keeps unsupported or malformed metadata readable: %s', source => {
    render(<FileMarkdown onNotify={onNotify} body={`---\n${source}\n---\n# Body`} />);
    expect(screen.getByRole('region', { name: '文档属性' }).querySelector('pre')?.textContent).toBe(source);
    expect(screen.getByRole('heading', { name: 'Body' })).toBeTruthy();
  },
);

it.each(['---\nname: unfinished', '# Body\n\n---\nname: ordinary\n---', '```yaml\n---\nname: example\n---\n```'])(
  'leaves non-frontmatter Markdown with the existing renderer: %s', body => {
    render(<FileMarkdown body={body} onNotify={onNotify} />);
    expect(screen.queryByRole('region', { name: '文档属性' })).toBeNull();
    expect(document.body.textContent).toContain('name:');
  },
);

it('does not change frontmatter-looking text in chat messages', () => {
  render(<MarkdownBody body={'---\nname: ordinary\n---'} onNotify={onNotify} />);
  expect(screen.queryByRole('region', { name: '文档属性' })).toBeNull();
  expect(screen.getByRole('heading', { name: 'name: ordinary' })).toBeTruthy();
});
