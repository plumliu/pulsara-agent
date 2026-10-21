import { fireEvent, render, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MarkdownBody, MarkdownInline, normalizeMathMarkdown } from '../../frontend/components/markdown-body';

afterEach(() => { delete window.pulsaraDesktop; });

const noopNotify = () => undefined;

describe('CJK emphasis and strikethrough', () => {
  it.each([MarkdownBody, MarkdownInline])('renders punctuation-adjacent CJK markup in %s', Component => {
    const { container } = render(<Component body={'**方案 A：**固定费用；这是**「重点」**说明；*提醒：*继续；~~错误：~~修正；**外层（~~旧值：~~新值）**结束'} onNotify={noopNotify} />);
    expect([...container.querySelectorAll('strong')].map(node => node.textContent)).toEqual(['方案 A：', '「重点」', '外层（旧值：新值）']);
    expect(container.querySelector('em')?.textContent).toBe('提醒：');
    expect([...container.querySelectorAll('del')].map(node => node.textContent)).toEqual(['错误：', '旧值：']);
    expect(container.querySelector('strong del')?.textContent).toBe('旧值：');
    expect(container.textContent).not.toContain('**');
    expect(container.textContent).not.toContain('~~');
  });

  it('preserves literal code, escapes, math and ordinary English parsing', () => {
    const literal = '**方案 A：**固定费用 ~~错误：~~修正';
    const body = [
      `\`${literal}\``, '', '```text', literal, '```', '',
      String.raw`\*\*方案 A：\*\*固定费用 \~\~错误：\~\~修正`, '',
      '$x ** y$ 与 $a \\sim b$；**结论：**成立', '',
      '**Label:**text and **valid** bold',
    ].join('\n');
    const { container } = render(<MarkdownBody body={body} onNotify={noopNotify} />);
    expect(container.querySelector('p code')?.textContent).toBe(literal);
    expect(container.querySelector('.code-card__line-content')?.textContent).toBe(literal);
    expect(container.querySelector('code strong, code del')).toBeNull();
    expect(container.textContent).toContain(literal);
    expect([...container.querySelectorAll('.math-copy-target')].map(node => node.getAttribute('data-math-source'))).toEqual(['x ** y', 'a \\sim b']);
    expect(container.textContent).toContain('**Label:**text');
    expect([...container.querySelectorAll('strong')].map(node => node.textContent)).toEqual(['结论：', 'valid']);
    expect(container.querySelector('del')).toBeNull();
  });

  it('renders closed markup during streaming without inventing missing delimiters', () => {
    const { container, rerender } = render(<MarkdownBody streaming body="**方案 A：" onNotify={noopNotify} />);
    expect(container.querySelector('strong')).toBeNull();
    rerender(<MarkdownBody streaming body="**方案 A：**固定费用；~~错误：" onNotify={noopNotify} />);
    expect(container.querySelector('strong')?.textContent).toBe('方案 A：');
    expect(container.querySelector('del')).toBeNull();
    rerender(<MarkdownBody body="**方案 A：**固定费用；~~错误：~~修正" onNotify={noopNotify} />);
    expect(container.querySelector('del')?.textContent).toBe('错误：');
  });
});

