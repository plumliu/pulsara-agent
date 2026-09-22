import {
  ArrowRight,
  ArrowUpRight,
  Blocks,
  Bot,
  Brain,
  Database,
  ListFilter,
  MessageCircle,
  Plus,
  Search,
  Settings2,
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
  canCreateSession: boolean;
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
  canCreateSession,
}: OverviewViewProps) {
  const runningSessions = sessions.filter((session) => session.status === 'running').length;
  const runningTasks = agentTasks.filter((task) => task.status === 'running').length;
  const connected = runtimeStatus === 'online';
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const readyModelConfigurations = modelConfigurations.filter((configuration) => configuration.status === 'ready').length;
  const unavailableModelConfigurations = modelConfigurations.length - readyModelConfigurations;
  const embeddingConfigured = localSettings?.dashscope_credentials.embedding_configured === true;
  const rerankConfigured = localSettings?.dashscope_credentials.rerank_configured === true;

  return (
    <section className="surface-view overview-view">
      <header className="page-header">
        <div>
          <span className="page-kicker">工作空间</span>
          <h1>{databaseBlocked ? '准备工作环境' : '工作总览'}</h1>
          <p>{databaseBlocked ? '连接本地数据库后，即可开始使用。' : '开始新的任务，继续未完成的工作。'}</p>
        </div>
        <div className="overview-header-actions">
          <div className={`runtime-health runtime-health--${runtimeStatus}`}>
            <span /><strong>{connectionLabels[runtimeStatus]}</strong>
          </div>
          {databaseBlocked ? (
            <button className="secondary-action" onClick={() => onNavigate('settings')}>
              <Database size={15} />配置 PostgreSQL
            </button>
          ) : (
            <button className="overview-text-action" onClick={() => onNavigate('workbench')}>
              打开工作台<ArrowUpRight size={16} />
            </button>
          )}
        </div>
      </header>

      <div className="overview-content">
          {databaseBlocked ? (
            <DatabaseSetupGuide
              state={databaseState}
              variant="overview"
              onOpenSettings={() => onNavigate('settings')}
            />
          ) : (
            <div className="overview-layout">
              <div className="overview-main">
                <button
                  className="overview-create"
                  aria-label="开始新任务"
                  disabled={!canCreateSession}
                  onClick={onNewSession}
                >
                  <span className="overview-create__copy">
                    <strong>开始新任务</strong>
                    <span>选择工作目录，开始与 Pulsara 协作。</span>
                  </span>
                  <span className="overview-create__plus" aria-hidden="true"><Plus size={24} strokeWidth={1.6} /></span>
                </button>

                <section className="overview-recents" aria-label="最近会话">
                  <header className="overview-section-heading">
                    <h2>最近会话<span className="overview-count" aria-label={`共 ${sessions.length} 个会话`}>{sessions.length}</span></h2>
                    <button className="overview-text-action" onClick={() => onNavigate('workbench')}>
                      查看全部<ArrowRight size={14} />
                    </button>
                  </header>
                  {sessions.length > 0 ? (
                    <div className="overview-session-list">
                      {sessions.slice(0, 4).map((session) => {
                        const presence = getSessionPresence(session, activeSessionId);
                        return (
                          <button className="overview-session" key={session.id} onClick={() => onOpenSession(session.id)}>
                            <span className="overview-session__icon" aria-hidden="true"><MessageCircle size={18} strokeWidth={1.6} /></span>
                            <span className="overview-session__copy">
                              <strong>{session.title}</strong>
                              <small>{session.subtitle}</small>
                            </span>
                            <span className="overview-session__meta">
                              <time>{session.updatedAt}</time>
                              <span className={`overview-session__presence is-${presence}`}>
                                <SessionPresenceGlyph presence={presence} />{sessionPresenceLabels[presence]}
                              </span>
                            </span>
                            <ArrowUpRight className="overview-session__arrow" size={16} />
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="overview-empty">
                      <span className="overview-empty__icon" aria-hidden="true"><MessageCircle size={24} strokeWidth={1.4} /></span>
                      <h3>还没有会话</h3>
                      <p>开始第一个任务，之后可以在这里接着聊。</p>
                    </div>
                  )}
                </section>
              </div>

              <aside className="overview-aside" aria-label="环境与入口">
                <section className="overview-environment" aria-label="运行环境">
                  <header className="overview-section-heading">
                    <h2>运行环境</h2>
                    <button className="overview-settings-action" aria-label="管理配置" title="管理配置" onClick={() => onNavigate('settings')}>
                      <Settings2 size={16} />
                    </button>
                  </header>
                  <dl className="overview-services">
                    <div>
                      <dt><Bot size={16} /><span>模型配置{unavailableModelConfigurations > 0 && <small>{unavailableModelConfigurations} 组不可用</small>}</span></dt>
                      <dd className={readyModelConfigurations > 0 ? 'is-ready' : ''}>{readyModelConfigurations} 组可用</dd>
                    </div>
                    <div>
                      <dt><Search size={16} />记忆检索</dt>
                      <dd className={embeddingConfigured ? 'is-ready' : ''}>{embeddingConfigured ? '已配置' : '未配置'}</dd>
                    </div>
                    <div>
                      <dt><ListFilter size={16} />结果重排</dt>
                      <dd className={rerankConfigured ? 'is-ready' : ''}>{rerankConfigured ? '已配置' : '未配置'}</dd>
                    </div>
                    <div>
                      <dt><Database size={16} />本地数据</dt>
                      <dd className={connected ? 'is-ready' : ''}>{connected ? '就绪' : '等待连接'}</dd>
                    </div>
                  </dl>
                  <section className="overview-activity" aria-label="运行概况">
                    <dl>
                      <div><dt>活动会话</dt><dd>{runningSessions}</dd></div>
                      <div><dt>当前会话运行中任务</dt><dd>{runningTasks}</dd></div>
                    </dl>
                  </section>
                </section>

                <nav className="overview-shortcuts" aria-label="快捷入口">
                  <button onClick={() => onNavigate('memory')}>
                    <span className="overview-shortcut-icon"><Brain size={18} strokeWidth={1.6} /></span>
                    <span><strong>记忆库</strong><small>管理沉淀下来的记忆</small></span>
                    <ArrowUpRight size={15} />
                  </button>
                  <button onClick={() => onNavigate('capabilities')}>
                    <span className="overview-shortcut-icon"><Blocks size={18} strokeWidth={1.6} /></span>
                    <span><strong>能力中心</strong><small>工具、技能与连接</small></span>
                    <ArrowUpRight size={15} />
                  </button>
                </nav>
              </aside>
            </div>
          )}
      </div>
    </section>
  );
}
