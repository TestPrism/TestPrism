import test, {type TestContext} from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {contractLimitsSchema} from '../src/contract-schema.js';
import {normalizeToolInput} from '../src/protocol.js';
import {workspaceTools} from '../src/tools.js';
import {captureCandidate, captureWorkspaceCandidate} from '../src/artifacts.js';
import {LocalFactory} from '../src/runtime.js';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {Store} from '../src/store.js';
import {config, fixture, completion} from './helpers.js';
import { inferTaskContract, validateOutputContract, HOST_HARD_CONSTRAINTS, outputContractSchema } from '../src/task-contract.js';

test('public source-repair tasks are detected before a tests-only official run', () => {
  const inferred = inferTaskContract('Fix the bug and save the corrected program as fixed_rpn_eval.py. The input format is one expression per line.');
  assert.equal(inferred.mode, 'source-repair');
  assert.ok(inferred.requiredOutputs.includes('fixed_rpn_eval.py'));
  assert.match(inferred.inputConstraints[0] ?? '', /input format/i);
  const result = validateOutputContract(undefined, inferred, 'tests-only');
  assert.equal(result.compatible, false);
  assert.match(result.errors[0] ?? '', /source artifacts/);
});

test('test-generation contracts retain the runner and host hard constraints', () => {
  const inferred = inferTaskContract('Add regression tests under agent-tests/ and run agent-tests/run-tests.sh. Input must be a nonnegative integer.');
  assert.equal(inferred.mode, 'tests-only');
  const result = validateOutputContract({
    inputs: ['nonnegative integer'], outputs: ['agent-tests/test_regression.py', 'agent-tests/run-tests.sh'],
    entrypoint: 'agent-tests/run-tests.sh', hardConstraints: [...HOST_HARD_CONSTRAINTS], rationale: 'public contract',
  }, inferred, 'tests-only');
  assert.equal(result.compatible, true);
});

test('model declarations cannot move output outside the host export root', () => {
  const inferred = inferTaskContract('Write regression tests under agent-tests/ and use run-tests.sh.');
  const result = validateOutputContract({
    inputs: [], outputs: ['fixed_solution.py'], entrypoint: 'run-tests.sh', hardConstraints: [], rationale: '',
  }, inferred, 'tests-only');
  assert.equal(result.compatible, false);
  assert.ok(result.errors.some(error => /agent-tests|entrypoint/.test(error)));
  assert.ok(result.warnings.some(error => /host hard constraint/.test(error)));
});

test('structured tool arguments reject unknown kinds, unsupported keywords and field types with repairable paths', () => {
  const good = {requirements: ['public behavior'], rationale: 'fixture', contract: {version: 2, outputs: [
    {kind: 'schema', target: 'agent-tests/data.json', schema: {type: 'object', required: ['count'],
      properties: {count: {type: 'integer', minimum: 0}}, additionalProperties: false}},
  ]}};
  assert.doesNotThrow(() => normalizeToolInput(JSON.stringify(good), workspaceTools.submit_candidate!));
  const invalid = [
    {kind: 'command', command: 'echo bypass'},
    {kind: 'format', target: 'agent-tests/data.json', format: 'json', extra: true},
    {kind: 'schema', target: 'agent-tests/data.json', schema: {type: 'object', $ref: 'https://invalid.example/schema'}},
    {kind: 'schema', target: 'agent-tests/data.json', schema: {type: 'string', minimum: 0}},
    {kind: 'schema', target: 'agent-tests/data.json', schema: {type: 'integer', minimum: '0'}},
    {kind: 'cardinality', target: 'agent-tests/data.json', unit: 'items'},
    {kind: 'cardinality', target: 'agent-tests/data.json', unit: 'items', min: 3, max: 1},
  ];
  for (const clause of invalid) {
    const raw = {...good, contract: {outputs: [clause]}};
    assert.throws(() => normalizeToolInput(JSON.stringify(raw), workspaceTools.submit_candidate!), /contract.outputs.0/);
  }
  assert.throws(() => normalizeToolInput(JSON.stringify(good).slice(0, -1), workspaceTools.submit_candidate!), /Incomplete|invalid JSON/);
  assert.throws(() => workspaceTools.submit_candidate!.parse!({...good, contract: {version: 99}}));
});

