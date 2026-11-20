import { autoUpdate, computePosition, flip, hide as hideAnchor, offset, shift } from '@floating-ui/dom';
import type { LucideIcon } from 'lucide-react';
import {
  createContext, useCallback, useContext, useId, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { desktopHost, type TextEditAction } from '../lib/desktop-host';

export interface MenuItem {
  key?: string;
  label: string;
  icon: LucideIcon;
  title?: string;
  disabled?: boolean;
  danger?: boolean;
  run: () => void;
}

export interface ContextMenuState {
  x: number;
  y: number;
  name: string;
  keyboard: boolean;
  preserveSelection?: boolean;
  items: MenuItem[];
}

type MenuEvent = ReactMouseEvent<HTMLElement> | ReactKeyboardEvent<HTMLElement>;
type ReadItems = (signal: AbortSignal) => Promise<MenuItem[]>;

interface PopupRequest {
  menu: ContextMenuState;
  owner?: HTMLElement;
  anchor?: HTMLElement;
  reading?: AbortController;
}

export async function editText(action: TextEditAction) {
  const host = desktopHost();
  if (!host) throw new Error('桌面剪贴板不可用。');
  await host.editText(action);
}

function available(owner?: Element) {
  return !owner || (owner.isConnected && !owner.closest('[hidden],[inert]'));
}

function focusMenuElement(element: HTMLElement, preserveSelection?: boolean) {
  const selection = preserveSelection ? element.ownerDocument.getSelection() : null;
  const range = selection?.rangeCount === 1 && !selection.isCollapsed ? selection.getRangeAt(0).cloneRange() : null;
  const backward = range && selection?.anchorNode === range.endContainer && selection.anchorOffset === range.endOffset;
  element.focus({preventScroll: true});
  if (!selection || !range || range.collapsed) return;
  const { startContainer, startOffset, endContainer, endOffset } = range;
  if (!startContainer.isConnected || !endContainer.isConnected) return;
  const [anchor, anchorOffset, focus, focusOffset] = backward
    ? [endContainer, endOffset, startContainer, startOffset] as const
    : [startContainer, startOffset, endContainer, endOffset] as const;
  if (selection.anchorNode !== anchor || selection.anchorOffset !== anchorOffset
    || selection.focusNode !== focus || selection.focusOffset !== focusOffset) {
    selection.setBaseAndExtent(anchor, anchorOffset, focus, focusOffset);
  }
}

const itemKey = (item?: MenuItem) => item?.key ?? item?.label;

function MenuPopup({ request, dismiss }: { request: PopupRequest; dismiss: () => void }) {
  const {menu, owner, anchor} = request;
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const enabled = menu.items.filter(item => !item.disabled);
  const [current, setCurrent] = useState<string | undefined>(menu.keyboard ? itemKey(enabled[0]) : undefined);
  const active = enabled.find(item => itemKey(item) === current);
  const activeRef = useRef(active);
  activeRef.current = active;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const run = useCallback((item: MenuItem) => {
    dismiss();
    if (!item.disabled && available(owner)) item.run();
  }, [dismiss, owner]);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || anchor) return;
    const {width, height} = element.getBoundingClientRect();
    element.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, menu.x + 3))}px`;
    const below = menu.y + 5;
    const top = below + height > window.innerHeight - 8 ? menu.y - height - 4 : below;
    element.style.top = `${Math.max(8, Math.min(window.innerHeight - height - 8, top))}px`;
  }, [anchor, menu]);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || !anchor) return;
    element.style.visibility = 'hidden';
    let disposed = false;
    let revision = 0;
    let revealed = false;
    const update = () => {
      const attempt = ++revision;
      void computePosition(anchor, element, {
        placement: 'bottom-end', strategy: 'fixed',
        middleware: [offset(1), flip({padding: 8}), shift({padding: 8, crossAxis: true}), hideAnchor()],
      }).then(({x, y, middlewareData}) => {
        if (disposed || attempt !== revision) return;
        if (middlewareData.hide?.referenceHidden || !available(anchor)) {
          dismiss();
          return;
        }
        Object.assign(element.style, {left: `${x}px`, top: `${y}px`, visibility: 'visible'});
        if (!revealed && menu.keyboard) focusMenuElement(element, menu.preserveSelection);
        revealed = true;
      });
    };
    const stop = autoUpdate(anchor, element, update);
    return () => { disposed = true; stop(); };
  }, [anchor, dismiss, menu.keyboard, menu.preserveSelection]);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const previous = document.activeElement;
    if (menu.keyboard && !anchor) focusMenuElement(element, menu.preserveSelection);
    const outside = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || element.contains(event.target) || anchor?.contains(event.target)) return;
      dismiss();
    };
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && element.contains(event.target)) return;
      if (!anchor) dismiss();
    };
    const key = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (event.key === 'Tab') { dismiss(); return; }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        dismiss();
        return;
      }
      const items = enabledRef.current;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        event.stopImmediatePropagation();
        const item = activeRef.current ?? items[0];
        if (item) run(item);
        return;
      }
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const index = items.findIndex(item => item === activeRef.current);
      if (event.key === 'Home') { setCurrent(itemKey(items[0])); return; }
      if (event.key === 'End') { setCurrent(itemKey(items.at(-1))); return; }
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      const next = index < 0 ? (delta > 0 ? 0 : items.length - 1) : (index + delta + items.length) % items.length;
      setCurrent(itemKey(items[next]));
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', key, true);
    window.addEventListener('blur', dismiss);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', onScroll, true);
    const observer = typeof MutationObserver === 'undefined' || !owner ? undefined : new MutationObserver(() => {
      if (!available(owner)) dismiss();
    });
    for (let node: HTMLElement | null | undefined = owner; observer && node; node = node.parentElement) {
      observer.observe(node, {childList: true, attributes: true, attributeFilter: ['hidden', 'inert']});
    }
    return () => {
      observer?.disconnect();
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', key, true);
      window.removeEventListener('blur', dismiss);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', onScroll, true);
      if (document.activeElement === element && previous instanceof HTMLElement && available(previous)) {
        focusMenuElement(previous, menu.preserveSelection);
      }
    };
  }, [anchor, dismiss, menu.keyboard, menu.preserveSelection, owner, run]);

  const activeIndex = active ? menu.items.indexOf(active) : -1;
  return <div ref={ref} className="context-menu-popup" data-annotation-ui="" role="menu" aria-label={menu.name}
    aria-activedescendant={activeIndex < 0 ? undefined : `${id}-${activeIndex}`} tabIndex={-1}
    onPointerDown={event => event.preventDefault()}
    onPointerMoveCapture={() => { if (activeRef.current) setCurrent(undefined); }}
    onPointerLeave={() => setCurrent(undefined)}>
    {menu.items.map((item, index) => <button key={itemKey(item)} id={`${id}-${index}`} role="menuitem" type="button" tabIndex={-1}
      title={item.title} disabled={item.disabled} data-danger={item.danger || undefined}
      className={active === item ? 'is-current' : undefined} onClick={() => run(item)}>
      <item.icon size={13} aria-hidden="true" />
      <span>{item.label}</span>
    </button>)}
  </div>;
}

interface MenuActions {
  show: (menu: ContextMenuState, owner?: HTMLElement) => void;
  hideOwned: (owner: HTMLElement) => void;
  open: (event: MenuEvent, name: string, items: MenuItem[], extra?: ReadItems) => void;
}

const MenuContext = createContext<MenuActions>({show: () => {}, hideOwned: () => {}, open: () => {}});
export function useContextMenu() { return useContext(MenuContext); }

export function isContextMenuKey(event: ReactKeyboardEvent<HTMLElement> | KeyboardEvent) {
  const native = 'nativeEvent' in event ? event.nativeEvent : event;
  return !native.isComposing && (native.key === 'ContextMenu' || (native.shiftKey && native.key === 'F10'));
}

export function ContextMenuProvider({children}: {children: ReactNode}) {
  const [current, setCurrent] = useState<PopupRequest>();
  const latest = useRef<PopupRequest | undefined>(undefined);
  const publish = useCallback((request?: PopupRequest) => {
    if (latest.current?.reading !== request?.reading) latest.current?.reading?.abort();
    latest.current = request;
    setCurrent(request);
  }, []);
  const dismiss = useCallback(() => publish(), [publish]);
  const show = useCallback((menu: ContextMenuState, owner?: HTMLElement) => {
    if (available(owner) && menu.items.length) publish({menu, owner});
  }, [publish]);
  const hideOwned = useCallback((owner: HTMLElement) => {
    if (latest.current?.owner === owner) dismiss();
  }, [dismiss]);
  const open = useCallback((event: MenuEvent, name: string, items: MenuItem[], extra?: ReadItems) => {
    const owner = event.currentTarget;
    if (event.defaultPrevented || !available(owner)) return;
    event.preventDefault();
    event.stopPropagation();
    const anchor = event.type === 'click' ? owner : undefined;
    if (anchor && latest.current?.anchor === anchor) { dismiss(); return; }
    const bounds = owner.getBoundingClientRect();
    const atPointer = event.type === 'contextmenu' && 'clientX' in event && Boolean(event.clientX || event.clientY);
    const keyboard = event.type === 'keydown' || (event.type === 'click' && 'detail' in event && event.detail === 0);
    const menu = {name, items, keyboard, x: atPointer ? event.clientX : bounds.left, y: atPointer ? event.clientY : bounds.bottom};
    const reading = extra ? new AbortController() : undefined;
    const request = {menu, owner, anchor, reading};
    publish(request);
    if (extra && reading) void extra(reading.signal).then(next => {
      if (reading.signal.aborted || latest.current !== request || !available(owner)) return;
      if (next.length) publish({...request, menu: {...menu, items: next}});
      else dismiss();
    }).catch(() => {});
  }, [publish, dismiss]);
  useLayoutEffect(() => () => { latest.current?.reading?.abort(); }, []);
  const row = current?.owner?.closest<HTMLElement>('.session-row');
  useLayoutEffect(() => {
    if (!row) return;
    row.dataset.contextMenuOpen = 'true';
    return () => { delete row.dataset.contextMenuOpen; };
  }, [row]);
  const actions = useMemo(() => ({show, hideOwned, open}), [show, hideOwned, open]);
  return <MenuContext.Provider value={actions}>
    {children}
    {current && createPortal(<MenuPopup key={`${current.menu.name}:${current.menu.x}:${current.menu.y}`} request={current} dismiss={dismiss} />, document.body)}
  </MenuContext.Provider>;
}
