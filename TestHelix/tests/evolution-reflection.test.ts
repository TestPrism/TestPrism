import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {promises as fs} from 'node:fs';
import {config, completion, fixture} from './helpers.js';
import {atomicJSON, readJSON} from '../src/util.js';
import {genesis} from '../src/genome.js';
import {reflectedLesson} from '../src/evolution-reflection.js';
import {reflectCalibration} from '../scripts/reflect-calibration.js';

const update = {operation: 'upsert', id: 'prepare-environment', triggers: ['framework', 'import'],
  component: 'preparation', when: 'Framework initialization is required', action: 'Initialize the public test environment before collecting cases',
  verify: 'Collection succeeds and reports nonzero cases', rationale: 'Observed framework preparation failures'};

test('shared reflection preserves unrelated lessons and rejects scope changes and task literals', () => {
  const parent = genesis(config());
  const first = reflectedLesson(parent, update, ['evidence'], []);
  assert.throws(() => reflectedLesson(first, {...update, component: 'review'}, ['evidence'], [], {id: 'review', scope: 'review'}), /unrelated/);
  assert.throws(() => reflectedLesson(parent, {...update, action: 'Use forbidden-task-name'}, ['evidence'], ['forbidden-task-name']), /task identifier/);
  const second = reflectedLesson(first, {...update, id: 'check-collection', component: 'review', triggers: ['collection'],
    when: 'Reviewing a runner', action: 'Inspect collected case counts', verify: 'At least one case was collected'}, ['evidence'], []);
  assert.deepEqual(second.genes.lessons.find(item => item.id === update.id), first.genes.lessons[0]);
});

async function setup(root: string) {
  const base = config(); base.model.provider = 'openai-responses'; base.scheduler.apiConcurrency = 3; base.scheduler.enabled = true;
  base.scheduler.planningConcurrency = 1;
  await atomicJSON(path.join(root, 'config.json'), base);
  await atomicJSON(path.join(root, 'official.json'), {results: [{task_id: 'PRIVATE-TASK-NAME', trial_ended_at: '2026-09-12T00:00:00Z', failure_mode: 'PRIVATE-ANSWER', patch_results: []}]});
  await fs.mkdir(path.join(root, 'testhelix-js'));
  await fs.writeFile(path.join(root, 'testhelix-js/events.jsonl'), JSON.stringify({type: 'model.error', data: 'PRIVATE-CODE context overflow'}) + '\n');
  await atomicJSON(path.join(root, 'testhelix-js/result.json'), {taskId: 'PRIVATE-TASK-NAME', reason: 'PRIVATE-ASSERTION ModuleNotFoundError', reviews: {}, matrix: []});
  const manifest = path.join(root, 'manifest.json');
  await atomicJSON(manifest, {config: 'config.json', output: 'reflection', calibration: [{officialResults: 'official.json', agentDirectory: 'testhelix-js'}]});
  return manifest;
}

test('calibration prepare is offline, strips literals, and resume reuses sealed reply and usage', async t => {
  const f = await fixture(); t.after(f.cleanup); const manifest = await setup(f.root); let calls = 0;
  const model = {complete: async (args: any) => {calls++; const text = JSON.stringify(args);
    assert.doesNotMatch(text, /PRIVATE-/); assert.match(text, /context-overflow/); assert.match(text, /framework-preparation/);
    return completion([['submit_lesson', update]]);}};
  assert.equal((await reflectCalibration(manifest, false, model)).status, 'prepared'); assert.equal(calls, 0);
  assert.equal((await reflectCalibration(manifest, true, model)).status, 'unpromoted');
  assert.equal((await reflectCalibration(manifest, true, model)).status, 'unpromoted'); assert.equal(calls, 1);
  assert.equal((await readJSON<any>(path.join(f.root, 'reflection/usage.json'))).calls, 1);
  const candidate = await readJSON<any>(path.join(f.root, 'reflection/candidate.json')); assert.equal(candidate.promoted, false);
});

test('calibration rejects mismatched identities and unfinished official trials before dispatch', async t => {
  const f = await fixture(); t.after(f.cleanup); const manifest = await setup(f.root); let calls = 0;
  const model = {complete: async () => {calls++; return completion([['submit_lesson', update]]);}};
  const file = path.join(f.root, 'official.json'), original = await readJSON<any>(file);
  for (const change of [{task_id: 'wrong-task'}, {task_key: 'wrong-task'}, {trial_ended_at: null},
    {trial_ended_at: ''}, {trial_ended_at: 'unfinished'}, {trial_ended_at: undefined}]) {
    await atomicJSON(file, {results: [{...original.results[0], ...change}]});
    await assert.rejects(reflectCalibration(manifest, true, model));
  }
  assert.equal(calls, 0);
  await assert.rejects(fs.access(path.join(f.root, 'reflection/request.json')));
});

test('candidate write failure propagates without another paid attempt and can resume from reply', async t => {
  const f = await fixture(); t.after(f.cleanup); const manifest = await setup(f.root); let calls = 0;
  const model = {complete: async () => {calls++; return completion([['submit_lesson', update]]);}};
  await reflectCalibration(manifest, false, model);
  const target = path.join(f.root, 'reflection/candidate.json');
  await fs.mkdir(target); // Force atomic rename to fail after the reply is sealed.
  await assert.rejects(reflectCalibration(manifest, true, model));
  assert.equal(calls, 1);
  await assert.rejects(fs.access(path.join(f.root, 'reflection/attempt-1.json')));
  await fs.rmdir(target);
  assert.equal((await reflectCalibration(manifest, true, model)).status, 'unpromoted');
  assert.equal(calls, 1);
});

test('uncertain paid dispatch is not retried on resume and rejected proposals use at most two calls', async t => {
  const f = await fixture(); t.after(f.cleanup); const manifest = await setup(f.root); let calls = 0;
  const failing = {complete: async () => {calls++; throw new Error('response lost');}};
  await assert.rejects(reflectCalibration(manifest, true, failing), /response lost/);
  await assert.rejects(reflectCalibration(manifest, true, failing), /uncertain completion/); assert.equal(calls, 1);
  const g = await fixture(); t.after(g.cleanup); const second = await setup(g.root); let rejectedCalls = 0;
  const invalid = {complete: async () => {rejectedCalls++; return completion([]);}};
  await assert.rejects(reflectCalibration(second, true, invalid));
  await assert.rejects(reflectCalibration(second, true, invalid)); assert.equal(rejectedCalls, 2);
});
