import { describe, expect, it } from 'vitest';
import { decodeCanonicalPromptContent, promptContentCanSubmit } from '../../frontend/lib/prompt-content';

describe('saved canonical prompt decoding is separate from human admission', () => {
  it.each([{parts:[]}, {parts:[{type:'text' as const,text:''}]}])('reads valid empty saved content %j', content => {
    expect(decodeCanonicalPromptContent(JSON.stringify(content), {kind:'entry',entryId:'entry:test'})).toEqual(content);
    expect(promptContentCanSubmit(content)).toBe(false);
  });
  it('keeps whitespace-only human submissions unavailable', () => {
    expect(promptContentCanSubmit({parts:[{type:'text',text:' \r\n\t'}]})).toBe(false);
    expect(promptContentCanSubmit({parts:[{type:'text',text:'hello'}]})).toBe(true);
  });
  it.each([{parts:null},{parts:[{type:'text',text:3}]},{parts:[{type:'unknown'}]}])('rejects malformed saved content %j', value => {
    expect(() => decodeCanonicalPromptContent(JSON.stringify(value), {kind:'entry',entryId:'entry:test'})).toThrow();
  });
});
