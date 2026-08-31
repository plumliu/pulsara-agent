import {
  Blocks,
  Command,
  Gauge,
  MessageCircle,
  Settings,
} from 'lucide-react';
import type { AppView } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';

const navigation = [
  { id: 'overview' as const, label: '总览', icon: Gauge },
  { id: 'workbench' as const, label: '会话', icon: MessageCircle },
  { id: 'capabilities' as const, label: '能力', icon: Blocks },
];

interface ActivityRailProps {
  activeView: AppView;
  onNavigate: (view: AppView) => void;
  onOpenCommand: () => void;
}

export function ActivityRail({ activeView, onNavigate, onOpenCommand }: ActivityRailProps) {
  return (
    <nav className="activity-rail" aria-label="主要功能">
      <button className="rail-logo" onClick={() => onNavigate('overview')} aria-label="返回 Pulsara 总览">
        <BrandMark compact />
      </button>

      <div className="rail-nav">
        {navigation.map(({ id, label, icon: Icon }) => (
          <button
            className={`rail-button${activeView === id ? ' is-active' : ''}`}
            key={id}
            onClick={() => onNavigate(id)}
            aria-label={label}
            aria-current={activeView === id ? 'page' : undefined}
            data-tooltip={label}
          >
            <Icon size={17} strokeWidth={1.8} />
          </button>
        ))}
      </div>

      <div className="rail-nav rail-nav--bottom">
        <button className="rail-button" onClick={onOpenCommand} aria-label="命令面板" data-tooltip="命令面板  ⌘K">
          <Command size={17} strokeWidth={1.8} />
        </button>
        <button
          className={`rail-button${activeView === 'settings' ? ' is-active' : ''}`}
          onClick={() => onNavigate('settings')}
          aria-label="设置"
          data-tooltip="设置"
        >
          <Settings size={17} strokeWidth={1.8} />
        </button>
        <span className="rail-avatar" aria-label="本地用户">PL</span>
      </div>
    </nav>
  );
}