test('contract JSON is bounded before recursive parsing and host limits cannot be raised by the model', () => {
  let schema: unknown = {type: 'string'};
  for (let i = 0; i < 30; i++) schema = {type: 'array', items: schema};
  assert.throws(() => outputContractSchema.parse({outputs: [{kind: 'schema', target: 'stdout', schema}]}), /depth limit/);
  const wide = Object.fromEntries(Array.from({length: 5000}, (_, i) => ['p' + i, {type: 'string'}]));
  assert.throws(() => outputContractSchema.parse({outputs: [{kind: 'schema', target: 'stdout', schema: {type: 'object', properties: wide}}]}), /node limit/);
  const cyclic: any = {type: 'array'}; cyclic.items = cyclic;
  assert.throws(() => outputContractSchema.parse({outputs: [{kind: 'schema', target: 'stdout', schema: cyclic}]}), /acyclic/);
  assert.throws(() => outputContractSchema.parse({inputs: new Array(10_000_000)}), /node limit/);
  assert.throws(() => outputContractSchema.parse({outputs: [{kind: 'schema', target: 'stdout',
    schema: {type: 'string', enum: ['😀'.repeat(2200)]}}]}), /byte limit/);
  const inferred = inferTaskContract('Public task');
  const declared = {inputs: [{kind: 'note', text: '😀'.repeat(400)}]};
  assert.equal(validateOutputContract(declared, inferred, 'internal', {maxBytes: 1024}).compatible, false);
  assert.equal(validateOutputContract(declared, inferred, 'internal', {maxBytes: 4096}).compatible, true);
  assert.equal(validateOutputContract({...declared, limits: {maxBytes: 1000000}}, inferred).compatible, false);
  assert.throws(() => contractLimitsSchema.parse({maxBytes: 1000000}));
});

test('host defaults are always present and declared paths cannot escape or reference hidden data', () => {
  // Even a caller-supplied inference object cannot remove the built-in hard constraints.
  const inferred = {...inferTaskContract('Public task'), hardConstraints: []};
  const defaults = validateOutputContract(undefined, inferred);
  assert.equal(defaults.origin, 'host-default');
  assert.deepEqual(defaults.effective.hardConstraints, HOST_HARD_CONSTRAINTS);
  assert.deepEqual(defaults.effective.outputs, [{kind: 'path', path: 'agent-tests/run-tests.sh', type: 'file'}]);
  for (const name of ['agent-tests/../outside', '/tmp/x', 'agent-tests\\x', 'agent-tests/solution.sh', 'agent-tests/.git/config']) {
    const result = validateOutputContract({outputs: [{kind: 'path', path: name}]}, inferred);
    assert.equal(result.compatible, false, name);
  }
  assert.equal(validateOutputContract({inputs: [{kind: 'schema', target: 'solution.sh', schema: {type: 'object'}}]}, inferred).compatible, false);
});

const identity = {id: 'contract', lineage: 'contract', round: 0, strategy: '', sessionId: 'contract'};
const declaration = {requirements: ['public behavior'], rationale: 'public fixture'};
async function contractFixture(t: TestContext) {
  const f = await fixture(); t.after(f.cleanup);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  await sandbox.write('agent-tests/run-tests.sh', Buffer.from('exit 1\n'), 0o755);
  return {f, baseline, sandbox};
}

test('both capture paths persist host defaults and enforce contracts against the exported bytes', async t => {
  const {sandbox, baseline} = await contractFixture(t);
  for (const capture of [captureCandidate, captureWorkspaceCandidate]) {
    const candidate = await capture(sandbox, baseline, declaration, identity, 64);
    assert.equal(candidate.contractCheck?.origin, 'host-default');
    assert.equal(candidate.contractCheck?.checks[0]?.status, 'verified');
    await assert.rejects(capture(sandbox, baseline, {...declaration, contract: {outputs: [
      {kind: 'path', path: 'agent-tests/missing.json'},
    ]}}, identity, 4000000), /Required exported file is missing/);
    await assert.rejects(capture(sandbox, baseline, {...declaration, contract: {outputs: [
      {kind: 'path', path: 'agent-tests/../outside'},
    ]}}, identity, 4000000), /path rejected/);
  }
});

test('file constraints inspect immutable baseline inputs and captured outputs while runtime semantics remain unverified', async t => {
  const {sandbox, baseline} = await contractFixture(t);
  await baseline.write('input.json', Buffer.from('{"count":3}'));
  await sandbox.write('input.json', Buffer.from('{"count":"wrong in repair"}'));
  await sandbox.write('agent-tests/results.json', Buffer.from('[1,2,3]'));
  const contract = {inputs: [{kind: 'schema', target: 'input.json', schema: {type: 'object', required: ['count'],
    properties: {count: {type: 'integer', minimum: 0}}, additionalProperties: false}}], outputs: [
    {kind: 'path', path: 'agent-tests/run-tests.sh', executable: true},
    {kind: 'format', target: 'agent-tests/results.json', format: 'json'},
    {kind: 'cardinality', target: 'agent-tests/results.json', unit: 'items', min: 3, max: 3},
    {kind: 'schema', target: 'stdout', schema: {type: 'object', required: ['status']}},
    {kind: 'note', text: 'Results must follow the original public task.'},
  ]};
  const candidate = await captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000);
  assert.deepEqual(candidate.contract?.hardConstraints, HOST_HARD_CONSTRAINTS);
  assert.equal(candidate.contractCheck?.checks.filter(check => check.status === 'verified').length, 4);
  assert.equal(candidate.contractCheck?.checks.filter(check => check.status === 'unverified').length, 2);
  assert.equal(candidate.contractCheck?.checks.some(check => check.status === 'failed'), false);
  await baseline.write('input.json', Buffer.from('{"count":"3"}'));
  await assert.rejects(captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000), /expected integer/);
});

