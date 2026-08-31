import type { SessionSummary } from '../lib/pulsara-types';

export type SessionPresence = 'current' | 'loaded' | 'resumable';

export const sessionPresenceLabels: Record<SessionPresence, string> = {
  current: '当前会话',
  loaded: '已载入',
  resumable: '可恢复',
};

export function getSessionPresence(
  session: SessionSummary,
  activeSessionId: string,
): SessionPresence {
  if (session.id === activeSessionId) return 'current';
  return session.live ? 'loaded' : 'resumable';
}

export function SessionPresenceGlyph({ presence }: { presence: SessionPresence }) {
  return <span className={`session-presence session-presence--${presence}`} aria-hidden="true" />;
}
