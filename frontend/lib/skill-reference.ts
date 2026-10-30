export interface SkillReference {
  name: string;
  raw: string;
}

// Match the /name contract in capability/resolver.py. A reference is
// display syntax, not proof that a skill is installed, enabled, or already read.
// Exclude XML closing tags, path/URL segments and extensions; files use @.
export function splitSkillReferences(text: string): Array<string | SkillReference> {
  const pattern = /(?<![\p{L}\p{N}_/@.$:<\\-])\/([a-z0-9]+(?:-[a-z0-9]+)*)(?![\p{L}\p{N}_/\\-]|\.[\p{L}\p{N}_])/gu;
  const excluded = explicitReferenceExcludedRanges(text);
  const parts: Array<string | SkillReference> = [];
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (excluded.some(([start, end]) => match.index >= start && match.index < end)) continue;
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

/** User intent excludes quoted/code content and link destinations. Keep offsets in the original text. */
export function explicitReferenceExcludedRanges(text: string, includeFileReferences = true): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  let offset = 0; let fence: { marker: string; length: number; start: number } | undefined;
  for (const line of text.split(/(?<=\n)|(?<=\r)(?!\n)/u)) {
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence.marker && marker[1].length >= fence.length && /^ {0,3}(?:`+|~+)\s*$/.test(line)) {
        ranges.push([fence.start, offset + line.length]); fence = undefined;
      }
    } else if (marker) fence = { marker: marker[1][0], length: marker[1].length, start: offset };
    else if (/^\s*>/.test(line)) ranges.push([offset, offset + line.length]);
    offset += line.length;
  }
  if (fence) ranges.push([fence.start, text.length]);
  const within = (index: number) => ranges.some(([start, end]) => index >= start && index < end);
  const ticks = /`+/g; let match: RegExpExecArray | null;
  while ((match = ticks.exec(text))) {
    if (within(match.index)) continue;
    const marker = match[0]; let end = ticks.exec(text);
    while (end && end[0] !== marker) end = ticks.exec(text);
    if (end) ranges.push([match.index, end.index + marker.length]);
    else ranges.push([match.index, text.length]);
  }
  for (const match of text.matchAll(/(?:https?:\/\/|www\.)[^\s<>]+|\]\([^\n]*?\)/gi)) ranges.push([match.index, match.index + match[0].length]);
  if (includeFileReferences) for (const match of text.matchAll(/【本地(文件夹|文件)(?:（只读副本，(\d+) bytes）)?：("(?:\\.|[^"\\])*?")】/g)) {
    if (match[2] !== undefined && (match[1] === '文件夹' || !Number.isSafeInteger(Number(match[2])))) continue;
    try {
      const path: unknown = JSON.parse(match[3]);
      // eslint-disable-next-line no-control-regex -- Same legal file-reference boundary used by the existing reference owner.
      if (typeof path === 'string' && /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(path) && !/[\u0000-\u001f\u007f]/.test(path) && path.split(/[\\/]/).filter(Boolean).length) ranges.push([match.index, match.index + match[0].length]);
    } catch { /* Malformed ordinary text does not establish a file reference. */ }
  }
  return ranges;
}

export function explicitReferenceAllowedAt(text: string, index: number): boolean {
  return !explicitReferenceExcludedRanges(text).some(([start, end]) => index >= start && index < end);
}
