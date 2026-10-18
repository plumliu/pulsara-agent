import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { UiPreferences } from './ui-preferences';

test('desktop UI preferences survive a new owner independently of renderer ports', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'pulsara-ui-prefs-'));
  try {
    const file = path.join(root, 'electron', 'ui-preferences.json');
    const first = new UiPreferences(file);
    const a = 'pulsara-workspace-open:session:rxtzyjzdh7q4sbk6xjv7qrwsrm';
    const b = 'pulsara-workspace-open:session:hk7ud7dvpyxqz4csc3z3hqksuq';
    first.write({key:a,value:'true'});
    first.write({key:b,value:'false'});
    first.write({key:'pulsara-theme',value:'dark'});
    first.write({key:'pulsara-sidebar-open',value:'false'});
    const widthKey = a.replace('-open:', '-width:');
    first.write({key:widthKey,value:'564.5'});
    const restored = new UiPreferences(file);
    assert.equal(restored.read()[widthKey], '564.5');
    assert.equal(restored.read()['pulsara-sidebar-open'], 'false');
    assert.throws(() => restored.write({key:widthKey,value:'NaN'}));
    assert.throws(() => restored.write({key:widthKey,value:'0'}));
    restored.write({key:widthKey,value:null});
    restored.write({key:'pulsara-sidebar-open',value:null});
    const second = new UiPreferences(file);
    assert.deepEqual(second.read(), {[a]:'true',[b]:'false','pulsara-theme':'dark'});
    second.write({key:a,value:null});
    assert.deepEqual(new UiPreferences(file).read(), {[b]:'false','pulsara-theme':'dark'});
    assert.throws(() => second.write({key:'postgres', value:'anything'}));
    assert.throws(() => second.write({key:'__proto__', value:null}));
    assert.throws(() => second.write({key:b, value:1}));
    assert.deepEqual(new UiPreferences(file).read(), second.read());
  } finally { rmSync(root, {recursive:true,force:true}); }
});
