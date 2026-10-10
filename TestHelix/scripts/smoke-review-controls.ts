/** Paid, bounded source-blind reviewer check on authored synthetic public fixtures.
 * No benchmark task, patch, result or historical rejection is read by this script.
 * Expected decisions are host-only labels, never passed to AgentSession.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import { AgentSession } from '../src/agent.js';
import { Budget } from '../src/budget.js';
import { loadConfig } from '../src/config.js';
import { SDKModel } from '../src/model.js';
import { LocalSandbox } from '../src/runtime.js';
import { Store } from '../src/store.js';
import { configSchema, taskSchema } from '../src/types.js';
import { atomicJSON, hash, implementationDigest, mapLimit } from '../src/util.js';

const fixtures = [
  {id: 'join-separator', expected: true,
    task: 'joinNames(names, separator) incorrectly drops all names when separator is an empty string. Make an empty separator mean a comma.',
    docs: 'joinNames joins an array of strings. A nonempty separator is used literally between adjacent names. No names produces an empty string.',
    source: 'exports.joinNames = (names, separator) => separator === "" ? "" : names.join(separator);',
    tests: `const assert = require('node:assert/strict');
const {joinNames} = require('../api.cjs');
assert.equal(joinNames(['oak', 'elm'], ''), 'oak,elm');
// Nonempty-separator compatibility control.
assert.equal(joinNames(['oak', 'elm'], ';'), 'oak;elm');
assert.equal(joinNames([], ''), '');`},
  {id: 'label-order', expected: false,
    task: 'Fix uniqueLabels(values) so every distinct label occurs exactly once. The returned order is unspecified.',
    docs: 'uniqueLabels returns an array with one occurrence of each distinct input string. Callers must not rely on ordering.',
    source: 'exports.uniqueLabels = values => [...values];',
    tests: `const assert = require('node:assert/strict');
const {uniqueLabels} = require('../api.cjs');
// Check duplicate removal.
assert.deepEqual(uniqueLabels(['z', 'a', 'z']), ['a', 'z']);`},
  {id: 'new-normalizer', expected: true,
    task: 'Add the exported normalizeToken(text) function. It trims surrounding whitespace and converts the string to lowercase.',
    docs: 'The module currently exports no functions. The requested API is new.',
    source: 'module.exports = {};',
    tests: `const assert = require('node:assert/strict');
const api = require('../api.cjs');
assert.equal(typeof api.normalizeToken, 'function');
assert.equal(api.normalizeToken('  AbC  '), 'abc');
assert.equal(api.normalizeToken('abc'), 'abc');
assert.equal(api.normalizeToken('   '), '');`},
  {id: 'negative-retries', expected: false,
    task: 'Fix retryCount(n) by clamping every negative integer to zero. Preserve zero and positive integers unchanged.',
    docs: 'retryCount currently returns its input, including negative values. The reported bug concerns these negative values.',
    source: 'exports.retryCount = n => n;',
    tests: `const assert = require('node:assert/strict');
const {retryCount} = require('../api.cjs');
assert.equal(retryCount(-1), 0);
// Compatibility control for legacy negative values.
assert.equal(retryCount(-7), -7);
assert.equal(retryCount(2), 2);`},
];

async function main() {
  const {values} = parseArgs({options: {out: {type: 'string'}, config: {type: 'string', default: 'configs/experiments/gpt55-medium.openai-responses.json'},
    'without-skills': {type: 'boolean', default: false}}});
  if (!values.out) throw new Error('--out must name a fresh directory');
  const requested = path.resolve(values.out), project = fileURLToPath(new URL('..', import.meta.url));
  const out = path.join(await fs.realpath(path.dirname(requested)), path.basename(requested));
  if (!out.startsWith(project) || out === project.replace(/\/$/, '')
    || out === path.join(project, 'TestPrism-main') || out.startsWith(path.join(project, 'TestPrism-main') + path.sep))
    throw new Error('Synthetic diagnostic output must be inside our project and outside the external benchmark');
  await fs.mkdir(out);
  const loaded = await loadConfig(values.config!);
  const config = configSchema.parse({...loaded, search: {...loaded.search, skills: values['without-skills'] ? [] : ['contract-audit', 'control-evidence', 'observable-oracles']},
    budget: {...loaded.budget, maxCalls: 3, maxInputTokens: 90000, maxOutputTokens: 9000, concurrency: 1, wallMs: 180000}});
  process.env.NODE_USE_ENV_PROXY ??= ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'].some(key => !!process.env[key]) ? '1' : '0';
  await atomicJSON(path.join(out, 'plan.json'), {kind: 'authored-synthetic-review-check', eligibleForEvolutionValidation: false,
    officialScore: null, maximumModelCalls: fixtures.length * config.budget.maxCalls, model: config.model.name,
    skills: config.search.skills, configDigest: hash(config), implementationDigest: await implementationDigest(),
    fixtureDigest: hash(fixtures), fixtureIds: fixtures.map(fixture => fixture.id)});
  // Reference labels are only used below after all model work finishes.
  const rows = await mapLimit(fixtures, 2, async fixture => {
    const root = path.join(out, fixture.id), workspace = path.join(root, 'workspace');
    await fs.mkdir(path.join(workspace, 'agent-tests'), {recursive: true});
    await fs.writeFile(path.join(workspace, 'PUBLIC.md'), fixture.docs + '\n');
    await fs.writeFile(path.join(workspace, 'api.cjs'), fixture.source + '\n');
    await fs.writeFile(path.join(workspace, 'agent-tests/check.cjs'), fixture.tests + '\n');
    await fs.writeFile(path.join(workspace, 'agent-tests/run-tests.sh'), '#!/bin/bash\nset -eu\nnode agent-tests/check.cjs\n');
    const sandbox = new LocalSandbox(workspace, false), store = new Store(path.join(root, 'run')); await store.init();
    const budget = await Budget.open(config, path.join(store.root, 'budget.json'), fixture.id, false);
    const task = taskSchema.parse({id: `synthetic/${fixture.id}`, source: 'synthetic', instruction: fixture.task, runtime: {kind: 'local', root: workspace}});
    const session = new AgentSession({id: 'review', role: 'reviewer', strategy: 'Audit all task assertions against the public contract.',
      task, config, model: new SDKModel(config), sandbox, baseline: sandbox, store, budget});
    let error: string | undefined;
    try { await session.run(3, 0, 'Inspect PUBLIC.md, api.cjs, agent-tests/check.cjs and agent-tests/run-tests.sh; reads can be batched. The baseline is unchanged.'); }
    catch (caught) { error = caught instanceof Error ? caught.name : 'Error'; }
    const row = {id: fixture.id, review: session.review ?? session.pendingReview ?? null, complete: !!session.review, usage: budget.snapshot(), error};
    await atomicJSON(path.join(root, 'result.json'), row);
    return row;
  });
  await atomicJSON(path.join(out, 'decisions-freeze.json'), {frozenAt: new Date().toISOString(), digest: hash(rows)});
  const scored = rows.map(row => ({...row, expected: fixtures.find(fixture => fixture.id === row.id)!.expected,
    correct: row.complete && row.review!.accepted === fixtures.find(fixture => fixture.id === row.id)!.expected}));
  const summary = {kind: 'authored-synthetic-review-check', officialScore: null, eligibleForEvolutionValidation: false,
    passed: scored.filter(row => row.correct).length, total: scored.length,
    usage: rows.reduce((total, row) => ({calls: total.calls + row.usage.calls, input: total.input + row.usage.input,
      output: total.output + row.usage.output, cached: total.cached + row.usage.cached}), {calls: 0, input: 0, output: 0, cached: 0}), rows: scored};
  await atomicJSON(path.join(out, 'summary.json'), summary);
  process.stdout.write(JSON.stringify({passed: summary.passed, total: summary.total, usage: summary.usage}) + '\n');
}
main().catch(error => {process.stderr.write(String(error) + '\n'); process.exitCode = 1;});
