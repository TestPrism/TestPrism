import test from 'node:test';
import assert from 'node:assert/strict';
import {dispatchTools} from '../src/tool-dispatch.js';
import {workspaceTools, readTools} from '../src/tools.js';
import {completion} from './helpers.js';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => {resolve = done;});
  return {promise, resolve};
}

test('dispatch failure stops queued reads and waits for active work before returning the original error', async () => {
  const failure = deferred(), sibling = deferred(), started: string[] = [];
  const error = new Error('artifact store unavailable');
  const reply = completion(Array.from({length: 5}, (_, i) => ['read_file', {path: String(i)}]));
  let settled = false;
  const run = dispatchTools(reply, workspaceTools, readTools, async call => {
    started.push(call.id);
    if (call === reply.calls[0]) {await failure.promise; throw error;}
    await sibling.promise; return call.id;
  }, new AbortController().signal, 2);
  const done = run.then(() => {settled = true;}, () => {settled = true;});
  failure.resolve(); await new Promise<void>(resolve => setImmediate(resolve));
  try {assert.equal(settled, false); assert.deepEqual(started, reply.calls.slice(0, 2).map(call => call.id));}
  finally {sibling.resolve(); await done;}
  await assert.rejects(run, value => value === error);
});

test('cancelling a batch reaches active tools, drains them and does not start queued tools', async () => {
  const controller = new AbortController(), entered = deferred(), drain = deferred();
  const calls = completion(Array.from({length: 5}, () => ['read_file', {path: 'x'}]));
  let started = 0, cancelled = 0, finished = false;
  const run = dispatchTools(calls, workspaceTools, readTools, async (_call, signal) => {
    if (++started === 2) entered.resolve();
    await new Promise<void>(resolve => signal.addEventListener('abort', () => {cancelled++; resolve();}, {once: true}));
    await drain.promise;
    signal.throwIfAborted();
  }, controller.signal, 2);
  const done = run.then(() => {finished = true;}, () => {finished = true;});
  await entered.promise; controller.abort(new Error('batch cancelled'));
  await new Promise<void>(resolve => setImmediate(resolve));
  try {assert.equal(cancelled, 2); assert.equal(finished, false); assert.equal(started, 2);}
  finally {drain.resolve(); await done;}
  await assert.rejects(run, /batch cancelled/); assert.equal(started, 2);
});

test('mixed batches preserve order and cancellation prevents trailing mutations', async () => {
  const controller = new AbortController(), started: string[] = [];
  const reply = completion([['read_file', {}], ['write_file', {}], ['exec', {}], ['submit_candidate', {}]]);
  await assert.rejects(dispatchTools(reply, workspaceTools, readTools, async call => {
    started.push(call.name);
    if (call.name === 'write_file') controller.abort(new Error('stop before exec'));
    return call.id;
  }, controller.signal), /stop before exec/);
  assert.deepEqual(started, ['read_file', 'write_file']);
});

test('aborted, oversized, duplicate and invalid-concurrency batches perform no work', async () => {
  let executed = 0;
  const work = async () => {executed++;};
  const controller = new AbortController(); controller.abort();
  await assert.rejects(dispatchTools(completion([['write_file', {}]]), workspaceTools, readTools, work, controller.signal));
  await assert.rejects(dispatchTools(completion(Array.from({length: 33}, () => ['write_file', {}])), workspaceTools, readTools, work,
    new AbortController().signal), /at most 32/);
  const duplicate = completion([['exec', {}], ['exec', {}]]); duplicate.calls[1]!.id = duplicate.calls[0]!.id;
  await assert.rejects(dispatchTools(duplicate, workspaceTools, readTools, work, new AbortController().signal), /Duplicate/);
  for (const concurrency of [0, -1, NaN, Infinity, 1.5, 9])
    await assert.rejects(dispatchTools(completion([]), workspaceTools, readTools, work, new AbortController().signal, concurrency), /concurrency/);
  assert.equal(executed, 0);
});
