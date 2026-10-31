import ELK from 'elkjs/lib/elk.bundled.js';
import { expect, it } from 'vitest';
import { layoutCellGraph, type GraphCardMeasurement, type GraphLayout } from '../../frontend/components/cell-graph-layout';

const card = (id: string, works: string[]): GraphCardMeasurement => ({ id, width: 240, height: 80 + 32 * works.length, works: works.map((id, index) => ({ id, y: 64 + 32 * index })) });
const dependency = (source: string, target: string) => ({ source, target });
function expectNoCardCrossings(layout: GraphLayout) {
  for (const line of layout.lines) {
    expect((line.controlPoints.length - 1) % 3).toBe(0);
    for (let index = 1; index < line.controlPoints.length; index += 3) {
      const [a, b, c, d] = line.controlPoints.slice(index - 1, index + 3);
      for (let sample = 0; sample <= 100; sample++) {
        const t = sample / 100, u = 1 - t;
        const x = u ** 3 * a.x + 3 * u ** 2 * t * b.x + 3 * u * t ** 2 * c.x + t ** 3 * d.x;
        const y = u ** 3 * a.y + 3 * u ** 2 * t * b.y + 3 * u * t ** 2 * c.y + t ** 3 * d.y;
        for (const node of layout.nodes) {
          const intersects = x > node.x + .001 && x < node.x + node.width - .001 && y > node.y + .001 && y < node.y + node.height - .001;
          expect(intersects, `${line.id} crosses ${node.id}`).toBe(false);
        }
      }
    }
  }
}

it('places predecessor Agents before their shared reviewer and routes both exact Work ports outside cards', async () => {
  const engine = new ELK({ algorithms: ['layered'] });
  const result = await layoutCellGraph(engine, {
    cards: [card('pricing', ['price']), card('capability', ['capability']), card('review', ['review', 'later']), card('observer', [])],
    dependencies: [dependency('price', 'review'), dependency('capability', 'review')],
  });
  const reviewer = result.nodes.find(node => node.id === 'review')!;
  for (const id of ['pricing', 'capability']) {
    const source = result.nodes.find(node => node.id === id)!;
    expect(source.x + source.width).toBeLessThan(reviewer.x);
  }
  expect(result.lines).toHaveLength(2);
  for (const line of result.lines) expect(line.controlPoints.at(-1)).toEqual({ x: reviewer.x, y: reviewer.y + 64 });
  expectNoCardCrossings(result);
});

it('routes feedback between reused Agents and dependencies inside one Agent outside their cards', async () => {
  const engine = new ELK({ algorithms: ['layered'] });
  const result = await layoutCellGraph(engine, {
    cards: [card('agent:a', ['a:first', 'a:second', 'a:third']), card('agent:b', ['b:first'])],
    dependencies: [dependency('a:first', 'b:first'), dependency('b:first', 'a:second'), dependency('a:second', 'a:third')],
  });
  expect(result.lines).toHaveLength(3);
  expectNoCardCrossings(result);
});

it('routes a long dependency around the intervening Agent instead of through it', async () => {
  const engine = new ELK({ algorithms: ['layered'] });
  const result = await layoutCellGraph(engine, {
    cards: [card('a', ['work:a']), card('b', ['work:b']), card('c', ['work:c'])],
    dependencies: [dependency('work:a', 'work:b'), dependency('work:b', 'work:c'), dependency('work:a', 'work:c')],
  });
  expect(result.lines).toHaveLength(3);
  expectNoCardCrossings(result);
});
