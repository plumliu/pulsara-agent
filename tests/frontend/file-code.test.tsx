import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { FileCode } from '../../frontend/components/file-code';
import { fileCodeLanguage } from '../../frontend/lib/code-highlight';

afterEach(cleanup);

it.each([
  ['script.PY', 'def greet():\r\n\treturn "你好"\r\n\r\n'],
  ['script.js', 'const answer = 42;\n'],
  ['script.ts', 'const answer: number = 42;\n'],
  ['data.json', '{"answer": 42, "ok": true}\n'],
  ['config.yaml', 'name: "hello"\nitems:\n  - 42\n'],
  ['run.sh', 'echo "hello"\nexit 0\n'],
  ['page.html', '<script>alert("text only")</script>\n<div>你好</div>\n'],
  ['style.css', '.card { color: red; padding: 12px; }\n'],
  ['main.c', 'int main() { return 0; }\n'],
  ['main.go', 'package main\nfunc main() { println("hi") }\n'],
  ['main.rs', 'fn main() { let value = 42; }\n'],
  ['query.sql', 'SELECT * FROM users WHERE id = 42;\n'],
  ['Dockerfile', 'FROM python:3.12\nRUN echo "hello"\n'],
])('highlights %s without changing source characters or line numbers', async (filename, source) => {
  const { container } = render(<FileCode filename={filename} source={source} />);
  const code = container.querySelector('code')!;
  expect(code.textContent).toBe(source);
  await waitFor(() => expect(code.querySelector('[class*="code-card__token--"]')).toBeTruthy());
  expect(code.textContent).toBe(source);
  expect(code.querySelector('script, div')).toBeNull();
  expect(container.querySelector('.file-preview-line-numbers')?.textContent)
    .toBe(source.split('\n').map((_, index) => index + 1).join('\n'));
});

it.each([
  ['module.mjs', 'js'], ['module.cts', 'ts'], ['component.tsx', 'ts'],
  ['config.yml', 'yaml'], ['image.svg', 'xml'], ['Dockerfile.dev', 'docker'],
  ['Makefile', 'make'], ['.zshrc', 'bash'], ['.env.local', 'ini'],
  ['notes.txt', 'plain'], ['LICENSE', undefined], ['unknown.custom', undefined],
  ['unknown.constructor', undefined], ['unknown.__proto__', undefined],
])('recognizes %s using existing language grammars', (filename, language) => {
  expect(fileCodeLanguage(filename)).toBe(language);
});

it('keeps unknown files plain and does not show old tokens after changing file or page', async () => {
  const source = 'def greet():\n    return 42\n';
  const view = render(<FileCode filename="main.py" source={source} />);
  await waitFor(() => expect(view.container.querySelector('[class*="code-card__token--"]')).toBeTruthy());
  view.rerender(<FileCode filename="notes.txt" source={source} />);
  expect(view.container.querySelector('[class*="code-card__token--"]')).toBeNull();
  view.rerender(<FileCode filename="main.py" source={'"""unfinished comment\n'} />);
  view.rerender(<FileCode filename="data.unknown" source={'<img src=x onerror=alert(1)>\n'} />);
  await waitFor(() => expect(view.container.querySelector('code')?.textContent).toBe('<img src=x onerror=alert(1)>\n'));
  expect(view.container.querySelector('[class*="code-card__token--"], img')).toBeNull();
});
