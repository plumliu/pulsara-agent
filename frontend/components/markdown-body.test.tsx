import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarkdownBody, normalizeMathMarkdown } from './markdown-body';

describe('MarkdownBody math rendering', () => {
  it('normalizes standalone dollar displays without rewriting TeX delimiters or code', () => {
    const source = [
      '行内 \\(x^2\\) 与 $y^2$。',
      '$$E=mc^2$$',
      '\\[a^2+b^2=c^2\\]',
      '`\\(inline code\\)`',
      '```text',
      '\\[fenced code\\]',
      '```',
    ].join('\n');

    const normalized = normalizeMathMarkdown(source);

    expect(normalized).toContain('行内 \\(x^2\\) 与 $y^2$。');
    expect(normalized).toContain('$$\nE=mc^2\n$$');
    expect(normalized).toContain('\\[a^2+b^2=c^2\\]');
    expect(normalized).toContain('`\\(inline code\\)`');
    expect(normalized).toContain('```text\n\\[fenced code\\]\n```');
  });

  it('renders dollar and slash forms as inline and display math', () => {
    const body = [
      '美元行内：$x^2$。斜线行内：\\(y^2\\)。',
      '',
      '$$E=mc^2$$',
      '',
      '\\[a^2+b^2=c^2\\]',
      '',
      '`\\(code stays code\\)`',
    ].join('\n');

    const { container } = render(<MarkdownBody body={body} />);

    expect(container.querySelectorAll('.katex')).toHaveLength(4);
    expect(container.querySelectorAll('.katex-display')).toHaveLength(2);
    expect(container.querySelector('code')?.textContent).toBe('\\(code stays code\\)');
    expect(container.querySelector('code .katex')).toBeNull();
  });

  it('preserves multiline display math nested in an ordered list', () => {
    const body = [
      '1. 对含 \\(\\eta\'\\) 的项分部积分',
      '   \\[',
      '   \\int_a^b f(x)\\,dx',
      '   =',
      '   F(b)',
      '   -',
      '   F(a)',
      '   \\]',
      '',
      '2. 因为 \\(\\eta(x)\\) 任意，所以继续。',
      '',
      '**后续粗体仍应正常渲染。**',
    ].join('\n');

    const { container, getByText } = render(<MarkdownBody body={body} />);

    expect(container.querySelectorAll('ol > li')).toHaveLength(2);
    expect(container.querySelectorAll('.katex-display')).toHaveLength(1);
    expect(container.querySelectorAll('.katex')).toHaveLength(3);
    expect(container.querySelector('.katex-error')).toBeNull();
    expect(getByText('后续粗体仍应正常渲染。').tagName).toBe('STRONG');
  });

  it('preserves fenced and inline source text without enabling raw HTML', () => {
    const body = [
      '正文 `read_file`、edit_file、ROOT、Kernel、HostSession、read-only、bypass-permissions。',
      '',
      '```python',
      'from api import read_file, edit_file',
      'ROOT = "read-only"',
      'exit_code = 0',
      '```',
      '',
      '<script>window.source_fidelity_broken = true</script>',
      '',
      '$$E=mc^2$$',
    ].join('\n');

    const { container } = render(<MarkdownBody body={body} />);
    const code = Array.from(container.querySelectorAll('pre code'), (node) => node.textContent).join('');

    expect(code).toBe([
      'from api import read_file, edit_file',
      'ROOT = "read-only"',
      'exit_code = 0',
      '',
    ].join('\n'));
    expect(container.querySelector('p code')?.textContent).toBe('read_file');
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelectorAll('.katex-display')).toHaveLength(1);
  });
});
