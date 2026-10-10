import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { promises as fs } from 'node:fs';
import { config, completion, fixture } from './helpers.js';
import { atomicJSON, hash } from '../src/util.js';
import { assertDisjoint, inspectEvolutionPlan, loadRelease, rollbackEvolution, runEvolution } from '../src/evolve.js';
import { applyGenome, child, genesis } from '../src/genome.js';
import { pairedGate, pareto, type Evaluation } from '../src/evolution-metrics.js';
import { InterruptedEvaluation, type Evaluator } from '../src/evolution-evaluator.js';
import { trialSchema } from '../src/report.js';
import { Budget } from '../src/budget.js';
import type { Model } from '../src/types.js';
import { skillIds, skillOffer } from '../src/skills.js';
import { evolutionArms, selectEvolutionArm, trainingReward } from '../src/evolution-bandit.js';

const trial = (key: string, resolved: boolean) => trialSchema.parse({task_id: key, task_key: key, baseline_detected: true,
  patch_results: Array.from({length: 10}, (_, i) => ({patch_id: `p${i}`, label: i < 5 ? 'oracle_valid' : 'oracle_invalid',
    observed_status: i < 5 && !(i === 0 && !resolved) ? 'passed' : 'failed', failure_mode: 'none'})),
  total_input_tokens: 100, total_output_tokens: 50});
async function setup(root: string) {
  const manifest = async (name: string, size: number) => atomicJSON(path.join(root, `${name}.json`),
    {total_selected: size, sources: {'from-fixture': {task_ids: Array.from({length: size}, (_, i) => `${name}-${i}`)}}});
  await manifest('train', 4); await manifest('validation', 20); await manifest('holdout', 5);
  await atomicJSON(path.join(root, 'config.json'), config());
  const plan = {baseConfig: 'config.json', trainManifest: 'train.json', validationManifest: 'validation.json', holdoutManifest: 'holdout.json',
    benchmark: {root: '.', dataset: '.', patchSets: '.', python: 'python3'}, seed: 'fixture-seed', population: 2,
    generations: 1, offspring: 1, reflection: 'catalog', budget: {maxCalls: 5000, maxInputTokens: 50000000,
      maxOutputTokens: 10000000, wallMs: 60000, maxEvaluations: 10},
    promotion: {repeats: 2, minTasks: 20, confidence: .95, maxSourceRegression: 0, minGain: 0}};
  await atomicJSON(path.join(root, 'plan.json'), plan); return plan;
}
function evaluator(outcomes: (generation: number, split: string, key: string) => boolean, calls: string[]): Evaluator {
  return async request => {
    calls.push(`${request.genome.id}:${request.split}:${request.seed}`);
    return {genomeId: request.genome.id, split: request.split, seed: request.seed, taskKeys: request.samples.map(s => s.key),
      trials: request.samples.map(sample => trial(sample.key, outcomes(request.genome.generation, request.split, sample.key))),
      usage: {input: request.samples.length * 100, output: request.samples.length * 50, calls: request.samples.length * 2, cached: 0},
      durationMs: 1, evidenceDigest: hash({genome: request.genome.id, split: request.split, seed: request.seed}),
      feedback: [request.split === 'train' ? 'train-only-marker: undocumented assertion rejects a valid representation' : 'validation-secret-marker']};
  };
}
test('evolution closes mutation→strict evaluation→paired promotion→release→rollback, with deterministic resume', async t => {
  const f = await fixture(); t.after(f.cleanup); await setup(f.root); const calls: string[] = [];
  const evaluate = evaluator(generation => generation > 0, calls);
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluate});
  assert.equal(result.status, 'completed'); assert.equal(result.promoted, true); assert.equal(calls.length, 6);
  assert.equal((result.usage as any).calls, 176);
  const active = path.join(f.out, 'active.json');
  await assert.rejects(loadRelease(active), /synthetic/);
  const release = await loadRelease(active, {allowInjected: true}); assert.equal(release.status, 'promoted');
  assert.equal(release.genome.generation, 1); assert.ok(release.rollback);
  assert.equal(release.config.budget.maxCalls, config().budget.maxCalls, 'evolution cannot inflate task budget');
  const resumed = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluate, resume: true});
  assert.deepEqual(resumed, result); assert.equal(calls.length, 6);
  await rollbackEvolution(f.out, {allowInjected: true});
  assert.equal((await loadRelease(active, {allowInjected: true})).status, 'baseline');
  await assert.rejects(rollbackEvolution(f.out, {allowInjected: true}), /no rollback/);
});

