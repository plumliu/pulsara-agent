import {applyEdits, findNodeAtLocation, getNodeValue, modify, parseTree} from 'jsonc-parser';
import type {JSONPath, Node, ParseError} from 'jsonc-parser';

export interface HookFormGroup { event: string; index: number; value: Record<string, unknown>; handlers: unknown[]; raw: string }
export interface HookFormRead { groups: HookFormGroup[]; unrepresented: boolean; error?: string }
export const blankHookGroup = () => ({hooks: [{type: 'command', command: ''}]});
export const initialHookFormText = () => JSON.stringify({hooks: {SessionStart: [blankHookGroup()]}}, null, 2);

// jsonc-parser owns syntax and localized edits. Pulsara only binds form fields;
// execution validation continues to belong to the native Hook parser on save.
export function readHookForm(text: string): HookFormRead {
  try {
    const errors: ParseError[] = [];
    const root = parseTree(text, errors, {disallowComments: true, allowTrailingComma: false});
    if (!root || errors.length || root.type !== 'object') throw new Error('invalid object');
    const verify = (node: Node) => {
      if (node.type === 'number' && !Number.isFinite(node.value)) throw new Error('non-finite number');
      if (node.type === 'object') {
        const names = (node.children ?? []).map(property => property.children![0].value);
        if (new Set(names).size !== names.length) throw new Error('duplicate property');
      }
      node.children?.forEach(verify);
    };
    verify(root);
    const hooks = findNodeAtLocation(root, ['hooks']);
    if (hooks && hooks.type !== 'object') throw new Error('invalid hooks object');
    const groups: HookFormGroup[] = [];
    let unrepresented = false;
    for (const property of hooks?.children ?? []) {
      const [key, array] = property.children!;
      if (array.type !== 'array') {unrepresented = true; continue;}
      array.children?.forEach((group, index) => {
        const handlers = findNodeAtLocation(group, ['hooks']);
        if (group.type !== 'object' || handlers?.type !== 'array') {unrepresented = true; return;}
        groups.push({event: String(key.value), index, value: getNodeValue(group), handlers: getNodeValue(handlers), raw: text.slice(group.offset, group.offset + group.length)});
      });
    }
    return {groups, unrepresented};
  } catch {
    return {groups: [], unrepresented: true, error: '当前配置无法安全载入表单，请通过 JSON 入口修正。'};
  }
}

export function editHookField(text: string, path: JSONPath, value: unknown): string {
  const edits = modify(text, path, value, {});
  // jsonc-parser 3.3.1 leaves the last character behind when deleting the
  // final element of a multi-item array (parentEnd - 2). Correct only that
  // dependency gap; syntax, node locations and all other edits remain SDK-owned.
  if (value === undefined && typeof path.at(-1) === 'number') {
    const root = parseTree(text);
    const parent = root && findNodeAtLocation(root, path.slice(0, -1));
    if (parent?.type === 'array' && parent.children!.length > 1 && path.at(-1) === parent.children!.length - 1) edits[0].length++;
  }
  return applyEdits(text, edits);
}

export function moveHookGroup(text: string, group: HookFormGroup, event: string): string {
  if (event === group.event) return text;
  const removed = editHookField(text, ['hooks', group.event, group.index], undefined);
  const appended = editHookField(removed, ['hooks', event, -1], null);
  const root = parseTree(appended)!;
  const array = findNodeAtLocation(root, ['hooks', event])!;
  const inserted = array.children!.at(-1)!;
  // Move the original JSON fragment, including literals the JS number type
  // cannot represent exactly; the dependency owns the structural insertion.
  return applyEdits(appended, [{offset: inserted.offset, length: inserted.length, content: group.raw}]);
}

export function hookRuleDraft(group?: HookFormGroup): string {
  return group ? `{"hooks":{${JSON.stringify(group.event)}:[${group.raw}]}}` : initialHookFormText();
}

// The selected group belongs to the exact file observation sent as the CAS
// baseline. Never relocate an old array index against a freshly read file.
export function applyHookRule(text: string, draft: string, original?: HookFormGroup): string {
  if (readHookForm(text).error) throw new Error('配置无法载入，请通过 JSON 入口修正。');
  const form = readHookForm(draft);
  if (form.error || form.unrepresented || form.groups.length !== 1) throw new Error('规则草稿无法保存，请重新打开配置。');
  const group = form.groups[0];
  const sameEvent = original?.event === group.event;
  const removed = original && !sameEvent ? editHookField(text, ['hooks', original.event, original.index], undefined) : text;
  const path: JSONPath = ['hooks', group.event, sameEvent ? original!.index : -1];
  const inserted = editHookField(removed, path, null);
  const root = parseTree(inserted)!;
  const array = findNodeAtLocation(root, ['hooks', group.event])!;
  const node = sameEvent ? array.children![original!.index] : array.children!.at(-1)!;
  return applyEdits(inserted, [{offset: node.offset, length: node.length, content: group.raw}]);
}
