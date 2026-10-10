/** Opt-in paid lifecycle control. Uses a synthetic public contract, no benchmark task or reference. */
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {loadConfig} from '../src/config.js';
import {SDKModel} from '../src/model.js';
import {DockerSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {taskSchema} from '../src/types.js';
import {atomicJSON, command, errorText, id, implementationDigest} from '../src/util.js';

const output = path.resolve(process.argv[2] ?? `.runs/skill-provider-${Date.now()}`);
await fs.mkdir(output); // Refuse to overwrite a paid run.
const implementation = await implementationDigest();
const settings = await loadConfig(process.argv[3] ?? 'configs/experiments/gpt55-medium.openai-responses.json');
settings.budget = {...settings.budget, maxCalls: 6, maxInputTokens: 100000, maxOutputTokens: 24000, wallMs: 300000, concurrency: 1};
const image = process.env.TESTHELIX_HARNESS_IMAGE ?? 'swebench/sweb.eval.x86_64.django_1776_django-11734:latest';
const name = id('testhelix-skill-provider');
const environment = '/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
try {
  const start = await command(['docker', 'run', '-d', '--name', name, '--label', 'testhelix.fixture=skill-provider', '--network', 'none',
    '-e', `PATH=${environment}`, '--workdir', '/workspace', '--entrypoint', 'sh', image, '-c', 'while :; do sleep 3600; done'], {timeoutMs: 120000});
  assert.equal(start.code, 0, start.stderr);
  const mkdir = await command(['docker', 'exec', name, 'mkdir', '-p', '/baseline']); assert.equal(mkdir.code, 0);
  const sandbox = new DockerSandbox(name, '/workspace', false), baseline = new DockerSandbox(name, '/baseline', false);
  // Django is installed in this public image. Model workspace contains only our fixture.
  const buggy = 'def total_values(queryset):\n    return queryset.count()\n';
  for (const workspace of [sandbox, baseline]) await workspace.write('totals.py', Buffer.from(buggy));
  const task = taskSchema.parse({id: 'synthetic/skill-lifecycle-django', instruction:
    'Fix the Django ORM helper total_values(queryset) in totals.py. Given a queryset of model rows with an integer value field, return the sum of those values as an integer. An empty queryset returns 0. Negative, zero and repeated values each contribute normally; respect queryset filtering and leave the rows unchanged. Create independent regression tests under agent-tests/.',
    runtime: {kind: 'docker', container: name, root: '/workspace'}});
  const store = new Store(output), budget = await Budget.open(settings, path.join(output, 'budget.json'), 'skill-provider', false);
  const session = new AgentSession({id: 'provider-control', role: 'candidate', strategy:
    'Use independent small examples to check the public contract; reuse the available generic Django harness if applicable.',
    task, config: settings, model: new SDKModel(settings), sandbox, baseline, store, budget});
  let failure: string | undefined;
  try { await session.run(6); } catch (error) { failure = errorText(error); }
  const events = (await fs.readFile(path.join(output, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  const candidate = session.candidate;
  let repaired, original;
  if (candidate) {
    for (const file of candidate.tests) await baseline.write(file.path, Buffer.from(file.data, 'base64'), file.mode);
    repaired = await sandbox.exec('bash agent-tests/run-tests.sh', 20000);
    original = await baseline.exec('bash agent-tests/run-tests.sh', 20000);
  }
  const procedures = events.filter(event => event.type === 'skills.procedure.completed').map(event => event.data);
  const evidence = {kind: 'Paid synthetic skill lifecycle control; no benchmark score or skill ablation', implementationDigest: implementation,
    image, model: settings.model.name, reasoningEffort: settings.model.providerOptions.openai?.reasoningEffort,
    usage: budget.snapshot(), failure, successfulResponses: events.filter(event => event.type === 'model.completed').length,
    apiErrors: events.filter(event => event.type === 'model.error').map(event => event.data),
    active: events.filter(event => event.type === 'skills.active').map(event => event.data), procedures,
    candidate: candidate ? {id: candidate.id, testsHash: candidate.testsHash, sourceHash: candidate.sourceHash, testFiles: candidate.tests.map(file => file.path)} : undefined,
    original, repaired};
  await atomicJSON(path.join(output, 'evidence.json'), evidence);
  process.stdout.write(JSON.stringify({output, model: evidence.model, usage: evidence.usage, successfulResponses: evidence.successfulResponses,
    procedures: procedures.map(p => ({key: p.key, outcome: p.outcome, activation: p.activation})), candidate: !!candidate,
    baselineCode: original?.code, repairedCode: repaired?.code, failure}) + '\n');
  assert.ok(candidate, failure ?? 'No candidate'); assert.equal(repaired?.code, 0);
  assert.notEqual(original?.code, 0); assert.equal(original?.timedOut, false);
  assert.ok(procedures.some(p => p.outcome === 'setup-control-pass'), 'No actual generic harness verification');
  assert.ok(procedures.some(p => p.key === 'artifact-workflow/write-and-run'), 'No actual suite procedure');
} finally { await command(['docker', 'rm', '-f', name]); }
