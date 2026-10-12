import {
  CalendarClock,
  Blocks,
  Brain,
  Gauge,
  MessageCircle,
  Ellipsis,
  Settings,
} from 'lucide-react';
import { useId, useRef, useState } from 'react';
import type { AppView } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';

const navigation = [
  { id: 'overview' as const, label: '总览', icon: Gauge },
  { id: 'workbench' as const, label: '会话', icon: MessageCircle },
  { id: 'capabilities' as const, label: '能力', icon: Blocks },
  { id: 'memory' as const, label: '记忆', icon: Brain },
  { id: 'scheduled' as const, label: '定时任务', icon: CalendarClock },
];

interface ActivityRailProps {
  activeView: AppView;
  onNavigate: (view: AppView) => void;
}

export function ActivityRail({ activeView, onNavigate }: ActivityRailProps) {
  const moreId = useId();
  const more = useRef<HTMLDivElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const secondaryNavigation = navigation.filter(item => item.id !== 'workbench');
  const moreActive = secondaryNavigation.some(item => item.id === activeView);
  const navigate = (view: AppView) => {
    if (more.current?.matches(':popover-open')) more.current.hidePopover();
    setMoreOpen(false);
    onNavigate(view);
  };
  return (
    <nav className="activity-rail" aria-label="主要功能">
      <button className="rail-logo" onClick={() => navigate('overview')} aria-label="返回 Pulsara 总览">
        <BrandMark compact />
      </button>

      <div className="rail-nav">
        {navigation.map(({ id, label, icon: Icon }) => (
          <button
            className={`rail-button${id !== 'workbench' ? ' rail-button--secondary' : ''}${activeView === id ? ' is-active' : ''}`}
            key={id}
            onClick={() => navigate(id)}
            aria-label={label}
            aria-current={activeView === id ? 'page' : undefined}
            data-tooltip={label}
          >
            <Icon size={17} strokeWidth={1.8} />
          </button>
        ))}
      </div>

      <div className="rail-nav rail-nav--bottom">
        <button
          className={`rail-button${activeView === 'settings' ? ' is-active' : ''}`}
          onClick={() => navigate('settings')}
          aria-label="设置"
          aria-current={activeView === 'settings' ? 'page' : undefined}
          data-tooltip="设置"
        >
          <Settings size={17} strokeWidth={1.8} />
        </button>
        <span className="rail-avatar" aria-label="本地用户">PL</span>
      </div>
      <div className="rail-more">
        <button type="button" className={`rail-button${moreActive || moreOpen ? ' is-active' : ''}`}
          popoverTarget={moreId} aria-label="更多" aria-expanded={moreOpen} aria-controls={moreId}>
          <Ellipsis size={21} strokeWidth={1.8} />
        </button>
        <div ref={more} id={moreId} className="rail-more-popover" popover="auto" role="group" aria-label="更多功能"
          onToggle={event => setMoreOpen(event.newState === 'open')}>
          {moreOpen && secondaryNavigation.map(({id, label, icon: Icon}) => (
            <button type="button" key={id} className={activeView === id ? 'is-active' : undefined}
              aria-current={activeView === id ? 'page' : undefined} onClick={() => navigate(id)}>
              <Icon size={17} strokeWidth={1.8} />{label}
            </button>
          ))}
        </div>
      </div>
    </nav>
  );
}
