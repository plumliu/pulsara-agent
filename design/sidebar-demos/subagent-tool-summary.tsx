import { ArrowRight, Check, ChevronRight, Copy } from 'lucide-react';
import { useState } from 'react';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';
import './subagent-tool-summary.css';

export function isDemoSubagentTool(trace: ToolTrace) {
  return trace.toolName === 'create_agent_tasks' || trace.toolName === 'spawn_agent';
}

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

function jsonObject(value?: string) {
  try { return object(JSON.parse(value ?? '')); } catch { return undefined; }
}

// Only creation facts from the demo tool call, never live progress inferred from
// an old result. The parent owns the existing task-group inventory and lookup.
export function creationSummary(trace: ToolTrace) {
  if (!isDemoSubagentTool(trace) || trace.resultState !== 'SUCCESS') return undefined;
  const args = jsonObject(trace.argumentsJson);
  const result = jsonObject(trace.resultText);
  if (trace.toolName === 'spawn_agent' && typeof result?.task_id === 'string') {
    return {
      names: [typeof args?.task_name === 'string' ? args.task_name : '子任务'],
      target: { kind: 'task', id: result.task_id },
    };
  }
  if (trace.toolName !== 'create_agent_tasks' || typeof result?.batch_id !== 'string'
    || !Array.isArray(result.tasks) || result.tasks.length === 0) return undefined;
  const definitions = Array.isArray(args?.tasks) ? args.tasks.map(object) : [];
  const names = result.tasks.map((value) => {
    const task = object(value);
    const definition = definitions.find(item => item && typeof task?.task_key === 'string' && item.task_key === task.task_key);
    return typeof definition?.label === 'string' ? definition.label
      : typeof task?.task_key === 'string' ? task.task_key : '子任务';
  });
  return { names, target: { kind: 'batch', id: result.batch_id } };
}

export function DemoSubagentToolSummary({ trace }: { trace: ToolTrace }) {
  const summary = creationSummary(trace);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(trace.resultText ?? '');
      setCopied(true);
      setCopyError(false);
    } catch { setCopyError(true); }
  };

  return (
    <section className="demo-task-result" aria-label="子任务创建摘要">
      {summary && (
        <button
          type="button"
          className="demo-task-result__jump"
          aria-label={`在右侧侧栏查看这 ${summary.names.length} 个子任务`}
          onClick={() => window.parent.postMessage({
            type: 'pulsara-demo-focus-tasks', target: summary.target,
          }, location.origin)}
        >
          <span className="demo-task-result__heading">
            <strong>{summary.names.length} 个子任务</strong>
            <span className="demo-task-result__link">在侧栏查看 <ArrowRight size={14} /></span>
          </span>
          <span className="demo-task-result__names">
            {summary.names.map((name, index) => (
              <span className="demo-task-result__name" key={index}>
                <span className="demo-task-result__number">{String(index + 1).padStart(2, '0')}</span>
                <span>{name}</span>
              </span>
            ))}
          </span>
        </button>
      )}
      {Object.prototype.hasOwnProperty.call(trace, 'resultText') && (
        <details className="demo-task-result__raw" open={summary ? undefined : true}>
          <summary><ChevronRight size={12} />原始结果</summary>
          <div className="demo-task-result__raw-body">
            <button type="button" onClick={() => void copy()} aria-label="复制工具原始结果">
              {copied ? <Check size={12} /> : <Copy size={12} />}{copied ? '已复制' : '复制'}
            </button>
            {copyError && <span role="status">复制失败，请重试</span>}
            <pre>{trace.resultText === '' ? '（空字符串）' : trace.resultText}</pre>
          </div>
        </details>
      )}
    </section>
  );
}
