import {expect, it} from 'vitest';
import {applyHookRule, hookRuleDraft, editHookField, moveHookGroup, readHookForm} from '../../frontend/lib/hook-config-form';

it('edits only the selected field and preserves ignored entries, metadata and mixed newline bytes', () => {
  const raw = '{\r\n"custom":12345678901234567890,\n"hooks":{"SessionStart":[{"extra":"keep", "hooks":[{"type":"command","command":"old","statusMessage":"retain"},{"type":"future","value":true}]}],"FutureEvent":"keep"}\r}';
  const form = readHookForm(raw);
  expect(form.groups).toHaveLength(1); expect(form.unrepresented).toBe(true);
  const next = editHookField(raw, ['hooks', 'SessionStart', 0, 'hooks', 0, 'command'], 'new');
  expect(next).toBe(raw.replace('"old"', '"new"'));
});

it('moves the entire group without changing sibling handlers or large integer literals', () => {
  const groupRaw = '{ "matcher":"Read", "unfamiliar":12345678901234567890, "hooks":[{"type":"command","command":"a"},{"type":"command","command":"b"}]}';
  const raw = `{"description":"keep","hooks":{"SessionStart":[${groupRaw}],"PreToolUse":[{"hooks":[{"type":"command","command":"existing"}]}]}}`;
  const next = moveHookGroup(raw, readHookForm(raw).groups[0], 'PreToolUse');
  expect(next).toContain(groupRaw);
  const data = JSON.parse(next);
  expect(data.description).toBe('keep');
  expect(data.hooks.SessionStart).toEqual([]);
  expect(data.hooks.PreToolUse.map((group: {hooks:Array<{command:string}>}) => group.hooks.map(hook => hook.command))).toEqual([['existing'], ['a', 'b']]);
});

it('cannot load ambiguous or malformed JSON into the form', () => {
  for (const text of ['{"hooks":{},"hooks":{}}', '{"hooks":{}} // comment', '{"hooks":{},}', '{"hooks":[],"value":1e999}']) {
    expect(readHookForm(text).error).toBeTruthy();
  }
});

it('edits a selected rule fragment while preserving surrounding raw bytes and unknown values', () => {
  const raw = '{\r\n"custom":12345678901234567890,\n"hooks":{"SessionStart":[{"extra":12345678901234567890, "hooks":[{"type":"command","command":"old"},{"type":"future"}]}]}\r}';
  const group = readHookForm(raw).groups[0];
  const draft = editHookField(hookRuleDraft(group), ['hooks', 'SessionStart', 0, 'hooks', 0, 'command'], 'new');
  expect(applyHookRule(raw, draft, group)).toBe(raw.replace('"old"', '"new"'));
});

it('moves an edited rule to another event without dropping sibling or ignored declarations', () => {
  const raw = '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"first"}]},{"extra":12345678901234567890,"hooks":[{"type":"command","command":"selected"}]}],"PreToolUse":[{"hooks":[{"type":"command","command":"existing"}]}],"FutureEvent":true}}';
  const original = readHookForm(raw).groups[1];
  const draft = hookRuleDraft(original);
  const moved = moveHookGroup(draft, readHookForm(draft).groups[0], 'PreToolUse');
  const next = applyHookRule(raw, moved, original);
  expect(next).toContain(original.raw);
  const hooks = JSON.parse(next).hooks;
  expect(hooks.SessionStart).toHaveLength(1);
  expect(hooks.SessionStart[0].hooks[0].command).toBe('first');
  expect(hooks.PreToolUse.map((group: {hooks:Array<{command:string}>}) => group.hooks[0].command)).toEqual(['existing', 'selected']);
  expect(hooks.FutureEvent).toBe(true);
});

it('does not append a rule over malformed original JSON', () => {
  expect(() => applyHookRule('{broken', hookRuleDraft())).toThrow('JSON');
});

it('removes the final command in a multi-command rule without leaving a broken JSON fragment', () => {
  const raw = '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"keep"},{"type":"command","command":"remove"}]}]}}';
  const next = editHookField(raw, ['hooks','SessionStart',0,'hooks',1], undefined);
  expect(JSON.parse(next).hooks.SessionStart[0].hooks).toEqual([{type:'command', command:'keep'}]);
});
