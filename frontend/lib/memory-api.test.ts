import { describe, expect, it } from 'vitest';
import { decodeMemoryStream } from './memory-api';

const header = { type: 'HEADER', root: 'fact:你好', view: 'global', workspace_id: null, disposition: 'READY' };
const end = { type: 'END', counts: { HEADER: 1 } };
const line = (value: unknown) => `${JSON.stringify(value)}\n`;

function response(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      // Split even multibyte characters and JSON tokens across physical chunks.
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    },
  }));
}

describe('memory confirmation stream', () => {
  it('requires and preserves the complete ordered confirmation', async () => {
    expect(await decodeMemoryStream(response(line(header) + line(end)))).toEqual([header, end]);
  });

  it.each([
    line(header),
    line(header) + JSON.stringify(end),
    line(header) + line({ type: 'END', counts: { HEADER: 2 } }),
    line(header) + line(end) + line(header),
    line(end),
    line(header) + line({ type: 'UNKNOWN' }) + line(end),
  ])('rejects incomplete or inconsistent product records', async (text) => {
    await expect(decodeMemoryStream(response(text))).rejects.toThrow();
  });
});