test('paired training screening rejects a regression before full rollout and never becomes promotion evidence', async t => {
  const f=await fixture(); t.after(f.cleanup); const plan=await setup(f.root);
  await atomicJSON(path.join(f.root,'plan.json'), {...plan,screening:{enabled:true,tasks:2}});
  const requests: Array<{generation:number; split:string; size:number}> = [];
  const evaluate: Evaluator = async request => {
    requests.push({generation:request.genome.generation,split:request.split,size:request.samples.length});
    return evaluator(generation=>generation===0,[])(request);
  };
  const result=await runEvolution(path.join(f.root,'plan.json'),f.out,{evaluator:evaluate});
  assert.equal(result.promoted,false);
  assert.deepEqual(requests.filter(x=>x.split==='train').map(x=>[x.generation,x.size]),[[0,4],[0,2],[1,2]]);
  const state=JSON.parse(await fs.readFile(path.join(f.out,'evolution.json'),'utf8'));
  assert.equal(state.screens.length,2);
  assert.ok(state.evaluations.every((e:any)=>e.genomeId===state.genomes[0].id));
  const release=await loadRelease(path.join(f.out,'active.json'),{allowInjected:true});
  assert.equal(release.trainingEvidence.length,1,'screening evidence must stay out of full-training metrics');
  const count=requests.length;
  await runEvolution(path.join(f.root,'plan.json'),f.out,{evaluator:evaluate,resume:true});
  assert.equal(requests.length,count);
});

test('a screening winner still receives full training and independent validation', async t => {
  const f=await fixture(); t.after(f.cleanup); const plan=await setup(f.root);
  await atomicJSON(path.join(f.root,'plan.json'), {...plan,screening:{enabled:true,tasks:2}});
  const sizes: number[]=[];
  const result=await runEvolution(path.join(f.root,'plan.json'),f.out,{evaluator:async request=>{
    sizes.push(request.samples.length); return evaluator(g=>g>0,[])(request);
  }});
  assert.equal(result.promoted,true);
  assert.deepEqual(sizes,[4,2,2,4,20,20,20,20]);
});

test('a training winner that regresses on validation is rejected; validation never feeds the reflector', async t => {
  const f = await fixture(); t.after(f.cleanup); const plan = await setup(f.root); plan.reflection = 'model';
  await atomicJSON(path.join(f.root, 'plan.json'), plan); const calls: string[] = [], prompts: string[] = [];
  const model: Model = {complete: async args => {
    prompts.push(JSON.stringify(args.messages));
    return completion([['submit_mutation', {component: 'guidance', value: 'Check documented behavior on multiple valid representations.', rationale: 'Training shows overconstraint'}]]);
  }};
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {model,
    evaluator: evaluator((generation, split) => split === 'train' ? generation > 0 : generation === 0, calls)});
  assert.equal(result.promoted, false); assert.equal(prompts.length, 1);
  assert.match(prompts[0]!, /train-only-marker/); assert.doesNotMatch(prompts[0]!, /validation-secret-marker/);
  assert.equal((await loadRelease(path.join(f.out, 'active.json'), {allowInjected: true})).genome.generation, 0);
});

test('evolution rejects split leakage, undeclared multi-gene mutations and budget/scoring changes', () => {
  assert.throws(() => assertDisjoint([{key: 's/a', source: 's'}], [{key: 's/a', source: 's'}], []), /overlaps/);
  const baseline = genesis(config());
  assert.throws(() => child(baseline, {...baseline.genes, guidance: 'one', agentSteps: 9},
    {component: 'guidance', rationale: 'why', evidence: ['training'], operator: 'reflection'}, []), /exactly/);
  assert.throws(() => child(baseline, {...baseline.genes, guidance: 'Memorize from-swe-bench/project-123'},
    {component: 'guidance', rationale: 'why', evidence: ['training'], operator: 'reflection'}, ['from-swe-bench/project-123']), /identifier/);
  assert.throws(() => child(baseline, {...baseline.genes, maxInputTokens: 999999999},
    {component: 'guidance', rationale: 'why', evidence: ['training'], operator: 'reflection'}, []));
  assert.equal(applyGenome(config(), baseline).search.review, true);
});

