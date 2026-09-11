import { createRoot } from 'react-dom/client';
import type { ComponentProps } from 'react';
import { WorkbenchView } from '../../frontend/components/workbench-view';
import type { Message } from '../../frontend/lib/pulsara-types';
import '../../frontend/app/styles/base.css';
import '../../frontend/app/styles/workbench.css';
import './production-workbench.css';

// Presentation fixtures, not a runtime connection or evidence of real execution.
// WorkbenchView, its message/trace components, Markdown and design tokens are
// reused from production. The demo build replaces only the subagent tool's
// expanded body. Details live in the outer graph, so this proposed projection
// intentionally has no message.subagentRuns.
const taskDefinitions = [
  { task_key: 'scope', label: '梳理现有协议', task: '核对用户控制、权限与工具结算的现有边界。', depends_on: [] },
  { task_key: 'api', label: '实现控制入口', task: '接入精确目标的取消请求与原 command 查询。', depends_on: ['scope'] },
  { task_key: 'ui', label: '搭建运行面板', task: '组织任务组入口与折叠日志。', depends_on: ['scope'] },
  { task_key: 'test', label: '验证取消路径', task: '验证迟到请求、权限检查和效果结算。', depends_on: ['api'] },
  { task_key: 'copy', label: '核对交互文案', task: '核对停止、取消、终止与未知结果的表达。', depends_on: ['ui'] },
  { task_key: 'review', label: '汇总与最终检查', task: '汇总测试与文案结果。', depends_on: ['test', 'copy'] },
];

const messages: Message[] = [
  {
    id: 'demo-prompt', role: 'user', userKind: 'prompt', time: '14:28',
    body: '把子任务和后台命令的控制入口接起来。可以并行推进，保持现有的工具结算语义。',
  },
  {
    id: 'demo-read', turnId: 'demo-root', role: 'assistant', assistantKind: 'tool-request',
    time: '14:28', status: 'completed', body: '',
    traces: [{
      id: 'demo-read-call', kind: 'read', toolName: 'read_file', title: '读取文件',
      subtitle: '已完成', status: 'completed', meta: '操作完成',
      argumentsJson: JSON.stringify({ path: 'src/pulsara_agent/conversation_kernel/host.py' }),
      resultText: 'src/pulsara_agent/conversation_kernel/host.py\n\nasync def stop_current_turn(...):\n    …',
      resultState: 'SUCCESS',
    }],
  },
  {
    id: 'demo-dispatch', turnId: 'demo-root', role: 'assistant', assistantKind: 'tool-request',
    time: '14:28', status: 'completed',
    body: '我把实现拆成了两条并行路径：控制入口与运行面板。每条路径完成后，再分别做验证和文案核对。',
    traces: [{
      id: 'demo-create-call', kind: 'artifact', toolName: 'create_agent_tasks', title: '创建子任务',
      subtitle: '已完成', status: 'completed', meta: '操作完成',
      argumentsJson: JSON.stringify({ tasks: taskDefinitions }),
      resultText: JSON.stringify({ batch_id: 'control', tasks: taskDefinitions.map(task => ({
        task_key: task.task_key, task_id: task.task_key,
        status: task.depends_on.length ? 'waiting_dependency' : 'active',
      })) }, null, 2),
      resultState: 'SUCCESS',
    }],
  },
  {
    id: 'demo-continue', turnId: 'demo-root', role: 'assistant', assistantKind: 'live',
    time: '14:40', status: 'running',
    body: '任务已经派发。我继续核对反馈接纳边界，避免终止命令影响主助手的当前工作。',
  },
];

type Props = ComponentProps<typeof WorkbenchView>;
const notify: Props['onNotify'] = (title, detail) => {
  window.parent.postMessage({ type: 'pulsara-demo-notice', text: detail ? `${title} · ${detail}` : title }, location.origin);
};
const unavailable = () => notify('仅演示，未执行');
const props: Props = {
  workspace: { id: 'demo-workspace', kind: 'project', name: 'pulsara_agent', path: '/Users/plumliu/Desktop/python_workspace/pulsara_agent' },
  session: { id: 'demo-session', title: '取消与后台命令控制', subtitle: '静态演示', status: 'running', updatedAt: '现在', live: true },
  messages,
  activePlanMode: false, isRunning: true, inspectorOpen: true,
  queuedCount: 0, queuedPrompts: [], localSubmissions: [], runtimeStatus: 'online',
  modelConfigurations: [{
    id: 'demo-model', source: 'user_declared', route_id: 'demo', route_name: 'OpenRouter',
    model_id: 'demo-model', display_name: '演示模型', wire_api: 'openai_chat_completions',
    base_url: '', status: 'ready', authentication: 'none', credential_configured: false,
    reasoning: { kind: 'unavailable' },
  }],
  modelCallBinding: { connection_id: 'demo-model', reasoning: null },
  canControl: true, isObserver: false, permission: 'accept-edits', skills: [],
  focusTaskRevision: 0, focusTaskHighlighted: false, artifactOwnerKey: 'static-demo',
  onReconnect: unavailable, onTakeControl: unavailable, onOpenSidebar: unavailable,
  onNewSession: unavailable, onToggleInspector: unavailable, onOpenModelSettings: unavailable,
  onStop: unavailable, onPermissionChange: unavailable,
  onFork: async () => unavailable(), onCompact: async () => unavailable(),
  onModelCallBindingChange: async () => unavailable(),
  onSend: async () => { unavailable(); return false; },
  onReadInteraction: async () => { throw new Error('Demo has no interactions'); },
  onResolveInteraction: async () => false,
  onReadToolArtifact: async () => { throw new Error('Demo has no artifact service'); },
  onNotify: notify,
};

createRoot(document.getElementById('root')!).render(<WorkbenchView {...props} />);
