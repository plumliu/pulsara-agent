import { Minus, PanelLeft, PanelRight, Square, X } from 'lucide-react';
import { desktopHost } from '../lib/desktop-host';
export function DesktopTitlebar({ title, workbench = false, onToggleSidebar, onToggleWorkspace }: { title: string; workbench?: boolean; onToggleSidebar: () => void; onToggleWorkspace: () => void }) {
  const host = desktopHost();
  return <header className={`desktop-titlebar ${host?.platform === 'darwin' ? 'is-macos' : ''}`} onDoubleClick={event => { if (!(event.target as HTMLElement).closest('button')) void host?.toggleMaximize(); }}>
    <button type="button" aria-label="切换会话侧栏" onClick={onToggleSidebar}><PanelLeft size={15} /></button>{!workbench && <><span className="desktop-titlebar__title">{title || 'Pulsara'}</span><button type="button" aria-label="切换标签工作区" onClick={onToggleWorkspace}><PanelRight size={15} /></button></>}
  </header>;
}

/** Linux controls belong to the shell; Windows and macOS use native controls. */
export function DesktopWindowControls() {
  const host = desktopHost();
  if (host?.platform !== 'linux') return null;
  return <div className="desktop-window-controls" role="group" aria-label="窗口控制"><button aria-label="最小化" onClick={() => void host.minimize()}><Minus size={14} /></button><button aria-label="最大化或还原" onClick={() => void host.toggleMaximize()}><Square size={12} /></button><button aria-label="关闭窗口" onClick={() => void host.closeWindow()}><X size={15} /></button></div>;
}