test('promotion clusters repeated measurements by task and protects source regressions', () => {
  const evaluation = (id: string, seed: number, keys: string[], solved: (key: string) => boolean): Evaluation => ({genomeId: id,
    split: 'validation', seed, taskKeys: keys, trials: keys.map(key => trial(key, solved(key))),
    usage: {input: 10, output: 10, calls: 1, cached: 0}, durationMs: 1, evidenceDigest: id + seed});
  const keys = ['one/a', 'two/b'];
  const base = [0, 1, 2].map(seed => evaluation('base', seed, keys, () => false));
  const next = [0, 1, 2].map(seed => evaluation('next', seed, keys, () => true));
  const gate = pairedGate(base, next, {seed: 'fixed', minTasks: 3, minGain: 0, maxSourceRegression: 0, confidence: .95});
  assert.equal(gate.pairedTasks, 2); assert.equal(gate.accepted, false, 'six repeated results are still only two independent tasks');
  const baseline = genesis(config()), evolved = child(baseline, {...baseline.genes, guidance: 'Reusable rule'},
    {component: 'guidance', rationale: 'training', evidence: ['train'], operator: 'catalog'}, []);
  const a = {...evaluation(baseline.id, 0, keys, key => key.startsWith('one')), split: 'train' as const};
  const b = {...evaluation(evolved.id, 0, keys, key => key.startsWith('two')), split: 'train' as const};
  assert.equal(pareto([baseline, evolved], [a, b], 2).length, 2, 'preserve complementary specialists');
});

test('whole-evaluation budget reservations settle exactly once, including after restart', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const file = path.join(f.root, 'budget.json'), budget = await Budget.open(settings, file, 'evo', false);
  const reservation = await budget.reserveWork({input: 10000, output: 10000, calls: 5});
  const resumed = await Budget.open(settings, file, 'evo', true);
  await resumed.settle(reservation, {input: 300, output: 200, calls: 2, cached: 0});
  const once = resumed.snapshot(); await resumed.settle(reservation, {input: 300, output: 200, calls: 2, cached: 0});
  assert.deepEqual(resumed.snapshot(), once); assert.equal(once.calls, 2); assert.equal(once.input, 300);
});

test('an exhausted evolution budget never creates an unvalidated active release', async t => {
  const f = await fixture(); t.after(f.cleanup); const plan = await setup(f.root); plan.budget.maxEvaluations = 1;
  await atomicJSON(path.join(f.root, 'plan.json'), plan);
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluator(g => g > 0, [])});
  assert.equal(result.status, 'budget_exhausted'); await assert.rejects(fs.stat(path.join(f.out, 'active.json')));
});

test('an interrupted evaluation retains unknown spend and receives a fresh reservation before retry', async t => {
  const f = await fixture(); t.after(f.cleanup); await setup(f.root);
  const directories: string[] = [], delegate = evaluator(generation => generation > 0, []);
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: async request => {
    directories.push(request.directory);
    if (directories.length === 1) throw new InterruptedEvaluation('Synthetic interruption with uncertain spend');
    return delegate(request);
  }});
  assert.equal(result.promoted, true); assert.equal(result.evaluated, 7);
  assert.notEqual(directories[0], directories[1]);
  assert.equal((result.usage as any).calls, 256); assert.equal((result.usage as any).estimated, true);
  assert.equal((result.usage as any).input, 1208800);
});

