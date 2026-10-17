export interface SkillReference {
  name: string;
  raw: string;
}

// Match the /name contract in capability/resolver.py. A reference is
// display syntax, not proof that a skill is installed, enabled, or already read.
// Exclude XML closing tags, path/URL segments and extensions; files use @.
export function splitSkillReferences(text: string): Array<string | SkillReference> {
  const pattern = /(?<![\p{L}\p{N}_\/@.$:<\\-])\/([a-z0-9]+(?:-[a-z0-9]+)*)(?![\p{L}\p{N}_.\/\\-])/gu;
  const parts: Array<string | SkillReference> = [];
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) parts.push(text.slice(offset, match.index));
    parts.push({ name: match[1], raw: match[0] });
    offset = match.index + match[0].length;
  }
  if (offset < text.length) parts.push(text.slice(offset));
  return parts;
}

export function skillReference(raw: string): SkillReference | undefined {
  const parts = splitSkillReferences(raw);
  return parts.length === 1 && typeof parts[0] !== 'string' ? parts[0] : undefined;
}
