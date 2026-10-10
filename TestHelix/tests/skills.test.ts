import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { skillIds, skillOffer, builtinSkills } from '../src/skills.js';
import { Toolbox } from '../src/tools.js';
import { LocalSandbox } from '../src/runtime.js';
import { Store } from '../src/store.js';
import { AgentSession } from '../src/agent.js';
import { Budget } from '../src/budget.js';
import { configSchema, type Model } from '../src/types.js';
import { retryPlan } from '../src/retry.js';
import { config, fixture, FixtureModel } from './helpers.js';
import { genesis, applyGenome } from '../src/genome.js';

test('skills load only from enabled host IDs; task files cannot replace skill text', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.mkdir(path.join(f.workspace, 'skills'));
  await fs.writeFile(path.join(f.workspace, 'skills/framework-tests.md'), 'PRIVATE_TASK_ANSWER');
  const store = new Store(f.out); await store.init();
  const sandbox = new LocalSandbox(f.workspace, false);
  const tools = new Toolbox(sandbox, store, 3000, 1000, false, ['framework-tests']);
  const loaded = await tools.execute('load_skill', {id: 'framework-tests'}, AbortSignal.timeout(1000));
  assert.equal(loaded.text, builtinSkills['framework-tests'].text);
  assert.doesNotMatch(JSON.stringify(loaded), /PRIVATE_TASK_ANSWER/);
  await assert.rejects(tools.execute('load_skill', {id: '../solution.sh'}, AbortSignal.timeout(1000)));
  await assert.rejects(tools.execute('load_skill', {id: 'observable-oracles'}, AbortSignal.timeout(1000)), /not enabled/);
  await assert.rejects(tools.execute('load_skill', {id: 'framework-tests', path: 'solution.sh'}, AbortSignal.timeout(1000)));
  assert.throws(() => configSchema.parse({...config(), search: {skills: ['custom-task-answer']}}));
});

test('skills respect reviewer boundaries and native replacement prompts; disabled profiles add no tool or prefix', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out); await store.init();
  for (const enabled of [false, true]) {
    const settings = configSchema.parse({...config(), search: {...config().search, skills: enabled ? skillIds : []}});
    const budget = await Budget.open(settings, path.join(f.root, `budget-${enabled}.json`), `skills-${enabled}`, false);
    const model = new FixtureModel();
    const session = new AgentSession({id: `candidate-${enabled}`, role: 'candidate', task: {...f.task, systemPrompt: 'EXACT NATIVE CONTRACT', systemPromptMode: 'replace'},
      strategy: 'Check public semantics', config: settings, model, sandbox, baseline: sandbox, store, budget});
    await session.run(2);
    assert.equal(model.history[0]!.system, 'EXACT NATIVE CONTRACT');
    assert.equal('load_skill' in model.history[0]!.tools, enabled);
    assert.equal(JSON.stringify(model.history[0]!.messages).includes('Preloaded artifact-workflow'), enabled);
    const reviewer = new AgentSession({id: `review-${enabled}`, role: 'reviewer', task: f.task, strategy: 'Read tests',
      config: settings, model, sandbox, baseline: sandbox, store, budget});
    assert.deepEqual(reviewer.toolbox.skills, enabled ? ['observable-oracles', 'failure-triage', 'contract-audit', 'control-evidence'] : []);
    assert.ok(!JSON.stringify(reviewer.messages).includes('Preloaded artifact-workflow'));
    const evolved = applyGenome(settings, genesis(settings));
    assert.deepEqual(evolved.search.skills, settings.search.skills, 'evolution preserves the frozen skill set');
  }
  assert.ok(skillOffer(skillIds).text.length < 2600, 'only the workflow and short catalog are preloaded');
});

test('known connection failures release token reservations while ambiguous transport failures retain them', async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (const code of ['UND_ERR_CONNECT_TIMEOUT', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN']) {
    assert.equal(retryPlan({name: 'AI_APICallError', cause: {cause: {code}}}, 1, 1, 0).rejected, true);
  }
  for (const error of [{code: 'ECONNRESET'}, {code: 'UND_ERR_SOCKET'}, {name: 'TimeoutError'}, {statusCode: 502}])
    assert.equal(retryPlan(error, 0, 1, 0).rejected, false);
  const settings = configSchema.parse({...config(), model: {...config().model, maxRetryDelayMs: 0}});
  const budget = await Budget.open(settings, path.join(f.root, 'transport-budget.json'), 'transport', false);
  const store = new Store(f.out); await store.init(); const sandbox = new LocalSandbox(f.workspace, false);
  const model: Model = {complete: async () => { throw Object.assign(new Error('Cannot connect'), {name: 'AI_APICallError', cause: {code: 'UND_ERR_CONNECT_TIMEOUT'}}); }};
  const session = new AgentSession({id: 'transport', role: 'candidate', strategy: '', task: f.task, config: settings, model, sandbox, baseline: sandbox, store, budget});
  await assert.rejects(session.run(2), /Cannot connect/);
  const usage = budget.snapshot();
  assert.equal(usage.calls, 2); assert.equal(usage.input, 0); assert.equal(usage.output, 0); assert.equal(usage.estimated, false);
});