test('efficiency promotion requires measured savings and no paired scoring regression; accuracy remains the default', () => {
  const keys = Array.from({length: 20}, (_, i) => `s/${i}`);
  const evaluation = (id: string, tokens: number): Evaluation => ({genomeId: id, split: 'validation', seed: 0, taskKeys: keys,
    trials: keys.map(key => trial(key, true)), usage: {input: tokens, output: tokens, calls: 20, cached: 0}, durationMs: 1, evidenceDigest: id});
  const base = evaluation('base', 1000), next = evaluation('next', 700);
  const options = {seed: 'cost', minTasks: 20, minGain: 0, maxSourceRegression: 0, confidence: .95};
  assert.equal(pairedGate([base], [next], options).accepted, false);
  const gate = pairedGate([base], [next], {...options, mode: 'efficiency', minTokenReduction: .15});
  assert.equal(gate.accepted, true); assert.equal(gate.reason, 'paired_quality_preserved_token_reduction');
  assert.ok(Math.abs(gate.tokenReduction - .3) < 1e-6);
  const estimated = structuredClone(next); estimated.usage.estimated = true;
  assert.equal(pairedGate([base], [estimated], {...options, mode: 'efficiency'}).accepted, false);
  const regressed = structuredClone(next); regressed.trials[0] = trial(keys[0]!, false);
  assert.equal(pairedGate([base], [regressed], {...options, mode: 'efficiency'}).accepted, false);
});

test('evolution can tune context or reviewer output with one gene while keeping provider and global budgets fixed', () => {
  const settings = config(), base = genesis(settings);
  const next = child(base, {...base.genes, toolOutputChars: 2400}, {component: 'toolOutputChars', rationale: 'Training output redundancy', evidence: ['train'], operator: 'reflection'}, []);
  const changed = applyGenome(settings, next);
  assert.equal(changed.context.toolOutputChars, 2400); assert.deepEqual(changed.budget, settings.budget);
  assert.deepEqual(changed.model, settings.model); assert.deepEqual(changed.search, settings.search);
});

test('training bandit credits paired quality, excludes validation rewards and rejects bad identities', () => {
  const base = genesis(config());
  const evolved = child(base, {...base.genes, guidance: 'Verify alternatives'},
    {component: 'guidance', rationale: 'Training', evidence: ['train'], operator: 'catalog', arm: 'guidance'}, []);
  const evidence = (id: string, resolved: boolean): Evaluation => ({genomeId: id, split: 'train', seed: 0,
    taskKeys: ['s/a', 's/b'], trials: ['s/a', 's/b'].map(key => trial(key, resolved)),
    usage: {calls: 1, input: 100, output: 10, cached: 0}, durationMs: 1, evidenceDigest: id});
  const left = evidence(base.id, false), right = evidence(evolved.id, true);
  assert.ok(trainingReward(left, right) > .5); assert.equal(trainingReward(right, left), 0);
  assert.throws(() => trainingReward({...left, split: 'validation'}, right), /training/);
  assert.throws(() => trainingReward(left, {...right, taskKeys: ['s/a', 's/a']}), /taskKeys/);
  assert.throws(() => trainingReward(left, {...right, seed: 1}), /paired/);
  const arms = [{id: 'guidance', component: 'guidance' as const}, {id: 'lessons', component: 'lessons' as const}];
  const selected = selectEvolutionArm(arms, {guidance: 1, lessons: 1}, [base, evolved], [left, right], []);
  assert.equal(selected.arm.id, 'guidance');
  const polluted = selectEvolutionArm(arms, {guidance: 1, lessons: 1}, [base, evolved],
    [left, right, {...left, split: 'validation'}, {...right, split: 'validation', trials: right.trials.map(t => trial(t.task_id, false))}], []);
  assert.deepEqual(polluted, selected, 'validation outcomes must never affect search selection');
});

test('evolution explores specialist preloads from the on-demand default and preserves explicit abstention', () => {
  const settings = config(), parent = genesis(settings);
  const samples = ['a', 'b'].map(key => ({key, source: 'local', context: 'general' as const}));
  const routes = () => evolutionArms(parent, skillIds, samples).filter(arm => arm.component === 'skillRoutes');
  assert.deepEqual(skillOffer(skillIds, false, {instruction: 'Repair compatibility', source: 'local', routes: []}).preloaded,
    ['artifact-workflow']);
  assert.deepEqual(routes().map(arm => arm.skill), ['contract-partitions', 'observable-oracles', null]);
  parent.genes.skillRoutes = [{context: 'general', skill: null}];
  assert.deepEqual(routes().map(arm => arm.skill), ['contract-partitions', 'observable-oracles']);
  parent.genes.skillRoutes = [{context: 'general', skill: 'contract-partitions'}];
  assert.deepEqual(routes().map(arm => arm.skill), ['observable-oracles', null]);
});

