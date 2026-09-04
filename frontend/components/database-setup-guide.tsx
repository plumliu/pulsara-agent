import { ArrowRight, Database } from 'lucide-react';
import type { DatabaseDataPlaneState } from '../lib/runtime-adapter';

interface DatabaseSetupGuideProps {
  state: Exclude<DatabaseDataPlaneState, 'ready'>;
  variant: 'overlay' | 'overview';
  onOpenSettings: () => void;
}

const setupCopy: Record<Exclude<DatabaseDataPlaneState, 'ready'>, {
  title: string;
  detail: string;
}> = {
  database_not_configured: {
    title: '先连接 PostgreSQL，再开始会话',
    detail: '会话、任务和记忆需要保存在这台设备的 PostgreSQL 中。填写连接信息并完成初始化后，就可以开始使用。',
  },
  database_configured_unverified: {
    title: '检查已保存的 PostgreSQL 连接',
    detail: '连接信息已经保存，但还没有完成检查。确认本机服务正在运行，再检查连接并初始化数据。',
  },
  database_unavailable: {
    title: 'PostgreSQL 当前无法连接',
    detail: 'Pulsara 无法使用已保存的连接。请检查本机 PostgreSQL 是否启动，并核对连接信息。',
  },
  database_schema_action_required: {
    title: '完成 PostgreSQL 初始化',
    detail: '数据库连接已经可用，但数据结构尚未就绪。运行“初始化 / 升级”后即可继续。',
  },
};

export function DatabaseSetupGuide({
  state,
  variant,
  onOpenSettings,
}: DatabaseSetupGuideProps) {
  const copy = setupCopy[state];

  return (
    <section
      className={`database-setup-guide database-setup-guide--${variant}`}
      aria-label="PostgreSQL 配置引导"
    >
      <div className="database-setup-guide__icon" aria-hidden="true"><Database size={21} /></div>
      <div className="database-setup-guide__copy">
        <span className="page-kicker">本地数据尚未就绪</span>
        <h2>{copy.title}</h2>
        <p>{copy.detail}</p>
      </div>
      {variant === 'overview' && (
        <ol className="database-setup-guide__steps" aria-label="配置步骤">
          <li><span>1</span><div><strong>保存连接</strong><small>填写 Runtime DSN；需要初始化时再填写 Admin DSN。</small></div></li>
          <li><span>2</span><div><strong>检查 PostgreSQL</strong><small>确认数据库、账号和权限可以正常使用。</small></div></li>
          <li><span>3</span><div><strong>初始化数据</strong><small>在设置页运行“初始化 / 升级”。</small></div></li>
        </ol>
      )}
      <button className="primary-action" onClick={onOpenSettings}>
        前往本地服务设置 <ArrowRight size={14} />
      </button>
    </section>
  );
}
