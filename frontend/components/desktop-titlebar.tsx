import { Minus, PanelLeft, Square, X } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { desktopHost } from '../lib/desktop-host';

interface DesktopTitlebarProps {
  conversation?: boolean;
  onToggleSidebar?: () => void;
  onTabsHost?: (host: HTMLDivElement | null) => void;
}

export function DesktopTitlebar({ conversation = false, onToggleSidebar, onTabsHost }: DesktopTitlebarProps) {
  const host = desktopHost();
  const header = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = header.current;
    if (!element || !host) return;
    const onDoubleClick = (event: MouseEvent) => {
      if (event.target instanceof Element && !event.target.closest('button, [role="tab"], [data-canvas-no-drag]')) {
        void host.toggleMaximize();
      }
    };
    // The tab strip is portalled here. Follow the physical titlebar hit region.
    element.addEventListener('dblclick', onDoubleClick);
    return () => element.removeEventListener('dblclick', onDoubleClick);
  }, [host]);

  return (
    <header className="desktop-titlebar" ref={header}>
      {onToggleSidebar && <button type="button" title="切换会话侧栏" aria-label="切换会话侧栏" onClick={onToggleSidebar}><PanelLeft size={15} /></button>}
      {conversation
        ? <div className="desktop-titlebar__session-tabs" ref={onTabsHost} />
        : <span className="desktop-titlebar__title">Pulsara</span>}
      {host?.platform === 'linux' && <div className="desktop-window-controls" role="group" aria-label="窗口控制">
        <button type="button" aria-label="最小化" title="最小化" onClick={() => void host.minimize()}><Minus size={14} /></button>
        <button type="button" aria-label="最大化或还原" title="最大化或还原" onClick={() => void host.toggleMaximize()}><Square size={12} /></button>
        <button type="button" aria-label="关闭窗口" title="关闭窗口" onClick={() => void host.closeWindow()}><X size={15} /></button>
      </div>}
    </header>
  );
}
