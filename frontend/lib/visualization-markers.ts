import type { Root } from 'mdast';

/** The Markdown parser owns block boundaries. Verify explicit closing because
 * CommonMark also emits a code node for an unclosed EOF fence. */
export function remarkVisualizationMarkers() {
  return (tree: Root, file: {value: unknown}) => {
    const body = String(file.value); let ordinal = 0;
    for (const node of tree.children) {
      if (node.type !== 'code' || node.lang !== 'pulsara-viz' || node.meta?.trim()) continue;
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start === undefined || end === undefined) continue;
      const lines = body.slice(start, end).replace(/\r\n?/g, '\n').split('\n');
      const opening = /^ {0,3}(`{3,}|~{3,})pulsara-viz[ \t]*$/.exec(lines[0]);
      // Whitespace between fence and info string is legal CommonMark.
      const spaced = opening ?? /^ {0,3}(`{3,}|~{3,})[ \t]+pulsara-viz[ \t]*$/.exec(lines[0]);
      if (!spaced || lines.length < 2 || !new RegExp(`^ {0,3}${spaced[1][0]}{${spaced[1].length},}[ \\t]*$`).test(lines.at(-1)!)) continue;
      const count = node.value.replace(/\r\n?/g, '\n').split('\n').filter(line => line.replace(/^[ \t]+|[ \t]+$/g, '') !== '').length || 1;
      node.data = {...node.data, hProperties: {'data-visualization-ordinal': ordinal, 'data-visualization-count': count}};
      ordinal += count;
    }
  };
}
