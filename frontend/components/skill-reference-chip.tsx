import { BookOpenText } from 'lucide-react';
import type { SkillReference } from '../lib/skill-reference';

export function SkillReferenceChip({ reference }: { reference: SkillReference }) {
  return <span className="skill-reference-chip" data-skill-reference={reference.raw} aria-label={`技能：${reference.name}`}>
    <BookOpenText size={14} aria-hidden="true" />
    <span className="skill-reference__name">{reference.name}</span>
  </span>;
}