test('UCB changes real skill preload policy, freezes the winner and resumes without new experiments', async t => {
  const f = await fixture(); t.after(f.cleanup); await setup(f.root);
  const settings = config(); settings.search.skills = [...skillIds]; await atomicJSON(path.join(f.root, 'config.json'), settings);
  const requests: string[] = [], delegate = evaluator(generation => generation > 0, requests);
  const evaluate: Evaluator = async request => {
    if (request.genome.generation > 0) {
      assert.equal(request.genome.mutation!.component, 'skillRoutes');
      assert.equal(request.genome.mutation!.arm, 'route:general:contract-partitions');
      const offer = skillOffer(request.config.search.skills, false, {instruction: 'Repair compatibility', source: 'local', routes: request.config.search.skillRoutes});
      assert.deepEqual(offer.preloaded, ['artifact-workflow', 'contract-partitions']);
    }
    return delegate(request);
  };
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluate});
  assert.equal(result.promoted, true); assert.equal(requests.length, 6);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.filter(event => event.type === 'evolution.arm_selected').length, 1);
  const resume = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluate, resume: true});
  assert.deepEqual(resume, result); assert.equal(requests.length, 6);
});

test('reflection extracts one structured lesson without resending the complete lesson library', async t => {
  const f = await fixture(); t.after(f.cleanup); const plan = await setup(f.root); plan.offspring = 2; plan.reflection = 'model';
  await atomicJSON(path.join(f.root, 'plan.json'), plan);
  const prompts: string[] = [];
  const model: Model = {complete: async args => {
    prompts.push(JSON.stringify(args.messages));
    if (args.forceTool === 'submit_lesson') return completion([['submit_lesson', {operation: 'upsert', id: 'representation-anchor',
      triggers: ['serialization'], when: 'Round trips can hide coupled mistakes.', action: 'Add independent anchors.',
      verify: 'Documented representations satisfy both anchored and round-trip checks.', rationale: 'Training exposed overconstraint'}]]);
    return completion([['submit_mutation', {component: 'guidance', value: 'Check valid alternative representations.', rationale: 'Training showed overconstraint'}]]);
  }};
  await runEvolution(path.join(f.root, 'plan.json'), f.out, {model, evaluator: evaluator(g => g > 0, [])});
  const state = JSON.parse(await fs.readFile(path.join(f.out, 'evolution.json'), 'utf8'));
  const extracted = state.genomes.find((genome: any) => genome.mutation?.component === 'lessons');
  assert.ok(extracted); assert.match(extracted.genes.lessons[0].text, /When:.*\nAction:.*\nVerify:/);
  assert.equal(prompts.length, 2); assert.ok(prompts.every(prompt => !prompt.includes('validation-secret-marker')));
});

test('capacity planning is read-only and search leaves evaluation slots for the full validation gate', async t => {
  const f = await fixture(); t.after(f.cleanup); const plan = await setup(f.root);
  plan.offspring = 3; plan.budget.maxEvaluations = 6; await atomicJSON(path.join(f.root, 'plan.json'), plan);
  const capacity = await inspectEvolutionPlan(path.join(f.root, 'plan.json'));
  assert.equal(capacity.gateEvaluations, 4); assert.equal(capacity.protectedValidation.calls, 1600);
  assert.equal(capacity.fitsOneComparisonCaps, true); assert.equal(capacity.fitsPlannedRolloutCaps, false);
  const requests: string[] = [];
  const result = await runEvolution(path.join(f.root, 'plan.json'), f.out, {evaluator: evaluator(g => g > 0, requests)});
  assert.equal(result.status, 'completed'); assert.equal(result.promoted, true);
  assert.equal(requests.filter(request => request.includes(':train:')).length, 2);
  assert.equal(requests.filter(request => request.includes(':validation:')).length, 4);
  assert.equal(result.proposals, 1);
});
