import {
  ArrowRight,
  Bot,
  Database,
  ListFilter,
  MessageCircle,
  Plus,
  Radio,
  Search,
  Sparkles,
} from 'lucide-react';
import type {
  AgentTask,
  AppView,
  RuntimeStatus,
  SessionSummary,
} from '../lib/pulsara-types';
import type {
  DatabaseDataPlaneState,
  LocalSettingsSummary,
  ModelConfigurationSummary,
} from '../lib/runtime-adapter';
import { BrandMark } from './brand-mark';
import { DatabaseSetupGuide } from './database-setup-guide';
import {
  getSessionPresence,
  SessionPresenceGlyph,
  sessionPresenceLabels,
} from './session-presence';

interface OverviewViewProps {
  sessions: SessionSummary[];
  activeSessionId: string;
  runtimeStatus: RuntimeStatus;
  databaseState?: DatabaseDataPlaneState;
  agentTasks: AgentTask[];
  localSettings?: LocalSettingsSummary;
  modelConfigurations: ModelConfigurationSummary[];
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

export function OverviewView({
  sessions,
  activeSessionId,
  runtimeStatus,
  databaseState,
  agentTasks,
  localSettings,
  modelConfigurations,
  onNavigate,
  onOpenSession,
  onNewSession,
}: OverviewViewProps) {
  const runningTasks = agentTasks.filter((task) => task.status === 'running').length;
  const connected = runtimeStatus === 'online';
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const readyModelConfigurations = modelConfigurations.filter((configuration) => configuration.status === 'ready').length;
  const unavailableModelConfigurations = modelConfigurations.length - readyModelConfigurations;
  const embeddingConfigured = localSettings?.dashscope_credentials.embedding_configured === true;
  const rerankConfigured = localSettings?.dashscope_credentials.rerank_configured === true;

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
            <span className="page-kicker">{databaseBlocked ? '完成本机设置' : '你的本地智能工作台'}</span>
            <h1>{databaseBlocked ? <>先准备好<br /><em>本地数据</em></> : <>准备好继续<br /><em>航行</em>了吗？</>}</h1>
            <p>{databaseBlocked ? '连接 PostgreSQL 后，Pulsara 才能安全保存会话、任务进度和记忆。' : '从一个清晰目标开始，Pulsara 会在这里整理会话、任务进度与需要你处理的事项。'}</p>
            <div className="hero-actions">
              {databaseBlocked ? (
                <button className="primary-action" onClick={() => onNavigate('settings')}><Database size={15} /> 配置 PostgreSQL</button>
              ) : (
                <>
                  <button className="primary-action" onClick={onNewSession}><Plus size={15} /> 开始新任务</button>
                  <button className="secondary-action" onClick={() => onNavigate('workbench')}><MessageCircle size={14} /> 打开工作台</button>
                </>
              )}
            </div>
          </div>
          <div className="hero-orbit" aria-hidden="true">
            <span className="hero-orbit__ring ring-a" /><span className="hero-orbit__ring ring-b" /><span className="hero-orbit__ring ring-c" />
            <span className="hero-orbit__sat satellite-a" /><span className="hero-orbit__sat satellite-b" /><span className="hero-orbit__sat satellite-c" />
            <div className="hero-orbit__core"><BrandMark /></div>
            <span className="orbit-coordinate">私密 · 仅限本机</span>
          </div>
        </section>

        {databaseBlocked ? (
          <DatabaseSetupGuide
            state={databaseState}
            variant="overview"
            onOpenSettings={() => onNavigate('settings')}
          />
        ) : <>
        <section className="metric-grid">
          <article><div className="metric-icon amber"><Radio size={15} /></div><div><strong>{sessions.filter((session) => session.status === 'running').length}</strong><span>活动会话</span></div><small>共 {sessions.length} 个会话</small></article>
          <article><div className="metric-icon blue"><Bot size={15} /></div><div><strong>{runningTasks}</strong><span>进行中任务</span></div><small>共 {agentTasks.length} 个子任务</small></article>
          <article><div className="metric-icon green"><Sparkles size={15} /></div><div><strong>{sessions.length}</strong><span>最近会话</span></div><small>可随时继续</small></article>
          <article><div className="metric-icon violet"><Database size={15} /></div><div><strong>{connected ? '正常' : '—'}</strong><span>本地数据</span></div><small>{connected ? '已经就绪' : '等待连接'}</small></article>
        </section>

        <div className="overview-columns">
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
            <header className="overview-card__header"><div><span className="page-kicker">本机设置</span><h2>当前配置</h2></div></header>
            <div className="system-map">
              <div className="system-node"><Bot size={14} /><span><strong>模型配置</strong><small>{readyModelConfigurations} 组可用{unavailableModelConfigurations > 0 ? ` · ${unavailableModelConfigurations} 组不可用` : ''}</small></span><b className={modelConfigurations.length === 0 ? 'is-inactive' : undefined}>{modelConfigurations.length} 组</b></div>
              <div className="system-line" />
              <div className="system-node"><Search size={14} /><span><strong>记忆检索</strong><small>DashScope Embedding</small></span><b className={embeddingConfigured ? undefined : 'is-inactive'}>{embeddingConfigured ? '已配置' : '未配置'}</b></div>
              <div className="system-line" />
              <div className="system-node"><ListFilter size={14} /><span><strong>结果重排</strong><small>DashScope Rerank</small></span><b className={rerankConfigured ? undefined : 'is-inactive'}>{rerankConfigured ? '已配置' : '未配置'}</b></div>
            </div>
          </section>
        </div>
        </>}
      </div>
    </section>
  );
}
