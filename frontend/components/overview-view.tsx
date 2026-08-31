import {
  ArrowRight,
  Bot,
  Database,
  MessageCircle,
  Plus,
  Radio,
  Sparkles,
  TerminalSquare,
  Waypoints,
} from 'lucide-react';
import type {
  AgentTask,
  AppView,
  RuntimeStatus,
  SessionSummary,
} from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';
import {
  getSessionPresence,
  SessionPresenceGlyph,
  sessionPresenceLabels,
} from './session-presence';

interface OverviewViewProps {
  sessions: SessionSummary[];
  activeSessionId: string;
  runtimeStatus: RuntimeStatus;
  agentTasks: AgentTask[];
  onNavigate: (view: AppView) => void;
  onOpenSession: (id: string) => void;
  onNewSession: () => void;
}

const connectionLabels: Record<RuntimeStatus, string> = {
  starting: '正在连接',
  online: '本地服务已连接',
  reconnecting: '正在重新连接',
  offline: '连接已中断',
  failed: '连接失败',
};

const shortConnectionLabels: Record<RuntimeStatus, string> = {
  starting: '连接中',
  online: '正常',
  reconnecting: '重连中',
  offline: '已断开',
  failed: '失败',
};

export function OverviewView({
  sessions,
  activeSessionId,
  runtimeStatus,
  agentTasks,
  onNavigate,
  onOpenSession,
  onNewSession,
}: OverviewViewProps) {
  const runningTasks = agentTasks.filter((task) => task.status === 'running').length;
  const active = sessions.find((session) => session.id === activeSessionId)
    ?? sessions.find((session) => session.status === 'running')
    ?? sessions[0];
  const activePresence = active ? getSessionPresence(active, activeSessionId) : undefined;
  const connected = runtimeStatus === 'online';

  return (
    <section className="surface-view overview-view">
      <header className="surface-topbar">
        <div className="surface-brand"><BrandMark compact /><span>Pulsara 工作台</span></div>
        <div className={`runtime-health runtime-health--${runtimeStatus}`}>
          <span /><strong>{connectionLabels[runtimeStatus]}</strong>
        </div>
      </header>

      <div className="surface-scroll overview-scroll">
        <section className="overview-hero">
          <div>
            <span className="page-kicker">你的本地智能工作台</span>
            <h1>准备好继续<br /><em>航行</em>了吗？</h1>
            <p>从一个清晰目标开始，Pulsara 会在这里整理会话、任务进度与需要你处理的事项。</p>
            <div className="hero-actions">
              <button className="primary-action" onClick={onNewSession}><Plus size={15} /> 开始新任务</button>
              <button className="secondary-action" onClick={() => onNavigate('workbench')}><MessageCircle size={14} /> 打开工作台</button>
            </div>
          </div>
          <div className="hero-orbit" aria-hidden="true">
            <span className="hero-orbit__ring ring-a" /><span className="hero-orbit__ring ring-b" /><span className="hero-orbit__ring ring-c" />
            <span className="hero-orbit__sat satellite-a" /><span className="hero-orbit__sat satellite-b" /><span className="hero-orbit__sat satellite-c" />
            <div className="hero-orbit__core"><BrandMark /></div>
            <span className="orbit-coordinate">私密 · 仅限本机</span>
          </div>
        </section>

        <section className="metric-grid">
          <article><div className="metric-icon amber"><Radio size={15} /></div><div><strong>{sessions.filter((session) => session.status === 'running').length}</strong><span>活动会话</span></div><small>共 {sessions.length} 个会话</small></article>
          <article><div className="metric-icon blue"><Bot size={15} /></div><div><strong>{runningTasks}</strong><span>进行中任务</span></div><small>共 {agentTasks.length} 个子任务</small></article>
          <article><div className="metric-icon green"><Sparkles size={15} /></div><div><strong>{sessions.length}</strong><span>最近会话</span></div><small>可随时继续</small></article>
          <article><div className="metric-icon violet"><Database size={15} /></div><div><strong>{connected ? '正常' : '—'}</strong><span>本地数据</span></div><small>{connected ? '已经就绪' : '等待连接'}</small></article>
        </section>

        <div className="overview-columns">
          <section className="overview-card active-mission-card">
            <header className="overview-card__header"><div><span className="page-kicker">当前工作</span><h2>正在发生</h2></div><button onClick={() => onNavigate('workbench')}>打开工作台 <ArrowRight size={12} /></button></header>
            {active && activePresence ? (
              <button className="active-mission" onClick={() => onOpenSession(active.id)}>
                <span className={`mission-presence-column mission-presence-column--${activePresence}`}><SessionPresenceGlyph presence={activePresence} /><b>{sessionPresenceLabels[activePresence]}</b></span>
                <span className="mission-copy"><strong>{active.title}</strong><small>{active.subtitle}</small><span className="mission-detail"><span><TerminalSquare size={10} /> 保存在本机</span></span></span>
                <ArrowRight size={15} />
              </button>
            ) : (
              <div className="empty-state"><Sparkles size={22} /><h3>还没有任务</h3><p>创建第一个会话后，进度会在这里出现。</p></div>
            )}
          </section>

          <section className="overview-card activity-card">
            <header className="overview-card__header"><div><span className="page-kicker">连接状态</span><h2>本地运行</h2></div><span className="trend-chip">{shortConnectionLabels[runtimeStatus]}</span></header>
            <div className="system-map">
              <div className="system-node"><Waypoints size={14} /><span><strong>Pulsara 服务</strong><small>负责会话和任务</small></span><b>{shortConnectionLabels[runtimeStatus]}</b></div>
              <div className="system-line" />
              <div className="system-node"><TerminalSquare size={14} /><span><strong>当前页面</strong><small>自动保持连接</small></span><b>{connected ? '已连接' : '等待中'}</b></div>
            </div>
          </section>
        </div>

        <div className="overview-columns overview-columns--lower">
          <section className="overview-card recent-card">
            <header className="overview-card__header"><div><span className="page-kicker">最近记录</span><h2>最近会话</h2></div><button onClick={() => onNavigate('workbench')}>查看全部</button></header>
            <div className="recent-table">
              {sessions.slice(0, 4).map((session) => {
                const presence = getSessionPresence(session, activeSessionId);
                return (
                  <button key={session.id} onClick={() => onOpenSession(session.id)}>
                    <SessionPresenceGlyph presence={presence} /><span><strong>{session.title}</strong><small>{session.subtitle}</small></span><span className={`table-state table-state--${presence}`}>{sessionPresenceLabels[presence]}</span><time>{session.updatedAt}</time><ArrowRight size={12} />
                  </button>
                );
              })}
              {sessions.length === 0 && <div className="empty-state"><MessageCircle size={22} /><h3>会话列表为空</h3><p>创建任务后，它会出现在这里。</p></div>}
            </div>
          </section>

          <section className="overview-card system-card">
            <header className="overview-card__header"><div><span className="page-kicker">这台设备</span><h2>本地工作空间</h2></div></header>
            <div className="system-map">
              <div className="system-node"><Waypoints size={14} /><span><strong>任务服务</strong><small>运行你的会话</small></span><b>{shortConnectionLabels[runtimeStatus]}</b></div>
              <div className="system-line" />
              <div className="system-node"><Database size={14} /><span><strong>会话数据</strong><small>保存在本机</small></span><b>{connected ? '就绪' : '等待'}</b></div>
              <div className="system-line" />
              <div className="system-node"><TerminalSquare size={14} /><span><strong>浏览器连接</strong><small>自动恢复</small></span><b>{connected ? '正常' : '未连接'}</b></div>
            </div>
            <footer><code>仅限这台设备</code></footer>
          </section>
        </div>
      </div>
    </section>
  );
}
