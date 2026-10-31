import ELK, { type ELK as LayoutEngine, type ElkNode } from 'elkjs/lib/elk-api.js';
import LayoutWorker from 'elkjs/lib/elk-worker.min.js?worker';

export type GraphCardMeasurement = { id: string; width: number; height: number; works: { id: string; y: number }[] };
export type GraphLayoutInput = { cards: GraphCardMeasurement[]; dependencies: { source: string; target: string }[] };
export type GraphLayout = {
  width: number; height: number;
  nodes: { id: string; x: number; y: number; width: number; height: number }[];
  lines: { id: string; controlPoints: { x: number; y: number }[] }[];
};

/** ELK owns layering, fixed Work ports and obstacle routing, off the UI thread. */
export function createCellGraphLayoutEngine() {
  return new ELK({ algorithms: ['layered'], workerFactory: () => new LayoutWorker() });
}

export async function layoutCellGraph(engine: Pick<LayoutEngine, 'layout'>, input: GraphLayoutInput): Promise<GraphLayout> {
  const port = (id: string, side: 'in' | 'out') => JSON.stringify([id, side]);
  const graph: ElkNode = {
    id: 'cell-graph',
    layoutOptions: {
      'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.edgeRouting': 'SPLINES',
      'elk.layered.edgeRouting.splines.mode': 'CONSERVATIVE',
      'elk.spacing.nodeNode': '64', 'elk.layered.spacing.nodeNodeBetweenLayers': '120',
      // Opposite-side fixed Work ports need clearance at spline self-loop corners.
      'elk.spacing.nodeSelfLoop': '32',
      'elk.padding': '[top=240,left=240,bottom=240,right=240]',
      'elk.layered.feedbackEdges': 'true',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
    },
    children: input.cards.map(card => ({
      id: card.id, width: card.width, height: card.height,
      layoutOptions: { 'elk.portConstraints': 'FIXED_POS' },
      ports: card.works.flatMap(work => [
        { id: port(work.id, 'in'), x: 0, y: work.y, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'WEST' } },
        { id: port(work.id, 'out'), x: card.width, y: work.y, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'EAST' } },
      ]),
    })),
    edges: input.dependencies.map(({ source, target }) => ({ id: JSON.stringify([source, target]), sources: [port(source, 'out')], targets: [port(target, 'in')] })),
  };
  const result = await engine.layout(graph);
  if (result.width === undefined || result.height === undefined) throw new Error('协作图布局缺少地图尺寸。');
  return {
    width: result.width, height: result.height,
    nodes: (result.children ?? []).map(node => {
      if (node.x === undefined || node.y === undefined || node.width === undefined || node.height === undefined) throw new Error('协作图布局缺少卡片位置。');
      return { id: node.id, x: node.x, y: node.y, width: node.width, height: node.height };
    }),
    lines: (result.edges ?? []).flatMap(edge => {
      if (!edge.sections?.length) throw new Error('协作图布局缺少依赖路径。');
      return edge.sections.map((section, index) => {
        const controlPoints = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
        if (controlPoints.length < 4 || (controlPoints.length - 1) % 3) throw new Error('协作图布局返回了无效的三次样条路径。');
        return { id: `${edge.id}:${index}`, controlPoints };
      });
    }),
  };
}
