import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fixture} from './helpers.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {Toolbox} from '../src/tools.js';

test('public absolute paths share relative read/edit state and preserve containment', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const store = new Store(f.out); await store.init();
  const sandbox = new LocalSandbox(f.workspace);
  const tools = new Toolbox(sandbox, store, 6000, 3000);
  const signal = new AbortController().signal;
  const absolute = path.join(f.workspace, 'prime.cjs');
  const before = await tools.execute('read_file', {path: absolute}, signal);
  assert.equal(before.path, 'prime.cjs');
  assert.deepEqual(before, await tools.execute('read_file', {path: 'prime.cjs'}, signal));
  const original = await fs.readFile(absolute, 'utf8');
  await tools.execute('edit_file', {path: absolute, old: original, replacement: original + '\n// observed edit\n', expectedHash: before.hash}, signal);
  const after = await tools.execute('read_file', {path: 'prime.cjs'}, signal);
  assert.notEqual(before.hash, after.hash);
  await assert.rejects(tools.execute('edit_file', {path: absolute, old: original,
    replacement: '', expectedHash: before.hash}, signal), /changed|hash|stale/i);
  const listing = await tools.execute('list_files', {directory: f.workspace}, signal);
  assert.ok((listing.files as string[]).includes('prime.cjs'));
  for (const bad of [f.workspace+'-sibling/prime.cjs', f.workspace+'/../project/prime.cjs',
    f.workspace+'/.git/config', '/etc/passwd', f.workspace+'\\prime.cjs']) {
    await assert.rejects(tools.execute('read_file', {path: bad}, signal));
    await assert.rejects(tools.execute('write_file', {path: bad, content: 'bad'}, signal));
  }
  await fs.symlink(path.dirname(f.workspace),path.join(f.workspace,'outside'));
  await assert.rejects(tools.execute('write_file', {path: f.workspace+'/outside/escape',content:'bad'}, signal), /symlink|escapes/);
});

test('absolute suite paths retain all-or-nothing destination validation', async t => {
  const f=await fixture();t.after(f.cleanup);const store=new Store(f.out);await store.init();
  const tools=new Toolbox(new LocalSandbox(f.workspace),store,6000,3000);
  const signal=new AbortController().signal;
  await assert.rejects(tools.execute('write_test_suite',{files:[
    {path:f.workspace+'/agent-tests/run-tests.sh',content:'exit 0\n'},
    {path:f.workspace+'/prime.cjs',content:'bad'}]},signal),/agent-tests/);
  await assert.rejects(fs.stat(path.join(f.workspace,'agent-tests/run-tests.sh')));
  const result=await tools.execute('write_test_suite',{files:[
    {path:f.workspace+'/agent-tests/run-tests.sh',content:'#!/bin/bash\nset -eu\nnode agent-tests/control.cjs\n'},
    {path:f.workspace+'/agent-tests/control.cjs',content:'require("node:assert/strict").equal(2+2,4);\n'}]},signal);
  assert.equal(result.executed,true);
});
