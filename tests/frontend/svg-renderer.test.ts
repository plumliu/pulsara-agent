import { expect, it } from 'vitest';
import { renderSvg } from '../../frontend/lib/svg-renderer';

const render = async (body: string) => {
  const result = await renderSvg(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 460 130">${body}</svg>`, new AbortController().signal);
  return {...result, document: new DOMParser().parseFromString(result.svg, 'image/svg+xml')};
};

it('preserves SMIL timing, transforms and case-sensitive SVG geometry through sanitizing', async () => {
  const result = await render(`
    <defs><linearGradient id="paint"><stop offset="0" stop-color="blue"/></linearGradient></defs>
    <circle cx="66" cy="65" r="26" fill="url(#paint)">
      <animateTransform attributeName="transform" type="rotate" from="0 66 65" to="360 66 65" dur="3s" repeatCount="indefinite"/>
      <animate attributeName="r" values="7;11;7" dur="1.6s" repeatCount="indefinite"/>
    </circle>
    <rect width="10" height="70"><animate attributeName="height" from="20" to="70" dur="2s"/></rect>
  `);
  expect([result.width, result.height]).toEqual([460, 130]);
  expect(result.document.documentElement.getAttribute('viewBox')).toBe('0 0 460 130');
  expect(result.document.querySelector('linearGradient')).toBeTruthy();
  const transform = result.document.querySelector('animateTransform')!;
  expect(transform.getAttribute('attributeName')).toBe('transform');
  expect(transform.getAttribute('from')).toBe('0 66 65');
  expect(transform.getAttribute('to')).toBe('360 66 65');
  expect(transform.getAttribute('repeatCount')).toBe('indefinite');
  expect(result.document.querySelectorAll('animate')).toHaveLength(2);
  expect(result.document.querySelector('animate')?.getAttribute('values')).toBe('7;11;7');
  expect(result.document.querySelector('animate')?.getAttribute('dur')).toBe('1.6s');
});

it('keeps DOMPurify script, event and animated URL protections', async () => {
  const {document} = await render(`
    <script>alert(1)</script><foreignObject><div>unsafe</div></foreignObject>
    <a href="javascript:alert(1)"><text>link</text>
      <animate attributeName="href" values="javascript:alert(1)" dur="1s"/>
    </a>
    <circle onload="alert(1)"><animate attributeName="r" onbegin="alert(1)" from="javascript:alert(1)" to="20" dur="1s"/></circle>
  `);
  expect(document.querySelector('script, foreignObject, [onload], [onbegin]')).toBeNull();
  expect(document.querySelector('a')?.hasAttribute('href')).toBe(false);
  expect(document.querySelector('a animate')?.hasAttribute('attributeName')).toBe(false);
  expect(document.querySelector('circle animate')?.hasAttribute('from')).toBe(false);
});

it('rejects malformed XML before sanitizing can repair it', async () => {
  await expect(render('<circle></svg>')).rejects.toThrow('Invalid SVG document');
});
