import type { LucideIcon } from 'lucide-react';

export type InteractionTone = 'blue' | 'violet' | 'green' | 'amber' | 'red';

export function InteractionCardHeader({icon: Icon, title, tone = 'blue', subtitle, scope}: {
  icon: LucideIcon;
  title: string;
  tone?: InteractionTone;
  subtitle?: string;
  scope?: string;
}) {
  return <header className="interaction-card__header" data-tone={tone}>
    <span className="interaction-card__icon" aria-hidden="true"><Icon size={21} strokeWidth={1.65} /></span>
    <div className="interaction-card__heading"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
    {scope && <span className="interaction-card__scope">{scope}</span>}
  </header>;
}
