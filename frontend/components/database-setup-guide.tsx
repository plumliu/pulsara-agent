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
    title: '正在准备内置数据服务',
    detail: 'Pulsara 正在准备自己的 PostgreSQL。请在应用启动页查看准备进度或错误详情。',
  },
  database_configured_unverified: {
    title: '检查内置数据服务',
    detail: '应用专用数据服务尚未完成检查，请查看服务状态。',
  },
  database_unavailable: {
    title: 'PostgreSQL 当前无法连接',
    detail: 'Pulsara 暂时无法连接应用管理的数据服务，请查看启动诊断并重试。',
  },
  database_schema_action_required: {
    title: '完成 PostgreSQL 初始化',
    detail: '内置数据库已经启动，应用正在准备当前版本的数据结构。',
  },
  database_reset_required: {
    title: '现有数据需要重置',
    detail: '现有数据结构与当前版本不兼容。请在本地服务设置中查看重置影响并确认；此操作会清空应用数据库中的历史。',
  },
  database_resetting: {
    title: '正在重置本地数据',
    detail: '正在停止数据服务并重新初始化，请等待操作完成。',
  },
  database_restart_required: {
    title: '请重启 Pulsara',
    detail: '数据服务已关闭。重启 Pulsara 后将重新检查数据库；重新打开应用后将恢复数据服务。',
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
      <button className="primary-action" onClick={onOpenSettings}>
        前往通用设置 <ArrowRight size={14} />
      </button>
    </section>
  );
}