describe('MarkdownBody math rendering', () => {
  it('applies new source and notification props even when the body is unchanged', async () => {
    const first = vi.fn(), latest = vi.fn(), writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'pulsaraDesktop', {value:{writeClipboardText:writeText}, configurable:true});
    const view = render(<MarkdownBody body="内容 $x$" annotationSource onNotify={first}/>);
    expect(view.container.querySelector('[data-source-text]')).toBeTruthy();
    view.rerender(<MarkdownBody body="内容 $x$" onNotify={latest}/>);
    expect(view.container.querySelector('[data-source-text]')).toBeNull();
    fireEvent.click(view.container.querySelector('.math-copy-target')!);
    await waitFor(() => expect(latest).toHaveBeenCalledWith('公式已复制', undefined, 'success'));
    expect(first).not.toHaveBeenCalled();
    expect(writeText).toHaveBeenCalledWith('x');
  });

  it('shows fenced code with a language toolbar, line numbers, safe highlighting and source-only copy', async () => {
    const body = '```python\ndef fibonacci(n):\n    return "<unsafe>"\n```';
    const writeText = vi.fn().mockResolvedValue(undefined);
    const onNotify = vi.fn();
    Object.defineProperty(window, 'pulsaraDesktop', { value: { writeClipboardText: writeText }, configurable: true });
    const { container } = render(<MarkdownBody body={body} onNotify={onNotify} />);
    const card = container.querySelector('.code-card')!;
    expect(card.querySelector('.code-card__toolbar')?.textContent).toContain('PYTHON');
    expect([...card.querySelectorAll('.code-card__line')].map(node => node.getAttribute('data-line'))).toEqual(['1', '2']);
    expect([...card.querySelectorAll('.code-card__line-content')].map(node => node.textContent)).toEqual(['def fibonacci(n):', '    return "<unsafe>"']);
    await waitFor(() => expect(card.querySelectorAll('[class*="code-card__token--"]').length).toBeGreaterThan(0));
    expect([...card.querySelectorAll('.code-card__line-content')].map(node => node.textContent)).toEqual(['def fibonacci(n):', '    return "<unsafe>"']);
    expect(card.querySelector('unsafe')).toBeNull();
    fireEvent.click(card.querySelector('button[aria-label="复制代码"]')!);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('def fibonacci(n):\n    return "<unsafe>"'));
    expect(onNotify).toHaveBeenCalledWith('代码已复制', undefined, 'success');
  });

  it('keeps long text fences in a scrollable card and expands on request', () => {
    const body = `\`\`\`text\n${Array.from({ length: 16 }, (_, i) => `line ${i}`).join('\n')}\n\`\`\``;
    const { container } = render(<MarkdownBody body={body} onNotify={noopNotify} />);
    const card = container.querySelector('.code-card')!;
    const buttons = within(card as HTMLElement);
    expect(card.querySelectorAll('.code-card__line')).toHaveLength(16);
    expect(card.querySelector('.code-card__body.is-expanded')).toBeNull();
    fireEvent.click(buttons.getByRole('button', { name: '按宽度换行' }));
    expect(card.querySelector('.code-card__body.is-wrapped')).not.toBeNull();
    expect([...card.querySelectorAll('.code-card__line')].map(node => node.getAttribute('data-line'))).toEqual(Array.from({ length: 16 }, (_, i) => String(i + 1)));
    expect(buttons.getByRole('button', { name: '关闭自动换行' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(buttons.getByRole('button', { name: '关闭自动换行' }));
    expect(card.querySelector('.code-card__body.is-wrapped')).toBeNull();
    fireEvent.click(buttons.getByRole('button', { name: '展开代码' }));
    expect(card.querySelector('.code-card__body.is-expanded')).not.toBeNull();
    fireEvent.click(buttons.getByRole('button', { name: '收起代码' }));
    expect(card.querySelector('.code-card__body.is-expanded')).toBeNull();
  });

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
    const codeLines = Array.from(container.querySelectorAll('.code-card__line-content'), node => node.textContent);

    expect(codeLines).toEqual([
      'from api import read_file, edit_file',
      'ROOT = "read-only"',
      'exit_code = 0',
    ]);
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
    Object.defineProperty(window, 'pulsaraDesktop', {
      value: { writeClipboardText: writeText },
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
    Object.defineProperty(window, 'pulsaraDesktop', {
      value: { writeClipboardText: writeText },
      configurable: true,
    });
    const { container } = render(<MarkdownBody body="$x$" onNotify={onNotify} />);
    const target = container.querySelector<HTMLElement>('.math-copy-target');

    fireEvent.click(target!);

    await waitFor(() => expect(onNotify).toHaveBeenCalledWith(
      '无法复制公式',
      '系统剪贴板暂时不可用，请重试。',
      'warning',
    ));
  });

  it('supports keyboard copy and ignores a pointer drag across display math', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const onNotify = vi.fn();
    Object.defineProperty(window, 'pulsaraDesktop', {
      value: { writeClipboardText: writeText },
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
