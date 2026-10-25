import assert from 'node:assert/strict';
import { test } from 'node:test';
import { toBackendPath, toWindowsPath } from './backend-paths';

test('same-distribution UNC aliases preserve Linux case and Unicode identity', () => {
  for (const value of ['/home/user/Case','/home/user/case','/home/user/café 中文','/home/user/cafe\u0301 中文']) {
    assert.equal(toBackendPath(toWindowsPath(value,'Ubuntu 本机'),'Ubuntu 本机'),value);
    assert.equal(toBackendPath(toWindowsPath(value,'Ubuntu 本机').replace('wsl.localhost','wsl$'),'Ubuntu 本机'),value);
  }
  assert.equal(toBackendPath('/','Ubuntu'),'/');
});

test('domain mapping refuses Windows drives, other distributions and ambiguous components', () => {
  for (const value of ['C:\\work','C:work','\\\\server\\share\\work','\\\\?\\C:\\work','\\\\wsl.localhost\\Other\\home','/home//user','/home/./user','/home/../user','/home/user/','/home\\user','//home/user','/home/\0user']) {
    assert.throws(() => toBackendPath(value,'Ubuntu'));
  }
});

test('Linux-only names remain previewable but cannot become ambiguous host paths', () => {
  for (const name of ['a:b','file?','file*','file"','file<','file>','file|','CON','con.txt','CON .txt','PRN','AUX','NUL','COM1','COM¹.log','LPT9','CONIN$','CONOUT$','trailing.','trailing ']) {
    const linux = '/home/user/'+name;
    assert.equal(toBackendPath(linux,'Ubuntu'),linux);
    assert.throws(() => toWindowsPath(linux,'Ubuntu'));
    assert.throws(() => toBackendPath('\\\\wsl.localhost\\Ubuntu\\home\\user\\'+name,'Ubuntu'));
  }
});
