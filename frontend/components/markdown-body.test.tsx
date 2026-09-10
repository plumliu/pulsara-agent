import { fireEvent, render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MarkdownBody, normalizeMathMarkdown } from './markdown-body';

const noopNotify = () => undefined;

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

    const { container } = render(<MarkdownBody body={body} onNotify={noopNotify} />);

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

    const { container, getByText } = render(<MarkdownBody body={body} onNotify={noopNotify} />);

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

    const { container } = render(<MarkdownBody body={body} onNotify={noopNotify} />);
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

  it('wraps every inline and display formula with its LaTeX source', () => {
    const body = [
      '行内 $x^2$ 与 \\(y+1\\)。',
      '',
      '$$E=mc^2$$',
      '',
      '\\[\\boxed{z}\\]',
    ].join('\n');

    const { container } = render(<MarkdownBody body={body} onNotify={noopNotify} />);
    const inline = Array.from(container.querySelectorAll<HTMLElement>('.math-copy-target--inline'));
    const display = Array.from(container.querySelectorAll<HTMLElement>('.math-copy-target--display'));

    expect(inline.map((target) => target.dataset.mathSource)).toEqual(['x^2', 'y+1']);
    expect(display.map((target) => target.dataset.mathSource)).toEqual(['E=mc^2', '\\boxed{z}']);
    expect(container.querySelectorAll('[aria-label="复制 LaTeX 公式"]')).toHaveLength(4);
  });

  it('copies the source of the clicked rendered formula and uses the shared success notice', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const onNotify = vi.fn();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const body = [
      '$$',
      '\\frac{d}{dt}\n-\n\\frac{\\partial L}{\\partial q}',
      '$$',
    ].join('\n');
    const { container } = render(<MarkdownBody body={body} onNotify={onNotify} />);
    const renderedChild = container.querySelector('.math-copy-target .katex-html span');

    expect(renderedChild).not.toBeNull();
    fireEvent.click(renderedChild!);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      '\\frac{d}{dt}\n-\n\\frac{\\partial L}{\\partial q}',
    ));
    expect(onNotify).toHaveBeenCalledWith('公式已复制', undefined, 'success');
  });

  it('reports clipboard rejection through the shared warning notice', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    const onNotify = vi.fn();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const { container } = render(<MarkdownBody body="$x$" onNotify={onNotify} />);
    const target = container.querySelector<HTMLElement>('.math-copy-target');

    fireEvent.click(target!);

    await waitFor(() => expect(onNotify).toHaveBeenCalledWith(
      '无法复制公式',
      '浏览器没有授予剪贴板权限。',
      'warning',
    ));
  });

  it('supports keyboard copy and ignores a pointer drag across display math', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const onNotify = vi.fn();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const { container } = render(<MarkdownBody body="$$x+y$$" onNotify={onNotify} />);
    const target = container.querySelector<HTMLElement>('.math-copy-target')!;

    fireEvent.keyDown(target, { key: 'Enter' });
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));

    fireEvent.pointerDown(target, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(target, { pointerId: 1, clientX: 30, clientY: 10 });
    fireEvent.click(target);
    expect(writeText).toHaveBeenCalledTimes(1);
  });
});
