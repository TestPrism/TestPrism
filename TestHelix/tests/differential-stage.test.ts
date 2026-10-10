import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {hypothesisDifference} from '../src/differential-stage.js';
import {comparisonBaseline, discoverSourceChanges} from '../src/source-state.js';
import {LocalFactory} from '../src/runtime.js';
import type {Candidate, FileArtifact} from '../src/types.js';
import {fixture} from './helpers.js';

const file = (name: string, content: string | Buffer, deleted = false): FileArtifact =>
  ({path: name, data: Buffer.from(content).toString('base64'), mode: 0o644, ...(deleted ? {deleted} : {})});
const candidate = (source: FileArtifact[]): Candidate => ({id: 'comparison', lineage: 'comparison', round: 0,
  strategy: '', source, tests: [], sourceHash: '', testsHash: '', requirements: [], rationale: '', sessionId: 'comparison'});

test('hypothesis comparison handles additions, deletions and unchanged baseline files without mistaking read errors for absence', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), workspace = await factory.fork('candidate');
  await discoverSourceChanges(workspace, baseline);
  const added = await hypothesisDifference(candidate([file('new.cjs', 'hello\n')]), candidate([]), baseline);
  assert.match(added, /-hello/); assert.doesNotMatch(added, /unavailable/);
  const empty = await hypothesisDifference(candidate([file('__init__.py', '')]), candidate([]), baseline);
  assert.match(empty, /primary present, mode 644; peer absent/);
  const mode = await hypothesisDifference(candidate([{...file('script.sh', ''), mode: 0o755}]), candidate([file('script.sh', '')]), baseline);
  assert.match(mode, /primary present, mode 755; peer present, mode 644/);
  const removed = await hypothesisDifference(candidate([file('prime.cjs', '', true)]), candidate([]), baseline);
  assert.match(removed, /\+exports.isPrime/);
  await baseline.write('prime.cjs', Buffer.from('unexpected mutation'));
  assert.match(await hypothesisDifference(candidate([file('prime.cjs', 'change')]), candidate([]), baseline), /unavailable/);
  await assert.rejects(comparisonBaseline(baseline, '../outside', 160000), /path/i);
});

test('binary, oversized and many-file differences are bounded and explicitly incomplete', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline');
  const binary = await hypothesisDifference(candidate([file('a', Buffer.from([0, 1]))]), candidate([file('a', 'text')]), baseline);
  assert.match(binary, /omitted.*binary/);
  const invalid = await hypothesisDifference(candidate([file('a', Buffer.from([255]))]), candidate([file('a', 'text')]), baseline);
  assert.match(invalid, /non-UTF-8/);
  const large = await hypothesisDifference(candidate([file('a', 'x'.repeat(170000))]), candidate([file('a', 'y'.repeat(170000))]), baseline);
  assert.match(large, /omitted.*size limit/);
  const many = await hypothesisDifference(candidate(Array.from({length: 9}, (_, index) => file(`${index}`, 'x'.repeat(20000)))),
    candidate(Array.from({length: 9}, (_, index) => file(`${index}`, 'y'.repeat(20000)))), baseline);
  assert.ok(many.length <= 8400); assert.match(many, /truncated|omitted/);
});
