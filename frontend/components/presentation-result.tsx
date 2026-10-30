import { Blocks, Bot, Code2, ChevronRight, FileText, Globe, Server, TerminalSquare, TriangleAlert } from 'lucide-react';
import { presentationLabel, type PresentationResult as Result, type PresentationTarget } from '../lib/presentation-targets';

export function PresentationResult({ result, onOpen }: { result: Result; onOpen?: (targets: PresentationTarget[]) => void }) {
  return <section className="presentation-result" aria-label="展示目标">
    {result.targets.length > 0 && <div className="presentation-result__targets">{result.targets.map((target, index) => {
      const Icon = target.type === 'file' ? FileText : target.type === 'browser' ? Globe : target.type === 'mcp' ? Server : target.type === 'terminal' ? TerminalSquare : target.type === 'agent' ? Bot : target.type === 'cell' ? Code2 : target.name === 'browser' ? Globe : target.name === 'capabilities' ? Blocks : target.name === 'subagents' ? Bot : target.name === 'cells' ? Code2 : TerminalSquare;
      const detail = target.type === 'file' ? target.path : target.type === 'terminal' ? '后台进程' : target.type === 'mcp' ? 'MCP 连接' : undefined;
      return <button key={index} type="button" className="presentation-result__target" disabled={!onOpen} onClick={() => onOpen?.([target])} title={target.type === 'file' ? target.path : undefined}>
        <span className="presentation-result__icon"><Icon size={14} /></span>
        <span className="presentation-result__copy"><strong>{presentationLabel(target)}</strong>{detail && <small>{detail}</small>}</span>
        <ChevronRight size={13} aria-hidden="true" />
      </button>;
    })}</div>}
    {!!result.errors?.length && <ul className="presentation-result__errors">{result.errors.map(error => <li key={error.index}><TriangleAlert size={13} /><span><strong>第 {error.index + 1} 项</strong>{error.message}</span></li>)}</ul>}
  </section>;
}