test('schema checks reject missing fields and wrong types without leaking file contents or treating coercion as validation', async t => {
  const {sandbox, baseline} = await contractFixture(t);
  const contract = {outputs: [{kind: 'schema', target: 'agent-tests/value.json', schema: {
    type: 'object', required: ['status'], properties: {status: {type: 'string', enum: ['ok']}}, additionalProperties: false,
  }}]};
  for (const [content, reason] of [
    ['{}', /required field/], ['{"status":true}', /expected string/], ['{"status":"secret-not-in-diagnostics"}', /outside enum/],
    ['{"status":"ok","extra":1}', /additional field/], ['{"status":"ok"} trailing-secret', /complete UTF-8 JSON/],
  ] as const) {
    await sandbox.write('agent-tests/value.json', Buffer.from(content));
    await assert.rejects(captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000), error => {
      assert.match(String(error), reason); assert.doesNotMatch(String(error), /secret-not-in-diagnostics|trailing-secret/); return true;
    });
  }
});

test('format and cardinality checks cover JSONL, UTF-8, lines, bytes and arrays', async t => {
  const {sandbox, baseline} = await contractFixture(t);
  await sandbox.write('agent-tests/records', Buffer.from('{"x":1}\n{"x":2}\n'));
  const contract = {outputs: [
    {kind: 'format', target: 'agent-tests/records', format: 'jsonl'},
    {kind: 'cardinality', target: 'agent-tests/records', unit: 'lines', min: 2, max: 2},
    {kind: 'cardinality', target: 'agent-tests/records', unit: 'bytes', min: 16, max: 16},
  ]};
  await captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000);
  await sandbox.write('agent-tests/records', Buffer.from('{}\n\n'));
  await assert.rejects(captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000), /Expected a complete/);
  await sandbox.write('agent-tests/records', Buffer.from([0xff]));
  await assert.rejects(captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000), /encoded data|UTF-8/);
});

test('bounded input reads preserve path isolation, enforce aggregate budgets and reuse repeated reads', async t => {
  const {sandbox, baseline, f} = await contractFixture(t);
  await baseline.write('small.txt', Buffer.from('x'.repeat(150)));
  await baseline.write('second.txt', Buffer.from('x'.repeat(150)));
  await baseline.write('large.txt', Buffer.from('x'.repeat(300)));
  const context = {contractLimits: {maxCheckBytes: 256}};
  const contract = {inputs: [
    {kind: 'format', target: 'small.txt', format: 'utf8'},
    {kind: 'cardinality', target: 'small.txt', unit: 'bytes', max: 150},
  ]};
  await captureCandidate(sandbox, baseline, {...declaration, contract}, identity, 4000000, context);
  for (const target of ['large.txt', 'second.txt']) await assert.rejects(captureCandidate(sandbox, baseline,
    {...declaration, contract: {inputs: [...contract.inputs, {kind: 'format', target, format: 'utf8'}]}}, identity, 4000000, context), /bounded read limit|byte budget/);
  const outside = path.join(f.root, 'outside'); await fs.writeFile(outside, 'private');
  await fs.symlink(outside, path.join(baseline.root, 'escape.txt'));
  await assert.rejects(baseline.read('escape.txt', 100), /symlink/);
});

test('a rejected contract returns a tool error and a corrected declaration succeeds without extending the budget', async t => {
  const {sandbox, baseline, f} = await contractFixture(t);
  const cfg = config(); cfg.search.skills = [];
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'contract-repair', false);
  let calls = 0;
  const session = new AgentSession({id: 'contract-repair', role: 'candidate', strategy: '', task: f.task,
    config: cfg, budget, store, sandbox, baseline, model: {complete: async args => {
      calls++;
      if (calls === 1) return completion([['submit_candidate', {...declaration,
        contract: {outputs: [{kind: 'cardinality', target: 'agent-tests/run-tests.sh', unit: 'bytes', min: '1'}]}}]]);
      assert.match(JSON.stringify(args.messages), /contract|Invalid input/);
      return completion([['submit_candidate', {...declaration,
        contract: {outputs: [{kind: 'path', path: 'agent-tests/run-tests.sh'}]}}]]);
    }}});
  await session.run(3);
  assert.equal(calls, 2);
  assert.equal(budget.remainingCalls, cfg.budget.maxCalls - 2);
  assert.equal(session.candidate?.contractCheck?.origin, 'model');
  assert.equal(session.candidate?.contractCheck?.compatible, true);
  const events = await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8');
  assert.match(events, /candidate.contract/);
});
